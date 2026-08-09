"""
Qdrant vector store — upsert and search for code entities.
Collection: code_entities
Vectors: 768d nomic-embed-text
Payload: repo_id, entity_type, file_path, language, name, qualified_name, summary, is_exported
"""
from __future__ import annotations

import structlog
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance, FieldCondition, Filter, MatchValue,
    PointStruct, ScalarQuantization, ScalarQuantizationConfig,
    ScalarType, VectorParams,
)

from src.config import settings
from src.models.symbol import FileSymbols, SymbolKind

log = structlog.get_logger(__name__)

_client: QdrantClient | None = None


def _get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = QdrantClient(url=settings.qdrant_url)
    return _client


def setup_collection() -> None:
    """Create the code_entities collection if it doesn't exist."""
    client = _get_client()
    existing = [c.name for c in client.get_collections().collections]

    if settings.qdrant_collection not in existing:
        client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dim,  # 768 for nomic-embed-text
                distance=Distance.COSINE,
            ),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    quantile=0.99,
                    always_ram=True,
                )
            ),
        )

        # Payload indexes for fast filtered search
        for field in ("repo_id", "language", "entity_type", "file_path", "is_exported"):
            client.create_payload_index(
                collection_name=settings.qdrant_collection,
                field_name=field,
                field_schema="keyword",
            )

        log.info("qdrant.collection.created", name=settings.qdrant_collection)


def upsert_file_entities(
    repo_id: str,
    fs: FileSymbols,
    embeddings: list[list[float]],
) -> None:
    """
    Upsert all symbols from a file into the vector collection.
    `embeddings` must be parallel to fs.symbols (same order and length).
    """
    if not embeddings or len(embeddings) != len(fs.symbols):
        log.warning("qdrant.skip", path=fs.path, reason="embedding count mismatch")
        return

    client = _get_client()
    points: list[PointStruct] = []

    for sym, vector in zip(fs.symbols, embeddings):
        # Build the text that was embedded (must match what was embedded)
        point_id = _stable_id(sym.id or f"{repo_id}:{fs.path}:{sym.name}")
        points.append(PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "repo_id": repo_id,
                "entity_type": sym.kind.value,
                "file_path": fs.path,
                "language": fs.language.value,
                "name": sym.name,
                "qualified_name": sym.qualified_name,
                "summary": sym.summary,
                "is_exported": sym.is_exported,
                "line_start": sym.line_start,
                "symbol_id": sym.id,
            },
        ))

    if points:
        client.upsert(collection_name=settings.qdrant_collection, points=points)
        log.debug("qdrant.upserted", path=fs.path, count=len(points))


def upsert_file_summary(
    repo_id: str,
    file_path: str,
    language: str,
    summary: str,
    vector: list[float],
) -> None:
    """Upsert the file-level embedding (summary of the whole file)."""
    client = _get_client()
    point_id = _stable_id(f"{repo_id}:{file_path}:__file__")
    client.upsert(
        collection_name=settings.qdrant_collection,
        points=[PointStruct(
            id=point_id,
            vector=vector,
            payload={
                "repo_id": repo_id,
                "entity_type": "File",
                "file_path": file_path,
                "language": language,
                "name": file_path,
                "qualified_name": file_path,
                "summary": summary,
                "is_exported": False,
            },
        )],
    )


def delete_file_entities(repo_id: str, file_path: str) -> None:
    """Remove all vector points for a file (called before re-indexing)."""
    client = _get_client()
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=Filter(must=[
            FieldCondition(key="repo_id", match=MatchValue(value=repo_id)),
            FieldCondition(key="file_path", match=MatchValue(value=file_path)),
        ]),
    )


def search(
    query_vector: list[float],
    repo_id: str,
    entity_types: list[str] | None = None,
    path_prefix: str = "",
    exported_only: bool = False,
    limit: int = 10,
    min_score: float = 0.35,
) -> list[dict]:
    """
    Semantic search across code entities.
    Filters: repo_id (required), entity_type, path prefix, exported only, min_score.
    """
    client = _get_client()

    must_conditions = [
        FieldCondition(key="repo_id", match=MatchValue(value=repo_id)),
    ]

    if exported_only:
        must_conditions.append(
            FieldCondition(key="is_exported", match=MatchValue(value=True))
        )

    # Push entity_type filter into Qdrant (it's an indexed keyword field)
    # so we don't lose results to the post-filter.
    if entity_types:
        if len(entity_types) == 1:
            must_conditions.append(
                FieldCondition(key="entity_type", match=MatchValue(value=entity_types[0]))
            )
        else:
            from qdrant_client.models import MatchAny
            must_conditions.append(
                FieldCondition(key="entity_type", match=MatchAny(any=entity_types))
            )

    search_filter = Filter(must=must_conditions)

    response = client.query_points(
        collection_name=settings.qdrant_collection,
        query=query_vector,
        query_filter=search_filter,
        limit=limit,
        with_payload=True,
    )

    # Post-filter by entity_type and path_prefix (Qdrant doesn't support STARTS WITH natively)
    output = []
    for hit in response.points:
        if hit.score < min_score:
            continue  # Skip low-confidence matches
        p = hit.payload or {}
        if entity_types and p.get("entity_type") not in entity_types:
            continue
        if path_prefix and not p.get("file_path", "").startswith(path_prefix):
            continue
        output.append({
            "score": hit.score,
            "entity_type": p.get("entity_type"),
            "name": p.get("name"),
            "qualified_name": p.get("qualified_name"),
            "file_path": p.get("file_path"),
            "language": p.get("language"),
            "summary": p.get("summary"),
            "line_start": p.get("line_start"),
            "symbol_id": p.get("symbol_id"),
        })

    return output


def _stable_id(text: str) -> int:
    """Convert a string to a stable integer ID for Qdrant points."""
    import hashlib
    return int(hashlib.md5(text.encode()).hexdigest()[:15], 16)
