"""
Graph builder — converts FileSymbols into Memgraph nodes and edges.
Uses MERGE (upsert) everywhere so re-indexing is safe and idempotent.
Batch inserts via UNWIND for performance on large codebases.
"""
from __future__ import annotations

import re
import structlog

from src.graph import client as g
from src.models.symbol import FileSymbols, SymbolKind

log = structlog.get_logger(__name__)

_BATCH_SIZE = 500  # Max items per UNWIND (Memgraph parameter limit safety)


def _chunked(items: list, size: int):
    """Yield successive chunks of `size` from `items`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def upsert_repository(repo_id: str, name: str, url: str, default_branch: str = "main") -> None:
    g.run_void(
        """
        MERGE (r:Repository {id: $id})
        SET r.name = $name, r.url = $url, r.default_branch = $branch
        """,
        {"id": repo_id, "name": name, "url": url, "branch": default_branch},
    )


def upsert_file(repo_id: str, fs: FileSymbols) -> None:
    """
    Insert or update the File node for a parsed file.
    Does NOT touch symbol nodes — call upsert_symbols separately.
    """
    g.run_void(
        """
        MERGE (f:File {id: $id})
        SET f.path = $path,
            f.repo_id = $repo_id,
            f.language = $language,
            f.lines = $lines,
            f.content_hash = $hash,
            f.is_test = $is_test,
            f.is_generated = $is_generated,
            f.parse_error = $parse_error
        """,
        {
            "id": f"{repo_id}:{fs.path}",
            "path": fs.path,
            "repo_id": repo_id,
            "language": fs.language.value,
            "lines": fs.lines,
            "hash": fs.content_hash,
            "is_test": fs.is_test,
            "is_generated": fs.is_generated,
            "parse_error": fs.parse_error,
        },
    )


def upsert_symbols(repo_id: str, fs: FileSymbols) -> None:
    """
    Batch insert Function, Class, Variable nodes and CONTAINS edges.
    Groups symbols by kind (label), then uses UNWIND for each group.
    """
    file_id = f"{repo_id}:{fs.path}"

    # Group symbols by kind for label-specific MERGE
    by_kind: dict[str, list[dict]] = {}
    for sym in fs.symbols:
        sym.make_id(repo_id)
        label = sym.kind.value
        by_kind.setdefault(label, []).append({
            "id": sym.id,
            "name": sym.name,
            "qname": sym.qualified_name,
            "file_path": fs.path,
            "repo_id": repo_id,
            "line_start": sym.line_start,
            "line_end": sym.line_end,
            "is_exported": sym.is_exported,
            "is_async": sym.is_async,
            "docstring": sym.docstring,
            "file_id": file_id,
        })

    for label, items in by_kind.items():
        for chunk in _chunked(items, _BATCH_SIZE):
            g.run_void(
                f"""
                UNWIND $items AS item
                MERGE (s:{label} {{id: item.id}})
                SET s.name = item.name,
                    s.qualified_name = item.qname,
                    s.file_path = item.file_path,
                    s.repo_id = item.repo_id,
                    s.line_start = item.line_start,
                    s.line_end = item.line_end,
                    s.is_exported = item.is_exported,
                    s.is_async = item.is_async,
                    s.docstring = item.docstring
                WITH s, item
                MATCH (f:File {{id: item.file_id}})
                MERGE (f)-[:CONTAINS {{line: item.line_start}}]->(s)
                """,
                {"items": chunk},
            )


def upsert_imports(repo_id: str, fs: FileSymbols) -> None:
    """
    Batch create Module nodes and IMPORTS edges via UNWIND.
    """
    file_id = f"{repo_id}:{fs.path}"

    items = []
    for imp in fs.imports:
        module_id = f"{repo_id}:{imp.raw_path}"
        items.append({
            "mod_id": module_id,
            "raw_path": imp.raw_path,
            "repo_id": repo_id,
            "file_id": file_id,
            "line": imp.line,
        })

    for chunk in _chunked(items, _BATCH_SIZE):
        g.run_void(
            """
            UNWIND $items AS item
            MERGE (m:Module {id: item.mod_id})
            SET m.import_path = item.raw_path,
                m.repo_id = item.repo_id,
                m.is_external = false
            WITH m, item
            MATCH (f:File {id: item.file_id})
            MERGE (f)-[:IMPORTS {line: item.line}]->(m)
            """,
            {"items": chunk},
        )


def upsert_decorators(repo_id: str, fs: FileSymbols) -> None:
    """
    Batch create DECORATED_BY edges via UNWIND.
    """
    items = []
    for dec in fs.decorators:
        items.append({
            "target": dec.target_name,
            "decorator": dec.decorator_name,
            "repo_id": repo_id,
            "path": fs.path,
            "order": dec.order,
        })

    if not items:
        return

    for chunk in _chunked(items, _BATCH_SIZE):
        g.run_void(
            """
            UNWIND $items AS item
            MATCH (target {name: item.target, repo_id: item.repo_id, file_path: item.path})
            MATCH (decorator {name: item.decorator, repo_id: item.repo_id})
            MERGE (target)-[:DECORATED_BY {order: item.order}]->(decorator)
            """,
            {"items": chunk},
        )


def upsert_calls(repo_id: str, fs: FileSymbols) -> None:
    """
    Batch create CALLS edges via UNWIND.
    """
    items = []
    for call in fs.calls:
        items.append({
            "caller": call.caller_name,
            "callee": call.callee_name,
            "repo_id": repo_id,
            "path": fs.path,
            "line": call.line,
            "is_dynamic": call.is_dynamic,
        })

    if not items:
        return

    for chunk in _chunked(items, _BATCH_SIZE):
        g.run_void(
            """
            UNWIND $items AS item
            MATCH (caller)
            WHERE caller.name = item.caller AND caller.repo_id = item.repo_id AND caller.file_path = item.path
            WITH caller, item
            MATCH (callee)
            WHERE callee.name = item.callee AND callee.repo_id = item.repo_id
            MERGE (caller)-[:CALLS {line: item.line, is_dynamic: item.is_dynamic}]->(callee)
            """,
            {"items": chunk},
        )


def upsert_test_edges(repo_id: str, fs: FileSymbols) -> None:
    """
    If this file is a test, create TEST_FOR edges to the source files it likely tests.

    Two strategies (combined):
    1. Name matching: test_foo.py → foo.py, foo_test.py → foo.py
    2. Import matching: resolved imports from this test file → source files
    """
    if not fs.is_test:
        return

    test_file_id = f"{repo_id}:{fs.path}"
    source_stems: set[str] = set()

    # Strategy 1: Name-based matching
    from pathlib import Path
    stem = Path(fs.path).stem  # e.g. "test_routes" or "routes_test"
    if stem.startswith("test_"):
        source_stems.add(stem[5:])  # "routes"
    elif stem.endswith("_test"):
        source_stems.add(stem[:-5])  # "routes"
    elif stem.startswith("test"):
        # testRoutes → Routes → routes
        remainder = stem[4:]
        if remainder:
            source_stems.add(remainder.lower())
            source_stems.add(remainder)

    # Create TEST_FOR via name matching (find source files whose stem matches)
    for src_stem in source_stems:
        g.run_void(
            """
            MATCH (test:File {id: $test_id})
            MATCH (src:File {repo_id: $repo_id})
            WHERE src.is_test = false
              AND src.path ENDS WITH $suffix_py
            MERGE (test)-[:TEST_FOR]->(src)
            """,
            {
                "test_id": test_file_id,
                "repo_id": repo_id,
                "suffix_py": f"{src_stem}.py",
            },
        )
        # Also match .ts/.js source files
        for ext in [".ts", ".js", ".tsx", ".jsx"]:
            g.run_void(
                """
                MATCH (test:File {id: $test_id})
                MATCH (src:File {repo_id: $repo_id})
                WHERE src.is_test = false
                  AND src.path ENDS WITH $suffix
                MERGE (test)-[:TEST_FOR]->(src)
                """,
                {
                    "test_id": test_file_id,
                    "repo_id": repo_id,
                    "suffix": f"{src_stem}{ext}",
                },
            )

    # Strategy 2: Import-based matching (link to resolved import targets)
    g.run_void(
        """
        MATCH (test:File {id: $test_id})
        MATCH (test)-[:IMPORTS]->(m:Module)-[:RESOLVES_TO]->(src:File)
        WHERE src.repo_id = $repo_id AND src.is_test = false
        MERGE (test)-[:TEST_FOR]->(src)
        """,
        {"test_id": test_file_id, "repo_id": repo_id},
    )

    log.debug("graph.test_edges", test_file=fs.path, repo_id=repo_id)


def upsert_inheritance(repo_id: str, fs: FileSymbols) -> None:
    """Create INHERITS edges for classes with base classes."""
    items = []
    for sym in fs.symbols:
        if sym.kind == SymbolKind.CLASS and sym.bases:
            for base in sym.bases:
                # Skip builtins like object, Exception, etc.
                if base in ("object",):
                    continue
                items.append({
                    "child_id": sym.id or f"{repo_id}:{sym.file_path}:{sym.line_start}:{sym.name}",
                    "parent_name": base,
                    "repo_id": repo_id,
                })
    if not items:
        return

    for chunk in _chunked(items, _BATCH_SIZE):
        g.run_void(
            """
            UNWIND $items AS item
            MATCH (child:Class {id: item.child_id})
            MATCH (parent:Class {name: item.parent_name, repo_id: item.repo_id})
            MERGE (child)-[:INHERITS]->(parent)
            """,
            {"items": chunk},
        )
    log.debug("graph.inheritance.upserted", repo_id=repo_id, path=fs.path, edges=len(items))


def insert_all(repo_id: str, fs: FileSymbols) -> None:
    """
    Full insert pipeline for a single file.
    Call in order: file → symbols → imports → calls → decorators → test edges → inheritance.
    """
    upsert_file(repo_id, fs)
    upsert_symbols(repo_id, fs)
    upsert_imports(repo_id, fs)
    upsert_calls(repo_id, fs)
    upsert_decorators(repo_id, fs)
    upsert_test_edges(repo_id, fs)
    upsert_inheritance(repo_id, fs)
    log.debug("graph.file.inserted", repo_id=repo_id, path=fs.path, symbols=len(fs.symbols))
