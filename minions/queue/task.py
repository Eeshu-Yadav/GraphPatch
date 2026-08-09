"""Task model — what gets submitted to the queue and tracked through execution."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ESCALATED = "escalated"


@dataclass
class Task:
    """A single minion task — the unit of work in the queue."""
    # Required fields
    repo_id: str
    ticket_id: str
    title: str
    body: str

    # Optional config
    task_type: str = "auto"             # auto | bug_fix | feature | migration | test_fix
    github_token: str = ""
    draft: bool = False
    source: str = "cli"                 # cli | slack | webhook | mcp
    requester: str = ""
    priority: int = 1                   # 0=highest, 2=lowest
    open_pr: bool = False               # True → run_minion_pr, False → run_minion

    # Auto-set
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: TaskStatus = TaskStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0

    # Results (filled after completion)
    pr_url: str = ""
    error: str = ""
    total_tokens: int = 0
    total_duration: float = 0.0
    nodes_executed: list[str] = field(default_factory=list)
    result_summary: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "repo_id": self.repo_id,
            "ticket_id": self.ticket_id,
            "title": self.title,
            "body": self.body,
            "task_type": self.task_type,
            "source": self.source,
            "requester": self.requester,
            "priority": self.priority,
            "open_pr": self.open_pr,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "pr_url": self.pr_url,
            "error": self.error,
            "total_tokens": self.total_tokens,
            "total_duration": self.total_duration,
            "nodes_executed": self.nodes_executed,
        }

    @staticmethod
    def from_dict(d: dict) -> Task:
        t = Task(
            repo_id=d["repo_id"],
            ticket_id=d["ticket_id"],
            title=d["title"],
            body=d["body"],
        )
        t.task_id = d.get("task_id", t.task_id)
        t.task_type = d.get("task_type", "auto")
        t.github_token = d.get("github_token", "")
        t.source = d.get("source", "cli")
        t.requester = d.get("requester", "")
        t.priority = d.get("priority", 1)
        t.open_pr = d.get("open_pr", False)
        t.draft = d.get("draft", False)
        t.status = TaskStatus(d.get("status", "queued"))
        t.created_at = d.get("created_at", t.created_at)
        t.started_at = d.get("started_at", 0.0)
        t.completed_at = d.get("completed_at", 0.0)
        t.pr_url = d.get("pr_url", "")
        t.error = d.get("error", "")
        t.total_tokens = d.get("total_tokens", 0)
        t.total_duration = d.get("total_duration", 0.0)
        t.nodes_executed = d.get("nodes_executed", [])
        return t

    @property
    def duration(self) -> float:
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        if self.started_at:
            return time.time() - self.started_at
        return 0.0

    def summary_line(self) -> str:
        status = self.status.value.upper()
        dur = f"{self.duration:.0f}s" if self.duration else "-"
        pr = f" → {self.pr_url}" if self.pr_url else ""
        err = f" | {self.error[:60]}" if self.error else ""
        return f"[{status:9s}] {self.task_id} | {self.ticket_id} | {self.title[:40]} | {dur}{pr}{err}"
