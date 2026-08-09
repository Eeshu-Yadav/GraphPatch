"""
CLI for Layer 7 — PR Publisher.

Usage:
    python -m layer7_publisher.cli publish \
        --repo realpython/codetiming \
        --id TICKET-1 \
        --title "Timer.stop() crashes when called twice" \
        --body "Calling stop() twice raises TimerError..." \
        [--token ghp_xxx] \
        [--base main] \
        [--draft]
"""
from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

import structlog
structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(colors=True),
    ]
)


def cmd_publish(args: argparse.Namespace) -> None:
    # Run full pipeline: L3 → L4.5 agent → L6 → L7
    from layer3_context.models.ticket import Ticket
    from layer3_context.assembly.assembler import assemble
    from layer45_agent.agent import run_agent
    from layer45_agent.models import AgentConfig
    from layer6_validator.runner import validate
    from layer7_publisher.publisher import publish

    ticket = Ticket(
        ticket_id=args.id,
        title=args.title,
        body=args.body,
        repo_id=args.repo,
    )

    print(f"\n[L3] Assembling context for {args.repo}...")
    bundle = assemble(ticket)

    print("[L4.5] Running agent...")
    config = AgentConfig()
    agent_result = run_agent(ticket, bundle, config)
    impl = agent_result.implementation
    print(f"     → {len(impl.file_results)} file(s) modified, {agent_result.iterations} iterations")

    print("[L6] Validating...")
    validation = validate(impl)
    print(f"     → {validation.summary()}")

    if validation.overall.value == "failed" and not args.force:
        print("\nValidation FAILED. Use --force to publish anyway.")
        sys.exit(1)

    print("[L7] Publishing PR...")
    result = publish(
        impl=impl,
        ticket_title=args.title,
        github_token=args.token,
        base_branch=args.base or None,
        validation_summary=validation.summary(),
        draft=args.draft,
    )

    print(f"\n{result.summary()}")
    if not result.success:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Layer 7 — PR Publisher")
    sub = parser.add_subparsers(dest="cmd")

    pub = sub.add_parser("publish", help="Run full pipeline and open PR")
    pub.add_argument("--repo", required=True, help="owner/repo (e.g. realpython/codetiming)")
    pub.add_argument("--id", required=True, help="Ticket ID (e.g. TICKET-1)")
    pub.add_argument("--title", required=True, help="Ticket title")
    pub.add_argument("--body", required=True, help="Ticket description")
    pub.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""), help="GitHub PAT")
    pub.add_argument("--base", default="", help="Base branch (default: auto-detect from GitHub)")
    pub.add_argument("--draft", action="store_true", help="Open as draft PR")
    pub.add_argument("--force", action="store_true", help="Publish even if validation fails")

    args = parser.parse_args()
    if args.cmd == "publish":
        cmd_publish(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
