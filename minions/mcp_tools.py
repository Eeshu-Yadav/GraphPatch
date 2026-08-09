"""MCP tool definitions for the minion blueprint engine.

These are NEW tools added alongside the existing run_pipeline / run_pipeline_pr.
Import and register them in the MCP server to expose them.
"""
from __future__ import annotations

import os
import traceback

import structlog

log = structlog.get_logger(__name__)


def run_minion(
    repo_id: str,
    ticket_id: str,
    title: str,
    body: str,
    task_type: str = "auto",
    github_token: str = "",
    fallback: bool = True,
) -> str:
    """Run the blueprint-based minion pipeline WITHOUT opening a PR.

    Returns diff + validation report. Falls back to legacy pipeline if blueprint fails.

    Args:
        repo_id:    GitHub repo in owner/repo format
        ticket_id:  Unique ticket identifier
        title:      Short description
        body:       Full ticket description
        task_type:  auto | bug_fix | feature | migration | test_fix
        github_token: GitHub PAT (optional, only needed for PR)
        fallback:   Fall back to legacy pipeline on failure (default: True)
    """
    try:
        from minions.engine.context import PipelineContext
        from minions.engine.registry import get_blueprint, classify_task
        from minions.engine.runner import BlueprintRunner

        if task_type == "auto":
            task_type = classify_task(title, body)

        ctx = PipelineContext(
            ticket_id=ticket_id,
            repo_id=repo_id,
            title=title,
            body=body,
            task_type=task_type,
            github_token=github_token or os.environ.get("GITHUB_TOKEN", ""),
            fallback_to_legacy=fallback,
        )

        # Remove create_pr, notify, escalate from blueprint (no PR mode)
        blueprint = get_blueprint(task_type)
        remove_nodes = {"create_pr", "notify", "escalate"}
        blueprint.nodes = [
            n for n in blueprint.nodes
            if n.name not in remove_nodes
        ]
        # Redirect all edges that point to removed nodes to None (end)
        for node_name, edges in blueprint.edges.items():
            for edge_type, target in list(edges.items()):
                if target in remove_nodes:
                    edges[edge_type] = None

        runner = BlueprintRunner(blueprint)
        ctx = runner.run(ctx)

        # Format output
        output = [ctx.summary()]
        if ctx.implementation and ctx.implementation.file_results:
            diff = ctx.implementation.to_diff_text()
            output.extend(["", "## Diff", "```diff", diff[:6000], "```"])

        return "\n".join(output)

    except Exception:
        return f"Minion pipeline failed:\n```\n{traceback.format_exc()}\n```"


def run_minion_pr(
    repo_id: str,
    ticket_id: str,
    title: str,
    body: str,
    task_type: str = "auto",
    github_token: str = "",
    draft: bool = False,
    fallback: bool = True,
) -> str:
    """Run the blueprint-based minion pipeline AND open a GitHub PR.

    Uses deterministic + agentic nodes for structured execution.
    Falls back to legacy pipeline if blueprint fails and fallback=True.

    Args:
        repo_id:      GitHub repo in owner/repo format
        ticket_id:    Unique ticket identifier
        title:        Short description
        body:         Full ticket description
        task_type:    auto | bug_fix | feature | migration | test_fix
        github_token: GitHub PAT (or set GITHUB_TOKEN env var)
        draft:        Open as draft PR (default: False)
        fallback:     Fall back to legacy pipeline on failure (default: True)
    """
    try:
        from minions.engine.context import PipelineContext
        from minions.engine.registry import get_blueprint, classify_task
        from minions.engine.runner import BlueprintRunner

        if task_type == "auto":
            task_type = classify_task(title, body)

        ctx = PipelineContext(
            ticket_id=ticket_id,
            repo_id=repo_id,
            title=title,
            body=body,
            task_type=task_type,
            github_token=github_token or os.environ.get("GITHUB_TOKEN", ""),
            draft_pr=draft,
            fallback_to_legacy=fallback,
        )

        blueprint = get_blueprint(task_type)
        runner = BlueprintRunner(blueprint)
        ctx = runner.run(ctx)

        return ctx.summary()

    except Exception:
        return f"Minion pipeline failed:\n```\n{traceback.format_exc()}\n```"
