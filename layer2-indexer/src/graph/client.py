"""
Memgraph client — connection pool, query execution, transaction helpers.
All graph operations go through this module.
"""
from __future__ import annotations

import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings

log = structlog.get_logger(__name__)

# Lazy import — gqlalchemy connects on first use
_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        from gqlalchemy import Memgraph
        _driver = Memgraph(host=settings.memgraph_host, port=settings.memgraph_port)
        log.info("memgraph.connected", host=settings.memgraph_host, port=settings.memgraph_port)
    return _driver


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def run(query: str, params: dict | None = None) -> list[dict]:
    """Execute a Cypher query and return results as a list of dicts."""
    driver = _get_driver()
    results = list(driver.execute_and_fetch(query, params or {}))
    return results


def run_void(query: str, params: dict | None = None) -> None:
    """Execute a Cypher query that returns no results (CREATE, MERGE, DELETE)."""
    driver = _get_driver()
    driver.execute(query, params or {})


def setup_indexes() -> None:
    """Create all required graph indexes. Safe to run multiple times (IF NOT EXISTS)."""
    indexes = [
        "CREATE INDEX ON :Function(name);",
        "CREATE INDEX ON :Function(repo_id);",
        "CREATE INDEX ON :File(path);",
        "CREATE INDEX ON :File(repo_id);",
        "CREATE INDEX ON :Class(name);",
        "CREATE INDEX ON :Class(repo_id);",
        "CREATE INDEX ON :Variable(repo_id);",
        "CREATE INDEX ON :Module(import_path);",
    ]
    for idx in indexes:
        try:
            run_void(idx)
        except Exception as e:
            log.warning("index.create.failed", query=idx, error=str(e))
    log.info("graph.indexes.created")


def delete_file_subgraph(repo_id: str, file_path: str) -> None:
    """
    Delete all nodes owned by a file (File + its symbols + edges).
    Called before re-indexing a changed file.
    """
    run_void(
        """
        MATCH (f:File {repo_id: $repo_id, path: $path})
        OPTIONAL MATCH (f)-[:CONTAINS]->(s)
        DETACH DELETE f, s
        """,
        {"repo_id": repo_id, "path": file_path},
    )


def get_file_hash(repo_id: str, file_path: str) -> str | None:
    """Return stored content hash for a file, or None if not indexed."""
    results = run(
        "MATCH (f:File {repo_id: $repo_id, path: $path}) RETURN f.content_hash AS h",
        {"repo_id": repo_id, "path": file_path},
    )
    return results[0]["h"] if results else None


def cleanup_zombie_nodes(repo_id: str) -> int:
    """
    Delete symbol nodes that have no parent File node (orphans from crashed workers).
    Returns count of deleted nodes.
    """
    results = run(
        """
        MATCH (s)
        WHERE s:Function OR s:Class OR s:Variable
        AND s.repo_id = $repo_id
        AND NOT (:File)-[:CONTAINS]->(s)
        WITH s, s.id AS sid
        DETACH DELETE s
        RETURN count(*) AS deleted
        """,
        {"repo_id": repo_id},
    )
    deleted = results[0]["deleted"] if results else 0
    if deleted > 0:
        log.warning("zombies.cleaned", repo_id=repo_id, count=deleted)
    return deleted
