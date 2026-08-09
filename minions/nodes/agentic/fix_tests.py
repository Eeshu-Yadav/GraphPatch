"""[A] fix_tests — Fix test failures. Sonnet model."""
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

    if not ctx.test_output:
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

    log.info("fix_tests.done", tools_used=result.tool_calls_made,
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

    return f"""Fix these test failures. Minimal changes only.

## Test Output (CI round {ctx.ci_round})
```
{ctx.test_output[-3000:]}
```

## Files You Previously Modified
{files_text}
{file_sections}

## Instructions — Follow in Order

### Step 1: DIAGNOSE before fixing
- Call classify_test_result(test_output) to get the failure category and suggested action.
- If category is "import_error" → fix imports, don't touch logic.
- If category is "assertion_failure" → your code logic is wrong, read expected vs actual.
- If category is "infra_error" → environment problem, not your code.
- Use think() to reason about the root cause before writing.

### Step 2: VERIFY blast radius (if you changed a function signature)
- Call get_callers(symbol_name) for the function you modified.
- If callers appear in the failing test, you likely broke the caller contract.
- If no callers in failing test, the problem is logic in your own change.

### Step 3: FIX surgically
- Use checkpoint('before_fix') before attempting a fix.
- Call write_file with minimal changes — do NOT rewrite entire files.
- Call lint_check(file_path) after writing.
- If your fix makes things worse, call restore('before_fix') and try a different approach.

### Rules
- Tests define the spec. If the test expects X, your code must produce X.
- Do NOT call read_file on files shown above — they're already loaded.
- If CI round >= 2 and you're still stuck, try undo_edit to revert your last change and rethink."""
