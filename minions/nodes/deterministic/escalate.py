"""[D] escalate — Hand task back to human. Pipeline stops here."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def execute(ctx: PipelineContext) -> NodeResult:
    ctx.escalated = True
    ctx.success = False

    reason_parts = []
    if ctx.ci_round >= ctx.max_ci_rounds and not ctx.test_passed:
        reason_parts.append(f"Tests failed after {ctx.ci_round} CI rounds")
    if ctx.review_feedback and not ctx.review_approved:
        reason_parts.append(f"Review rejected: {ctx.review_feedback[:200]}")
    if not ctx.modified_files:
        reason_parts.append("No code changes produced")

    ctx.error = " | ".join(reason_parts) or "Escalated to human"

    log.warning("escalate", ticket_id=ctx.ticket_id, reason=ctx.error,
                ci_rounds=ctx.ci_round, tokens=ctx.total_tokens)

    return NodeResult(success=False, error=ctx.error)
