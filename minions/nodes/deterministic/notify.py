"""[D] notify — Log completion. Extensible for Slack/webhooks later."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def execute(ctx: PipelineContext) -> NodeResult:
    log.info(
        "notify.pipeline_complete",
        ticket_id=ctx.ticket_id,
        success=ctx.success,
        pr_url=ctx.pr_url,
        total_tokens=ctx.total_tokens,
        ci_rounds=ctx.ci_round,
        nodes=ctx.nodes_executed,
        duration=f"{ctx.total_duration:.1f}s",
    )
    return NodeResult(success=True, tokens_used=0)
