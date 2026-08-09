"""
Health check endpoint — used by load balancers and monitoring.
Reports status of all dependencies: Memgraph, Qdrant, Redis, Ollama.
"""
from __future__ import annotations

import time

import httpx
import redis as redis_lib
import structlog
from fastapi import APIRouter

from src.config import settings

router = APIRouter(prefix="/health")
log = structlog.get_logger(__name__)


def _check_memgraph() -> dict:
    try:
        from src.graph.client import run
        run("RETURN 1 AS ok")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_qdrant() -> dict:
    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.get(f"{settings.qdrant_url}/healthz")
            return {"status": "ok" if r.status_code == 200 else "degraded"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_redis() -> dict:
    try:
        r = redis_lib.from_url(settings.redis_url)
        r.ping()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_ollama() -> dict:
    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.get(f"{settings.ollama_url}/api/tags")
            models = [m["name"] for m in r.json().get("models", [])]
            has_model = any(settings.embedding_model in m for m in models)
            return {"status": "ok" if has_model else "degraded", "model_loaded": has_model}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("")
def health_check() -> dict:
    checks = {
        "memgraph": _check_memgraph(),
        "qdrant": _check_qdrant(),
        "redis": _check_redis(),
        "ollama": _check_ollama(),
    }
    all_ok = all(c["status"] == "ok" for c in checks.values())
    return {
        "status": "ok" if all_ok else "degraded",
        "checks": checks,
        "timestamp": int(time.time()),
    }
