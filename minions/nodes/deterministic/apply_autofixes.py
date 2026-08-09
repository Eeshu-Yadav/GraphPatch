"""[D] apply_autofixes — Try deterministic fixes for known patterns. Zero tokens."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def execute(ctx: PipelineContext) -> NodeResult:
    from minions.autofix.catalog import try_autofixes

    if not ctx.test_output and not ctx.lint_errors:
        return NodeResult(success=True, tokens_used=0)

    error_text = ctx.test_output + "\n" + "\n".join(ctx.lint_errors)
    changed_files = list(ctx.modified_files.keys()) if ctx.modified_files else []

    applied = try_autofixes(
        error_output=error_text,
        changed_files=changed_files,
        repo_path=ctx.repo_path,
    )

    ctx.autofix_applied.extend(applied)

    # Re-read modified files after autofixes
    if applied and ctx.implementation:
        for fr in ctx.implementation.file_results:
            full_path = ctx.repo_path / fr.file_path
            if full_path.exists():
                fr.modified_content = full_path.read_text(encoding="utf-8", errors="replace")
                ctx.modified_files[fr.file_path] = fr.modified_content

    log.info("autofixes.done", applied=applied)
    return NodeResult(success=True, tokens_used=0)
