"""Task store — Redis-backed task queue with SQLite fallback.

Uses Redis if available (for multi-process workers), falls back to
SQLite for single-process local dev. Same interface either way.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import threading
from pathlib import Path

import structlog

from minions.queue.task import Task, TaskStatus

log = structlog.get_logger(__name__)


class TaskStore:
    """Abstract task store interface."""

    def submit(self, task: Task) -> str:
        raise NotImplementedError

    def claim_next(self, worker_id: str) -> Task | None:
        raise NotImplementedError

    def update(self, task: Task) -> None:
        raise NotImplementedError

    def get(self, task_id: str) -> Task | None:
        raise NotImplementedError

    def list_all(self, limit: int = 50) -> list[Task]:
        raise NotImplementedError

    def count_by_status(self) -> dict[str, int]:
        raise NotImplementedError


# ── Redis Store ───────────────────────────────────────────────────────────────

class RedisTaskStore(TaskStore):
    """Redis-backed store using sorted sets for priority queue."""

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        import redis
        self.r = redis.from_url(redis_url, decode_responses=True)
        self._queue_key = "minion:queue"
        self._tasks_key = "minion:tasks"
        self._lock = threading.Lock()

    def submit(self, task: Task) -> str:
        data = json.dumps(task.to_dict())
        self.r.hset(self._tasks_key, task.task_id, data)
        # Priority queue: lower score = higher priority, tie-break by timestamp
        score = task.priority * 1e12 + task.created_at
        self.r.zadd(self._queue_key, {task.task_id: score})
        log.info("store.submit", task_id=task.task_id, priority=task.priority)
        return task.task_id

    def claim_next(self, worker_id: str) -> Task | None:
        with self._lock:
            # Pop lowest-score item (highest priority, oldest)
            items = self.r.zpopmin(self._queue_key, count=1)
            if not items:
                return None
            task_id = items[0][0]
            data = self.r.hget(self._tasks_key, task_id)
            if not data:
                return None
            task = Task.from_dict(json.loads(data))
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()
            self.r.hset(self._tasks_key, task_id, json.dumps(task.to_dict()))
            log.info("store.claimed", task_id=task_id, worker=worker_id)
            return task

    def update(self, task: Task) -> None:
        self.r.hset(self._tasks_key, task.task_id, json.dumps(task.to_dict()))

    def get(self, task_id: str) -> Task | None:
        data = self.r.hget(self._tasks_key, task_id)
        if not data:
            return None
        return Task.from_dict(json.loads(data))

    def list_all(self, limit: int = 50) -> list[Task]:
        all_data = self.r.hgetall(self._tasks_key)
        tasks = [Task.from_dict(json.loads(v)) for v in all_data.values()]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]

    def count_by_status(self) -> dict[str, int]:
        counts = {s.value: 0 for s in TaskStatus}
        for data in self.r.hgetall(self._tasks_key).values():
            t = json.loads(data)
            status = t.get("status", "queued")
            counts[status] = counts.get(status, 0) + 1
        return counts


# ── SQLite Store (fallback) ───────────────────────────────────────────────────

class SQLiteTaskStore(TaskStore):
    """SQLite-backed store for local dev when Redis is not available."""

    def __init__(self, db_path: str | None = None):
        if db_path is None:
            db_path = str(Path(os.environ.get(
                "MINION_DB_PATH",
                str(Path.home() / "Desktop" / "context" / "minions" / "tasks.db")
            )))
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    priority INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_priority ON tasks(priority, created_at)")

    def submit(self, task: Task) -> str:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks (task_id, data, status, priority, created_at) VALUES (?, ?, ?, ?, ?)",
                (task.task_id, json.dumps(task.to_dict()), task.status.value, task.priority, task.created_at),
            )
        log.info("store.submit", task_id=task.task_id, priority=task.priority, backend="sqlite")
        return task.task_id

    def claim_next(self, worker_id: str) -> Task | None:
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT task_id, data FROM tasks WHERE status = 'queued' ORDER BY priority ASC, created_at ASC LIMIT 1"
                ).fetchone()
                if not row:
                    return None
                task_id, data = row
                task = Task.from_dict(json.loads(data))
                task.status = TaskStatus.RUNNING
                task.started_at = time.time()
                conn.execute(
                    "UPDATE tasks SET data = ?, status = 'running' WHERE task_id = ?",
                    (json.dumps(task.to_dict()), task_id),
                )
                log.info("store.claimed", task_id=task_id, worker=worker_id, backend="sqlite")
                return task

    def update(self, task: Task) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET data = ?, status = ? WHERE task_id = ?",
                (json.dumps(task.to_dict()), task.status.value, task.task_id),
            )

    def get(self, task_id: str) -> Task | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT data FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return Task.from_dict(json.loads(row[0]))

    def list_all(self, limit: int = 50) -> list[Task]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT data FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [Task.from_dict(json.loads(r[0])) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        counts = {s.value: 0 for s in TaskStatus}
        with sqlite3.connect(self.db_path) as conn:
            for row in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall():
                counts[row[0]] = row[1]
        return counts


# ── Factory ───────────────────────────────────────────────────────────────────

def create_store() -> TaskStore:
    """Create the best available task store — Redis if available, SQLite fallback."""
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    try:
        import redis
        r = redis.from_url(redis_url, decode_responses=True)
        r.ping()
        log.info("store.backend", type="redis", url=redis_url)
        return RedisTaskStore(redis_url)
    except Exception:
        log.info("store.backend", type="sqlite", reason="redis unavailable")
        return SQLiteTaskStore()
