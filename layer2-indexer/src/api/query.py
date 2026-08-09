"""
Query API — used by downstream agents (Layer 3+) via REST.
All endpoints require repo_id. Return JSON.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.graph import queries as graph_q
from src.semantic import embeddings, vector_store

router = APIRouter(prefix="/query")


# ── Request / Response models ─────────────────────────────────────────────────

class SearchRequest(BaseModel):
    query: str
    repo_id: str
    entity_types: list[str] | None = None   # ["Function", "Class", "File"]
    path_prefix: str = ""
    exported_only: bool = False
    limit: int = 10


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/impact")
def get_impact(
    repo_id: str = Query(...),
    symbol: str = Query(...),
    depth: int = Query(default=3, ge=1, le=5),
) -> dict:
    """
    What breaks if `symbol` changes?
    Returns callers grouped by will_break (static) and may_break (dynamic dispatch).
    """
    return graph_q.get_impact(repo_id, symbol, depth)


@router.post("/search")
def semantic_search(req: SearchRequest) -> list[dict]:
    """
    Natural language search over indexed code.
    'What handles payment processing?' → ranked list of matching symbols/files.
    """
    vector = embeddings.embed_single(req.query)
    if not vector:
        raise HTTPException(status_code=503, detail="Embedding service unavailable")

    return vector_store.search(
        query_vector=vector,
        repo_id=req.repo_id,
        entity_types=req.entity_types,
        path_prefix=req.path_prefix,
        exported_only=req.exported_only,
        limit=req.limit,
    )


@router.get("/callers")
def get_callers(
    repo_id: str = Query(...),
    symbol: str = Query(...),
) -> list[dict]:
    return graph_q.get_callers(repo_id, symbol)


@router.get("/file")
def get_file(
    repo_id: str = Query(...),
    path: str = Query(...),
) -> dict:
    summary = graph_q.get_file_summary(repo_id, path)
    if not summary:
        raise HTTPException(status_code=404, detail="File not found in index")
    return vars(summary)


@router.get("/dependencies")
def get_dependencies(
    repo_id: str = Query(...),
    path: str = Query(...),
) -> dict:
    return graph_q.get_file_dependencies(repo_id, path)


@router.get("/tests")
def get_tests(
    repo_id: str = Query(...),
    path: str = Query(...),
) -> list[str]:
    return graph_q.get_test_files(repo_id, path)


@router.get("/coupling")
def get_coupling(
    repo_id: str = Query(...),
    path: str = Query(...),
    min_score: float = Query(default=0.1, ge=0.0, le=1.0),
) -> list[dict]:
    return graph_q.get_git_coupling(repo_id, path, min_score)


@router.get("/top-files")
def get_top_files(
    repo_id: str = Query(...),
    prefix: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[dict]:
    return graph_q.get_top_files(repo_id, prefix, limit)
