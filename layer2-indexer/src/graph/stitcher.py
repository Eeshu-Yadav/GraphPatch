"""
Graph stitcher — resolves Module placeholder nodes to actual File nodes
and creates cross-file edges after all files in a repo have been parsed.

Run once after full index, and per-file for incremental updates.
"""
from __future__ import annotations

import structlog

from src.graph import client as g
from src.models.symbol import Language
from src.resolution.python import PythonResolver
from src.resolution.typescript import TypeScriptResolver

log = structlog.get_logger(__name__)


def stitch_repo(repo_id: str, repo_root: str, all_files: set[str]) -> None:
    """
    Resolve all unresolved Module nodes in a repo.
    For each File -[:IMPORTS]-> Module, try to link Module to the actual File it represents.
    """
    py_resolver = PythonResolver(repo_root, all_files)
    ts_resolver = TypeScriptResolver(repo_root, all_files)

    # Fetch all unresolved import edges:
    # - never resolved (resolved_file IS NULL), OR
    # - resolved but RESOLVES_TO edge is missing (retry idempotently)
    unresolved = g.run(
        """
        MATCH (src:File {repo_id: $repo_id})-[imp:IMPORTS]->(m:Module {repo_id: $repo_id})
        WHERE NOT m.is_external = true
          AND NOT (m)-[:RESOLVES_TO]->()
        RETURN src.path AS src_path, src.language AS lang,
               m.import_path AS raw_path, m.id AS mod_id,
               imp.line AS line
        """,
        {"repo_id": repo_id},
    )

    resolved_batch: list[dict] = []
    external_batch: list[str] = []

    for row in unresolved:
        src_path: str = row["src_path"]
        lang: str = row["lang"]
        raw_path: str = row["raw_path"]
        mod_id: str = row["mod_id"]

        resolved_file: str | None = None

        if lang == Language.PYTHON.value:
            is_relative = raw_path.startswith(".")
            resolved_file = py_resolver.resolve(raw_path, src_path, is_relative)
        elif lang == Language.TYPESCRIPT.value:
            resolved_file = ts_resolver.resolve(raw_path, src_path)

        if resolved_file:
            resolved_batch.append({"mod_id": mod_id, "resolved_file": resolved_file})
        else:
            external_batch.append(mod_id)

    # Batch insert resolved modules (UNWIND)
    batch_size = 500
    for i in range(0, len(resolved_batch), batch_size):
        chunk = resolved_batch[i:i + batch_size]
        g.run_void(
            """
            UNWIND $items AS item
            MATCH (m:Module {id: item.mod_id})
            SET m.resolved_file = item.resolved_file
            WITH m, item
            MATCH (target:File {repo_id: $repo_id, path: item.resolved_file})
            MERGE (m)-[:RESOLVES_TO]->(target)
            """,
            {"items": chunk, "repo_id": repo_id},
        )

    # Batch mark external modules (UNWIND)
    for i in range(0, len(external_batch), batch_size):
        chunk = external_batch[i:i + batch_size]
        g.run_void(
            """
            UNWIND $ids AS mod_id
            MATCH (m:Module {id: mod_id})
            SET m.is_external = true
            """,
            {"ids": chunk},
        )

    log.info(
        "stitch.complete",
        repo_id=repo_id,
        resolved=len(resolved_batch),
        external=len(external_batch),
    )


def stitch_file(repo_id: str, repo_root: str, all_files: set[str], file_path: str) -> None:
    """
    Re-stitch a single file after incremental update.
    Also re-stitches files that import the updated file (they may gain a new resolution).
    """
    py_resolver = PythonResolver(repo_root, all_files)
    ts_resolver = TypeScriptResolver(repo_root, all_files)

    # Re-stitch the updated file's own imports
    unresolved = g.run(
        """
        MATCH (src:File {repo_id: $repo_id, path: $path})-[:IMPORTS]->(m:Module {repo_id: $repo_id})
        WHERE NOT m.is_external = true AND NOT (m)-[:RESOLVES_TO]->()
        RETURN src.path AS src_path, src.language AS lang,
               m.import_path AS raw_path, m.id AS mod_id
        """,
        {"repo_id": repo_id, "path": file_path},
    )

    for row in unresolved:
        lang = row["lang"]
        raw_path = row["raw_path"]
        mod_id = row["mod_id"]
        resolved_file = None

        if lang == Language.PYTHON.value:
            resolved_file = py_resolver.resolve(raw_path, row["src_path"], raw_path.startswith("."))
        elif lang == Language.TYPESCRIPT.value:
            resolved_file = ts_resolver.resolve(raw_path, row["src_path"])

        if resolved_file:
            g.run_void(
                """
                MATCH (m:Module {id: $mod_id})
                SET m.resolved_file = $resolved_file, m.is_external = false
                WITH m
                MATCH (target:File {repo_id: $repo_id, path: $resolved_file})
                MERGE (m)-[:RESOLVES_TO]->(target)
                """,
                {"mod_id": mod_id, "repo_id": repo_id, "resolved_file": resolved_file},
            )
        else:
            g.run_void(
                "MATCH (m:Module {id: $id}) SET m.is_external = true",
                {"id": mod_id},
            )
