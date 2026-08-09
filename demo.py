#!/usr/bin/env python3
"""
CLI demo client for the Ticket-to-PR MCP server.

Usage:
    python demo.py status
    python demo.py pipeline --repo owner/repo --id TICKET-1 --title "..." --body "..."
    python demo.py pr       --repo owner/repo --id TICKET-1 --title "..." --body "..."
    python demo.py retry    --repo owner/repo --pr 18
    python demo.py index    --repo-url https://github.com/owner/repo --repo-id owner/repo
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent

# Re-exec with venv python if not already using it
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
if sys.executable != VENV_PYTHON and Path(VENV_PYTHON).exists():
    os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)

import argparse
import asyncio
import json

PYTHON = str(ROOT / ".venv" / "bin" / "python")
PYTHONPATH = ":".join([
    str(ROOT / "layer2-indexer" / "src"),
    str(ROOT / "layer3-context"),
    str(ROOT / "layer4-planner"),
    str(ROOT / "layer45-agent"),
    str(ROOT / "layer6-validator"),
    str(ROOT / "layer7-pr-publisher"),
    str(ROOT / "mcp-server"),
])


def _load_env():
    """Load .env file, then build the subprocess environment."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    return {
        **os.environ,
        "PYTHONPATH": PYTHONPATH,
    }


async def call_tool(tool_name: str, arguments: dict, env: dict) -> str:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server = StdioServerParameters(
        command=PYTHON,
        args=["-m", "mcp_server.server"],
        cwd=str(ROOT / "mcp-server"),
        env=env,
    )

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            if result.content:
                return result.content[0].text
            return "(no output)"


def run(tool_name: str, arguments: dict, env: dict):
    print(f"\n► Calling MCP tool: {tool_name}")
    print(f"  Args: {json.dumps(arguments, indent=2)}\n")
    result = asyncio.run(call_tool(tool_name, arguments, env))
    print(result)


def main():
    env = _load_env()

    parser = argparse.ArgumentParser(description="Ticket-to-PR MCP Demo CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    sub.add_parser("status", help="Check all pipeline services")

    # pipeline (no PR)
    p = sub.add_parser("pipeline", help="Run L3→L4→L5→L6, print diff + validation")
    p.add_argument("--repo", required=True, help="owner/repo")
    p.add_argument("--id",   required=True, help="Ticket ID")
    p.add_argument("--title",required=True, help="Ticket title")
    p.add_argument("--body", required=True, help="Ticket description")

    # pr (full pipeline + GitHub PR)
    p2 = sub.add_parser("pr", help="Run full pipeline and open GitHub PR")
    p2.add_argument("--repo",  required=True)
    p2.add_argument("--id",    required=True)
    p2.add_argument("--title", required=True)
    p2.add_argument("--body",  required=True)
    p2.add_argument("--draft", action="store_true", default=False)

    # retry (push fix to existing PR based on review feedback)
    p_retry = sub.add_parser("retry", help="Re-run pipeline using PR feedback, push to same PR")
    p_retry.add_argument("--repo",  required=True)
    p_retry.add_argument("--pr",    required=True, type=int, help="PR number to retry from")
    p_retry.add_argument("--id",    default="", help="Override ticket ID")
    p_retry.add_argument("--title", default="", help="Override title")
    p_retry.add_argument("--body",  default="", help="Override body")

    # index
    p3 = sub.add_parser("index", help="Clone and index a new repo")
    p3.add_argument("--repo-url", required=True)
    p3.add_argument("--repo-id",  required=True)

    args = parser.parse_args()

    if args.cmd == "status":
        run("get_pipeline_status", {}, env)

    elif args.cmd == "pipeline":
        run("run_pipeline", {
            "repo_id":   args.repo,
            "ticket_id": args.id,
            "title":     args.title,
            "body":      args.body,
        }, env)

    elif args.cmd == "pr":
        run("run_pipeline_pr", {
            "repo_id":   args.repo,
            "ticket_id": args.id,
            "title":     args.title,
            "body":      args.body,
            "draft":     args.draft,
        }, env)

    elif args.cmd == "retry":
        run("retry_pipeline_pr", {
            "repo_id":   args.repo,
            "pr_number": args.pr,
            "ticket_id": args.id,
            "title":     args.title,
            "body":      args.body,
        }, env)

    elif args.cmd == "index":
        run("index_repo", {
            "repo_url": args.repo_url,
            "repo_id":  args.repo_id,
        }, env)


if __name__ == "__main__":
    main()
