"""[D] assemble_context — Call L3 assembler. Zero LLM tokens (embeddings only)."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def execute(ctx: PipelineContext) -> NodeResult:
    from layer3_context.models.ticket import Ticket
    from layer3_context.assembly.assembler import assemble

    ctx.ticket = Ticket(
        ticket_id=ctx.ticket_id,
        title=ctx.title,
        body=ctx.body,
        repo_id=ctx.repo_id,
    )

    # Classify intent once, cache for reuse across pipeline
    if not ctx.intent:
        from layer3_context.assembly.assembler import _classify_ticket
        ctx.intent = _classify_ticket(ctx.ticket) or {}
        log.info("assemble_context.intent",
                 type=ctx.intent.get("type"),
                 complexity=ctx.intent.get("complexity", "unknown"),
                 targets=ctx.intent.get("target_symbols", []))

    ctx.bundle = assemble(ctx.ticket, intent=ctx.intent)

    log.info("assemble_context.done",
             symbols=len(ctx.bundle.relevant_symbols),
             files=len(ctx.bundle.relevant_files),
             tokens_est=ctx.bundle.token_estimate,
             strategies=ctx.bundle.strategies_used)

    return NodeResult(success=True, tokens_used=0)
