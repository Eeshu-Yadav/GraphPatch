"""[A] fix_lint — Fix remaining lint errors after auto-fix. Sonnet model."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)

FIX_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_CALLS = 6


def execute(ctx: PipelineContext) -> NodeResult:
    from minions.agents.base import BaseAgent
    from minions.tools.fix_tools import FIX_TOOLS

    if not ctx.lint_errors:
        return NodeResult(success=True, tokens_used=0)

    prompt = _build_prompt(ctx)

    agent = BaseAgent(
        model=FIX_MODEL,
        tools=FIX_TOOLS,
        max_tool_calls=MAX_TOOL_CALLS,
        repo_path=ctx.repo_path,
        repo_id=ctx.repo_id,
    )

    result = agent.run(
        prompt,
        existing_modifications=ctx.modified_files,
        existing_originals=ctx.original_files,
    )

    # Update context with fixes
    ctx.modified_files = result.modified_files
    ctx.original_files = result.original_files

    # Rebuild implementation
    from minions.nodes.agentic.write_code import _build_implementation
    ctx.implementation = _build_implementation(ctx)

    log.info("fix_lint.done", tools_used=result.tool_calls_made,
             tokens=result.total_tokens)

    return NodeResult(success=True, tokens_used=result.total_tokens)


def _build_prompt(ctx: PipelineContext) -> str:
    errors_text = "\n".join(ctx.lint_errors[:30])
    files_text = "\n".join(f"- {fp}" for fp in ctx.modified_files.keys())

    return f"""Fix these lint errors. Minimal changes only.

## Lint Errors
```
{errors_text}
```

## Files You Modified
{files_text}

## Instructions
1. Read each file with errors, fix the specific lint issues with write_file.
2. After each write_file, call lint_check(file_path) to verify the fix worked.
3. If a fix introduces new lint errors, call undo_edit(file_path) and try a different approach.
4. Do NOT rewrite entire files — fix only the flagged lines."""
