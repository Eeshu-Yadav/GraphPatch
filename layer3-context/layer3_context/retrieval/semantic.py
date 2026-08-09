"""
Semantic retrieval — embed ticket text and search Qdrant for similar code symbols.
"""
from __future__ import annotations

import structlog
from src.semantic import embeddings, vector_store

log = structlog.get_logger(__name__)


def search(full_text: str, repo_id: str, k: int = 20) -> list[dict]:
    """
    Embed ticket text and search Qdrant for similar code symbols.
    Returns list of raw search result dicts from vector_store.search().
    """
    vector = embeddings.embed_single(full_text)
    if not vector:
        log.warning("semantic.embed.failed")
        return []

    results = vector_store.search(
        query_vector=vector,
        repo_id=repo_id,
        entity_types=["Function", "Class"],
        limit=k,
    )
    log.debug("semantic.results", count=len(results))
    return results
