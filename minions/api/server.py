"""FastAPI server for the Minions dashboard.

Wraps the existing task store (Redis/SQLite) with REST endpoints.
Run: uvicorn minions.api.server:app --port 8111 --reload
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Ensure minions package is importable
_root = Path(__file__).parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_env_path = _root / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(_env_path))
    except ImportError:
        pass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from minions.queue.task import Task, TaskStatus
from minions.queue.store import create_store

app = FastAPI(title="Minions Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_store = None


def get_store():
    global _store
    if _store is None:
        _store = create_store()
    return _store


# ── Models ────────────────────────────────────────────────────────────────────


class SubmitRequest(BaseModel):
    repo_id: str
    ticket_id: str
    title: str
    body: str = ""
    task_type: str = "auto"
    priority: int = 1
    open_pr: bool = False
    draft: bool = False


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/api/stats")
def get_stats():
    """Aggregate stats for overview page."""
    store = get_store()
    tasks = store.list_all(limit=500)
    counts = store.count_by_status()

    total = len(tasks)
    completed = [t for t in tasks if t.status == TaskStatus.SUCCESS]
    failed = [t for t in tasks if t.status == TaskStatus.FAILED]
    escalated = [t for t in tasks if t.status == TaskStatus.ESCALATED]

    success_rate = (len(completed) / total * 100) if total > 0 else 0
    avg_tokens = (
        sum(t.total_tokens for t in completed) / len(completed)
        if completed else 0
    )
    avg_duration = (
        sum(t.duration for t in completed) / len(completed)
        if completed else 0
    )

    # Token hotspots — aggregate tokens_by_node across all tasks
    node_tokens: dict[str, int] = {}
    node_counts: dict[str, int] = {}
    for t in tasks:
        d = t.to_dict()
        nodes_exec = d.get("nodes_executed", [])
        # tokens_by_node is not in to_dict — we store it in result_summary or reconstruct
        # For now, track which nodes appear most in executed lists
        for node in nodes_exec:
            node_counts[node] = node_counts.get(node, 0) + 1

    # Bottleneck: tasks that failed or escalated — which node was last?
    bottlenecks: dict[str, int] = {}
    for t in failed + escalated:
        d = t.to_dict()
        nodes = d.get("nodes_executed", [])
        if nodes:
            last_node = nodes[-1]
            bottlenecks[last_node] = bottlenecks.get(last_node, 0) + 1

    return {
        "total": total,
        "counts": counts,
        "success_rate": round(success_rate, 1),
        "avg_tokens": int(avg_tokens),
        "avg_duration": round(avg_duration, 1),
        "node_frequency": dict(sorted(node_counts.items(), key=lambda x: -x[1])[:10]),
        "bottlenecks": dict(sorted(bottlenecks.items(), key=lambda x: -x[1])[:10]),
    }


@app.get("/api/tasks")
def list_tasks(status: str | None = None, limit: int = 50):
    """List tasks, optionally filtered by status."""
    store = get_store()
    tasks = store.list_all(limit=limit)

    if status and status != "all":
        tasks = [t for t in tasks if t.status.value == status]

    return [t.to_dict() for t in tasks]


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str):
    """Get single task detail."""
    store = get_store()
    task = store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task.to_dict()


@app.post("/api/tasks/submit")
def submit_task(req: SubmitRequest):
    """Submit a new task to the queue."""
    store = get_store()
    task = Task(
        repo_id=req.repo_id,
        ticket_id=req.ticket_id,
        title=req.title,
        body=req.body,
        task_type=req.task_type,
        priority=req.priority,
        open_pr=req.open_pr,
        draft=req.draft,
        source="dashboard",
        requester=os.environ.get("USER", "dashboard"),
        github_token=os.environ.get("GITHUB_TOKEN", ""),
    )
    task_id = store.submit(task)
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/health")
def health():
    store = get_store()
    store_type = type(store).__name__
    return {
        "status": "ok",
        "store": store_type,
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "github_token": bool(os.environ.get("GITHUB_TOKEN")),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("minions.api.server:app", host="0.0.0.0", port=8111, reload=True)
