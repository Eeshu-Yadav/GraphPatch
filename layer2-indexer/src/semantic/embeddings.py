"""
Embedding generation using nomic-embed-text via Ollama (self-hosted, free).
Batches requests for efficiency. 768-dimensional vectors.
"""
from __future__ import annotations

import structlog
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings

log = structlog.get_logger(__name__)

_BATCH_SIZE = 32  # Ollama handles smaller batches more reliably than cloud APIs


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
def embed_batch(texts: list[str]) -> list[list[float]]:
    """
    Embed a batch of texts. Returns list of 768-dim float vectors.
    Uses Ollama's /api/embed endpoint (batch support added in Ollama 0.1.31+).
    """
    if not texts:
        return []

    with httpx.Client(timeout=60.0) as client:
        response = client.post(
            f"{settings.ollama_url}/api/embed",
            json={"model": settings.embedding_model, "input": texts},
        )
        response.raise_for_status()
        data = response.json()
        return data["embeddings"]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """
    Embed texts in batches of BATCH_SIZE.
    Returns embeddings in the same order as input.
    """
    if not texts:
        return []

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), _BATCH_SIZE):
        batch = texts[i:i + _BATCH_SIZE]
        embeddings = embed_batch(batch)
        all_embeddings.extend(embeddings)
        log.debug("embeddings.batch", start=i, count=len(batch))

    return all_embeddings


def embed_single(text: str) -> list[float]:
    """Embed a single query string (for search)."""
    results = embed_batch([text])
    return results[0] if results else []
