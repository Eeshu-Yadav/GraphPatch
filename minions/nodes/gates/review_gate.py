"""[G] review_gate — Route based on code review result."""
from __future__ import annotations

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext


def execute(ctx: PipelineContext) -> NodeResult:
    if ctx.review_approved:
        return NodeResult(success=True, next_node="create_pr")

    # No files left after review dropped them all
    if not ctx.implementation or not ctx.implementation.file_results:
        return NodeResult(success=False, next_node="escalate",
                          error="Review dropped all files")

    if ctx.review_fix_attempted:
        # Already tried fixing once — publish as draft
        ctx.draft_pr = True
        return NodeResult(success=True, next_node="create_pr")

    ctx.review_fix_attempted = True
    return NodeResult(success=False, next_node="fix_review")
