"""Worker — pulls tasks from queue and executes them through blueprint engine.

Can run multiple workers for parallel execution.
Each worker has its own ID and claims tasks atomically.
"""
from __future__ import annotations

import os
import signal
import time
import uuid
import threading

import structlog

from minions.queue.task import Task, TaskStatus
from minions.queue.store import TaskStore

log = structlog.get_logger(__name__)


class Worker:
    """Single worker process that pulls and executes tasks."""

    def __init__(self, store: TaskStore, worker_id: str | None = None):
        self.store = store
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex[:6]}"
        self._running = False
        self._current_task: Task | None = None

    def run_forever(self, poll_interval: float = 2.0):
        """Main loop: pull tasks and execute them until stopped."""
        self._running = True
        log.info("worker.started", worker_id=self.worker_id, poll=f"{poll_interval}s")

        while self._running:
            task = self.store.claim_next(self.worker_id)
            if task:
                self._execute_task(task)
            else:
                time.sleep(poll_interval)

        log.info("worker.stopped", worker_id=self.worker_id)

    def run_once(self) -> Task | None:
        """Pull and execute a single task. Returns the completed task or None."""
        task = self.store.claim_next(self.worker_id)
        if task:
            self._execute_task(task)
            return task
        return None

    def stop(self):
        """Signal the worker to stop after current task completes."""
        self._running = False
        log.info("worker.stopping", worker_id=self.worker_id)

    def _execute_task(self, task: Task):
        """Execute a single task through the blueprint engine."""
        self._current_task = task
        log.info("worker.task_start", worker=self.worker_id,
                 task_id=task.task_id, ticket=task.ticket_id,
                 repo=task.repo_id, type=task.task_type)

        try:
            from minions.engine.context import PipelineContext
            from minions.engine.registry import get_blueprint, classify_task
            from minions.engine.runner import BlueprintRunner

            # Auto-classify if needed
            if task.task_type == "auto":
                task.task_type = classify_task(task.title, task.body)

            ctx = PipelineContext(
                ticket_id=task.ticket_id,
                repo_id=task.repo_id,
                title=task.title,
                body=task.body,
                task_type=task.task_type,
                github_token=task.github_token or os.environ.get("GITHUB_TOKEN", ""),
                draft_pr=task.draft,
                fallback_to_legacy=True,
            )

            blueprint = get_blueprint(task.task_type)

            # If no PR requested, strip PR nodes
            if not task.open_pr:
                remove_nodes = {"create_pr", "notify", "escalate"}
                blueprint.nodes = [n for n in blueprint.nodes if n.name not in remove_nodes]
                for node_name, edges in blueprint.edges.items():
                    for edge_type, target in list(edges.items()):
                        if target in remove_nodes:
                            edges[edge_type] = None

            runner = BlueprintRunner(blueprint)
            ctx = runner.run(ctx)

            # Update task with results
            task.total_tokens = ctx.total_tokens
            task.total_duration = ctx.total_duration
            task.nodes_executed = ctx.nodes_executed
            task.pr_url = ctx.pr_url
            task.result_summary = ctx.summary()

            if ctx.success:
                task.status = TaskStatus.SUCCESS
            elif ctx.escalated:
                task.status = TaskStatus.ESCALATED
                task.error = ctx.error
            else:
                task.status = TaskStatus.FAILED
                task.error = ctx.error

        except Exception as e:
            task.status = TaskStatus.FAILED
            task.error = str(e)
            log.error("worker.task_error", task_id=task.task_id, error=str(e))

        task.completed_at = time.time()
        self.store.update(task)

        log.info("worker.task_done",
                 worker=self.worker_id,
                 task_id=task.task_id,
                 status=task.status.value,
                 tokens=task.total_tokens,
                 duration=f"{task.duration:.1f}s",
                 pr=task.pr_url or "(none)")

        self._current_task = None


def run_workers(store: TaskStore, num_workers: int = 2, poll_interval: float = 2.0):
    """Run multiple workers in parallel threads.

    Each worker pulls tasks independently from the queue.
    Atomic claim_next() prevents double-execution.
    """
    workers: list[Worker] = []
    threads: list[threading.Thread] = []

    def _shutdown(signum, frame):
        log.info("workers.shutdown_signal", signal=signum)
        for w in workers:
            w.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    for i in range(num_workers):
        w = Worker(store, worker_id=f"worker-{i}")
        workers.append(w)
        t = threading.Thread(target=w.run_forever, args=(poll_interval,), daemon=True)
        threads.append(t)
        t.start()

    log.info("workers.started", count=num_workers, poll=f"{poll_interval}s")

    # Wait for all threads (Ctrl+C triggers _shutdown which stops workers)
    try:
        for t in threads:
            while t.is_alive():
                t.join(timeout=1.0)
    except KeyboardInterrupt:
        for w in workers:
            w.stop()
        for t in threads:
            t.join(timeout=5.0)

    log.info("workers.all_stopped")
