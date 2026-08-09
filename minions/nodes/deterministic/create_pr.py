"""[D] create_pr — Git commit/push + GitHub PR via L7. Zero LLM tokens."""
from __future__ import annotations

import os
import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def execute(ctx: PipelineContext) -> NodeResult:
    if not ctx.implementation or not ctx.implementation.file_results:
        ctx.error = "No files to publish"
        return NodeResult(success=False, error="No files to publish")

    from layer7_publisher.publisher import publish

    token = ctx.github_token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        ctx.error = "GITHUB_TOKEN not set"
        return NodeResult(success=False, error="GITHUB_TOKEN not set")

    # Build validation summary
    val_parts = []
    if ctx.test_counts:
        val_parts.append(
            f"Tests: {ctx.test_counts.get('passed', 0)} passed, "
            f"{ctx.test_counts.get('failed', 0)} failed"
        )
    if ctx.build_output:
        val_parts.append(f"Build: {'passed' if ctx.build_passed else 'check output'}")
    if ctx.review_feedback:
        val_parts.append(f"Review: {'APPROVED' if ctx.review_approved else 'PARTIAL'}")
    val_parts.append(f"\nMinion: {ctx.total_tokens:,} tokens, {ctx.total_duration:.0f}s")

    validation_summary = "\n".join(val_parts)

    pr_result = publish(
        impl=ctx.implementation,
        ticket_title=ctx.title,
        github_token=token,
        validation_summary=validation_summary,
        draft=ctx.draft_pr,
    )

    if pr_result.success:
        ctx.pr_url = pr_result.pr_url or ""
        ctx.pr_number = pr_result.pr_number or 0
        log.info("pr.created", url=ctx.pr_url, draft=ctx.draft_pr)
    else:
        ctx.error = pr_result.error or "PR creation failed"
        log.error("pr.failed", error=ctx.error)

    return NodeResult(success=pr_result.success, tokens_used=0)
