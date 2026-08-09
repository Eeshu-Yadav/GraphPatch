"""[A] fix_review — Address Opus review feedback. Sonnet model."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)

FIX_MODEL = "claude-sonnet-4-20250514"
MAX_TOOL_CALLS = 8


def execute(ctx: PipelineContext) -> NodeResult:
    from minions.agents.base import BaseAgent
    from minions.tools.fix_tools import FIX_TOOLS

    if not ctx.review_feedback:
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

    ctx.modified_files = result.modified_files
    ctx.original_files = result.original_files

    from minions.nodes.agentic.write_code import _build_implementation
    ctx.implementation = _build_implementation(ctx)

    log.info("fix_review.done", tools_used=result.tool_calls_made,
             tokens=result.total_tokens)

    return NodeResult(success=True, tokens_used=result.total_tokens)


def _build_prompt(ctx: PipelineContext) -> str:
    files_text = "\n".join(f"- {fp}" for fp in ctx.modified_files.keys())

    # Inject cached file contents so fixer doesn't re-read
    file_sections = ""
    if ctx.file_cache:
        parts = []
        for fp in list(ctx.modified_files.keys())[:3]:
            content = ctx.file_cache.get(fp) or ctx.modified_files.get(fp, "")
            if content:
                lines = content.splitlines()
                preview = "\n".join(lines[:150])
                parts.append(f"### {fp}\n```\n{preview}\n```")
        if parts:
            file_sections = "\n## Current File Contents (do NOT re-read these)\n" + "\n".join(parts)

    return f"""Address this code review feedback. Fix what the reviewer flagged.

## Review Feedback
{ctx.review_feedback}

## Files That Were Reviewed
{files_text}
{file_sections}

## Instructions — Follow in Order

### Step 1: UNDERSTAND the feedback
- Use think() to categorize each review comment: logic bug, style issue, missing edge case, or unnecessary code.
- If the reviewer flagged a function signature change, call get_impact(symbol_name) to verify you haven't broken callers.

### Step 2: FIX what was flagged
- Use checkpoint('before_review_fix') before making changes.
- Fix ONLY what the reviewer flagged — call write_file with minimal edits.
- Call lint_check(file_path) after each fix.
- Do NOT call read_file on files shown above — they're already loaded.

### Step 3: VERIFY
- If the reviewer said a file is unnecessary, do NOT recreate it.
- Do NOT add unrelated improvements.
- If your fix breaks something, restore('before_review_fix') and try again."""
