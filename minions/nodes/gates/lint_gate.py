"""[G] lint_gate — Route based on lint results."""
from __future__ import annotations

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext


def execute(ctx: PipelineContext) -> NodeResult:
    if not ctx.lint_errors:
        return NodeResult(success=True, next_node="run_tests")

    if ctx.lint_fix_attempted:
        # Already tried fixing once — move on, lint errors are non-fatal
        return NodeResult(success=True, next_node="run_tests")

    ctx.lint_fix_attempted = True
    return NodeResult(success=False, next_node="fix_lint")
