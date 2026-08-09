"""[G] test_gate — Hard 2-round cap. Agent cannot override this."""
from __future__ import annotations

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext


def execute(ctx: PipelineContext) -> NodeResult:
    if ctx.test_passed:
        return NodeResult(success=True, next_node="build_check")

    if ctx.ci_round >= ctx.max_ci_rounds:
        # HARD CAP — no more retries
        return NodeResult(
            success=False,
            next_node="escalate",
            error=f"Tests failed after {ctx.ci_round} CI rounds",
        )

    # Still have retries — go through autofix → agent fix → retest
    return NodeResult(success=False, next_node="apply_autofixes")
