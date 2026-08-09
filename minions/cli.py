"""CLI entry point for the minion blueprint engine.

Usage:
    python -m minions.cli run     --repo owner/repo --id TICKET-1 --title "Fix bug" --body "..."
    python -m minions.cli pr      --repo owner/repo --id TICKET-1 --title "Fix bug" --body "..."
    python -m minions.cli submit  --repo owner/repo --id TICKET-1 --title "Fix bug" --body "..."
    python -m minions.cli worker  [--workers 2] [--poll 2]
    python -m minions.cli tasks
    python -m minions.cli status
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

_env_path = _root / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=str(_env_path))
    except ImportError:
        pass


def cmd_run(args):
    """Run pipeline without PR (blocking, direct execution)."""
    from minions.mcp_tools import run_minion
    result = run_minion(
        repo_id=args.repo,
        ticket_id=args.id,
        title=args.title,
        body=args.body,
        task_type=args.type,
        fallback=not args.no_fallback,
    )
    print(result)


def cmd_pr(args):
    """Run pipeline and open PR (blocking, direct execution)."""
    from minions.mcp_tools import run_minion_pr
    result = run_minion_pr(
        repo_id=args.repo,
        ticket_id=args.id,
        title=args.title,
        body=args.body,
        task_type=args.type,
        github_token=args.token or os.environ.get("GITHUB_TOKEN", ""),
        draft=args.draft,
        fallback=not args.no_fallback,
    )
    print(result)


def cmd_submit(args):
    """Submit a task to the queue (non-blocking, picked up by worker)."""
    from minions.queue.task import Task
    from minions.queue.store import create_store

    task = Task(
        repo_id=args.repo,
        ticket_id=args.id,
        title=args.title,
        body=args.body,
        task_type=args.type,
        source="cli",
        requester=os.environ.get("USER", "unknown"),
        priority=args.priority,
        open_pr=args.pr,
        github_token=args.token or os.environ.get("GITHUB_TOKEN", ""),
        draft=args.draft,
    )

    store = create_store()
    task_id = store.submit(task)
    print(f"Task submitted: {task_id}")
    print(f"  Ticket:   {task.ticket_id}")
    print(f"  Repo:     {task.repo_id}")
    print(f"  Type:     {task.task_type}")
    print(f"  Priority: {task.priority}")
    print(f"  PR:       {'yes' if task.open_pr else 'no'}")
    print(f"\nStart a worker to process it: python -m minions.cli worker")


def cmd_worker(args):
    """Start worker(s) that pull from queue and execute tasks."""
    from minions.queue.store import create_store
    from minions.queue.worker import run_workers, Worker

    store = create_store()

    if args.workers == 1:
        # Single worker — simpler, no threading
        worker = Worker(store)
        print(f"Worker {worker.worker_id} running (poll={args.poll}s)...")
        print("Press Ctrl+C to stop\n")
        worker.run_forever(poll_interval=args.poll)
    else:
        print(f"Starting {args.workers} workers (poll={args.poll}s)...")
        print("Press Ctrl+C to stop\n")
        run_workers(store, num_workers=args.workers, poll_interval=args.poll)


def cmd_tasks(args):
    """List all tasks in the queue."""
    from minions.queue.store import create_store

    store = create_store()
    tasks = store.list_all(limit=args.limit)
    counts = store.count_by_status()

    print(f"## Task Queue — {sum(counts.values())} total")
    print(f"  queued={counts.get('queued',0)} running={counts.get('running',0)} "
          f"success={counts.get('success',0)} failed={counts.get('failed',0)} "
          f"escalated={counts.get('escalated',0)}")
    print()

    if not tasks:
        print("No tasks.")
        return

    for task in tasks:
        print(task.summary_line())


def cmd_status(args):
    """Check pipeline services status."""
    from minions.engine.registry import _BLUEPRINTS
    from minions.queue.store import create_store

    print("## Minion Blueprint Engine")
    print(f"Blueprints: {', '.join(_BLUEPRINTS.keys())}")
    print(f"ANTHROPIC_API_KEY: {'set' if os.environ.get('ANTHROPIC_API_KEY') else 'NOT SET'}")
    print(f"GITHUB_TOKEN: {'set' if os.environ.get('GITHUB_TOKEN') else 'NOT SET'}")

    # Check queue backend
    store = create_store()
    store_type = type(store).__name__
    counts = store.count_by_status()
    print(f"\nQueue backend: {store_type}")
    print(f"Tasks: queued={counts.get('queued',0)} running={counts.get('running',0)} "
          f"success={counts.get('success',0)} failed={counts.get('failed',0)}")


def main():
    parser = argparse.ArgumentParser(
        description="Minion Blueprint Engine — structured ticket-to-PR pipeline"
    )
    sub = parser.add_subparsers(dest="command")

    # ── run (blocking, no PR) ──
    run_p = sub.add_parser("run", help="Run pipeline without PR (blocking)")
    run_p.add_argument("--repo", required=True, help="owner/repo")
    run_p.add_argument("--id", required=True, help="Ticket ID")
    run_p.add_argument("--title", required=True, help="Ticket title")
    run_p.add_argument("--body", default="", help="Ticket body")
    run_p.add_argument("--type", default="auto",
                       choices=["auto", "bug_fix", "feature", "migration", "test_fix"])
    run_p.add_argument("--no-fallback", action="store_true")

    # ── pr (blocking, with PR) ──
    pr_p = sub.add_parser("pr", help="Run pipeline and open PR (blocking)")
    pr_p.add_argument("--repo", required=True, help="owner/repo")
    pr_p.add_argument("--id", required=True, help="Ticket ID")
    pr_p.add_argument("--title", required=True, help="Ticket title")
    pr_p.add_argument("--body", default="", help="Ticket body")
    pr_p.add_argument("--type", default="auto",
                       choices=["auto", "bug_fix", "feature", "migration", "test_fix"])
    pr_p.add_argument("--token", default="", help="GitHub token")
    pr_p.add_argument("--draft", action="store_true")
    pr_p.add_argument("--no-fallback", action="store_true")

    # ── submit (non-blocking, queued) ──
    submit_p = sub.add_parser("submit", help="Submit task to queue (non-blocking)")
    submit_p.add_argument("--repo", required=True, help="owner/repo")
    submit_p.add_argument("--id", required=True, help="Ticket ID")
    submit_p.add_argument("--title", required=True, help="Ticket title")
    submit_p.add_argument("--body", default="", help="Ticket body")
    submit_p.add_argument("--type", default="auto",
                          choices=["auto", "bug_fix", "feature", "migration", "test_fix"])
    submit_p.add_argument("--token", default="", help="GitHub token")
    submit_p.add_argument("--priority", type=int, default=1, choices=[0, 1, 2],
                          help="0=highest, 2=lowest")
    submit_p.add_argument("--pr", action="store_true", help="Open PR when done")
    submit_p.add_argument("--draft", action="store_true")

    # ── worker ──
    worker_p = sub.add_parser("worker", help="Start worker(s) to process queue")
    worker_p.add_argument("--workers", type=int, default=1, help="Number of parallel workers")
    worker_p.add_argument("--poll", type=float, default=2.0, help="Poll interval in seconds")

    # ── tasks ──
    tasks_p = sub.add_parser("tasks", help="List tasks in queue")
    tasks_p.add_argument("--limit", type=int, default=20)

    # ── status ──
    sub.add_parser("status", help="Check engine status")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "pr":
        cmd_pr(args)
    elif args.command == "submit":
        cmd_submit(args)
    elif args.command == "worker":
        cmd_worker(args)
    elif args.command == "tasks":
        cmd_tasks(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
