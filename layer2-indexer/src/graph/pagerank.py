"""
PageRank computation via Memgraph MAGE.
Stores centrality scores back onto File and Function nodes.
Run after full index, or after large batch of incremental updates.
"""
from __future__ import annotations

import structlog

from src.graph import client as g

log = structlog.get_logger(__name__)


def run_pagerank(repo_id: str) -> None:
    """
    Run PageRank on the CALLS + IMPORTS subgraph for a repo.
    Updates centrality property on File and Function nodes.
    Uses Memgraph MAGE pagerank procedure (built-in).
    """
    log.info("pagerank.starting", repo_id=repo_id)

    # Run MAGE PageRank — it operates on the whole graph
    # We store results per-node keyed by repo_id to avoid cross-repo contamination
    results = g.run(
        """
        CALL pagerank.get()
        YIELD node, rank
        WITH node, rank
        WHERE (node:File OR node:Function) AND node.repo_id = $repo_id
        SET node.centrality = rank
        RETURN count(*) AS updated
        """,
        {"repo_id": repo_id},
    )

    updated = results[0]["updated"] if results else 0
    log.info("pagerank.done", repo_id=repo_id, nodes_updated=updated)
