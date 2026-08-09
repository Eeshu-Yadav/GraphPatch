"""
CODEOWNERS parser — reads .github/CODEOWNERS (or root CODEOWNERS) and creates
OWNED_BY edges from File nodes to Owner nodes in Memgraph.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path

import structlog

from src.graph import client as g

log = structlog.get_logger(__name__)


def _find_codeowners(repo_root: str) -> Path | None:
    """Find the CODEOWNERS file in standard locations."""
    root = Path(repo_root)
    for candidate in [
        root / ".github" / "CODEOWNERS",
        root / "CODEOWNERS",
        root / "docs" / "CODEOWNERS",
    ]:
        if candidate.exists():
            return candidate
    return None


def _parse_codeowners(content: str) -> list[tuple[str, list[str]]]:
    """
    Parse CODEOWNERS file content.
    Returns: [(pattern, [owner1, owner2, ...]), ...]
    Later entries take precedence (GitHub semantics), but we store all.
    """
    rules: list[tuple[str, list[str]]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        owners = [p for p in parts[1:] if p.startswith("@") or "@" in p]
        if owners:
            rules.append((pattern, owners))
    return rules


def _pattern_matches(pattern: str, file_path: str) -> bool:
    """Check if a CODEOWNERS pattern matches a file path."""
    # Leading slash means root-relative
    if pattern.startswith("/"):
        return fnmatch.fnmatch(file_path, pattern.lstrip("/"))

    # Directory pattern (trailing /)
    if pattern.endswith("/"):
        return file_path.startswith(pattern) or f"/{pattern}" in f"/{file_path}"

    # Wildcard patterns
    if "*" in pattern:
        return fnmatch.fnmatch(file_path, pattern) or fnmatch.fnmatch(
            file_path.split("/")[-1], pattern
        )

    # Exact match or suffix match
    return file_path == pattern or file_path.endswith(f"/{pattern}")


def parse_and_store_codeowners(repo_id: str, repo_root: str) -> int:
    """
    Parse CODEOWNERS file and create OWNED_BY edges in Memgraph.
    Creates Owner nodes and (File)-[:OWNED_BY {pattern}]->(Owner) edges.
    Returns number of edges created.
    """
    codeowners_path = _find_codeowners(repo_root)
    if not codeowners_path:
        log.info("codeowners.not_found", repo_id=repo_id)
        return 0

    content = codeowners_path.read_text(encoding="utf-8", errors="replace")
    rules = _parse_codeowners(content)
    if not rules:
        log.info("codeowners.empty", repo_id=repo_id)
        return 0

    log.info("codeowners.parsed", repo_id=repo_id, rules=len(rules))

    # Get all file paths in the repo from Memgraph
    files = g.run(
        "MATCH (f:File {repo_id: $repo_id}) RETURN f.path AS path",
        {"repo_id": repo_id},
    )
    file_paths = [r["path"] for r in files]

    edges_created = 0
    for pattern, owners in rules:
        matching_files = [fp for fp in file_paths if _pattern_matches(pattern, fp)]
        for owner in owners:
            # Create Owner node
            g.run_void(
                """
                MERGE (o:Owner {name: $name, repo_id: $repo_id})
                """,
                {"name": owner, "repo_id": repo_id},
            )
            # Create OWNED_BY edges for matching files
            for fp in matching_files:
                file_id = f"{repo_id}:{fp}"
                g.run_void(
                    """
                    MATCH (f:File {id: $file_id})
                    MATCH (o:Owner {name: $owner, repo_id: $repo_id})
                    MERGE (f)-[:OWNED_BY {pattern: $pattern}]->(o)
                    """,
                    {
                        "file_id": file_id,
                        "owner": owner,
                        "repo_id": repo_id,
                        "pattern": pattern,
                    },
                )
                edges_created += 1

    log.info("codeowners.done", repo_id=repo_id, edges=edges_created)
    return edges_created
