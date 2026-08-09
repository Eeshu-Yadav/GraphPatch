"""
FastAPI application entry point.
Mounts all routers and runs startup setup.
"""
import structlog
from fastapi import FastAPI

from src.api import health, index, query, webhooks

log = structlog.get_logger(__name__)

app = FastAPI(
    title="Codebase Indexer — Layer 2",
    description="Parses repos into a knowledge graph + vector index for downstream agents.",
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(webhooks.router)
app.include_router(query.router)
app.include_router(index.router)


@app.on_event("startup")
async def startup():
    from src.graph.client import setup_indexes
    from src.semantic.vector_store import setup_collection
    try:
        setup_indexes()
        setup_collection()
        log.info("startup.complete")
    except Exception as e:
        log.warning("startup.partial", error=str(e))
