"""[A] explore — Graph-powered exploration + planning. Haiku model."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)

EXPLORE_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_CALLS = 8


def execute(ctx: PipelineContext) -> NodeResult:
    from minions.agents.base import BaseAgent
    from minions.tools.explore_tools import EXPLORE_TOOLS

    if not ctx.bundle:
        return NodeResult(success=False, error="No context bundle available")

    prompt = _build_prompt(ctx)

    agent = BaseAgent(
        model=EXPLORE_MODEL,
        tools=EXPLORE_TOOLS,
        max_tool_calls=MAX_TOOL_CALLS,
        repo_path=ctx.repo_path,
        repo_id=ctx.repo_id,
    )

    result = agent.run(prompt)

    ctx.exploration_summary = result.reasoning
    ctx.plan = result.final_output

    if not ctx.plan:
        ctx.plan = result.reasoning  # Use reasoning as plan if no final output

    # Save files read by explorer to cross-node cache (avoids re-reads in write_code)
    ctx.files_read_summary.update(result.files_read)
    for fp, summary in result.files_read.items():
        if fp not in ctx.file_cache:
            # Read actual content for the cache so writer can skip re-reading
            full_path = ctx.repo_path / fp
            if full_path.exists():
                try:
                    ctx.file_cache[fp] = full_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

    log.info("explore.done", tools_used=result.tool_calls_made,
             tokens=result.total_tokens, plan_len=len(ctx.plan),
             files_cached=len(ctx.file_cache))

    return NodeResult(
        success=bool(ctx.plan),
        tokens_used=result.total_tokens,
        error="" if ctx.plan else "Explorer produced no plan",
    )


def _build_prompt(ctx: PipelineContext) -> str:
    sections = [
        "You are exploring a codebase to plan a fix. Use the graph tools to understand the code.",
        "",
        "## Ticket",
        f"**ID:** {ctx.ticket_id}",
        f"**Title:** {ctx.title}",
        "",
        ctx.body,
        "",
        "## Repository Map (from knowledge graph)",
        ctx.bundle.to_prompt_text(max_symbols=15, max_files=8),
    ]

    if ctx.profile:
        sections.extend([
            "",
            "## Project Profile (auto-detected)",
            f"- **Language:** {ctx.profile.get('language', 'unknown')}",
            f"- **Test command:** `{ctx.profile.get('test_command', 'auto-detect')}`",
        ])
        if ctx.profile.get("env_vars"):
            sections.append(f"- **Env vars:** {ctx.profile['env_vars']}")
        if ctx.profile.get("notes"):
            sections.append(f"- **Notes:** {ctx.profile['notes']}")

    if ctx.reproduce_output:
        sections.extend([
            "",
            "## Failing Test Output (reproduced before you started)",
            "```",
            ctx.reproduce_output[-3000:],
            "```",
        ])

    if ctx.directory_rules:
        sections.extend([
            "",
            "## Directory Rules",
            ctx.directory_rules[:2000],
        ])

    sections.extend([
        "",
        "## Your Task — 3 Steps (follow in order)",
        "",
        "### Step 1: DISCOVER target code",
        "- Use search_symbols (semantic) or find_files (by name) to locate relevant code",
        "- Use list_directory to understand module structure if unsure where code lives",
        "- Use file_outline BEFORE read_file — it shows structure without function bodies (10x fewer tokens)",
        "- Then read_file with start_line/end_line for just the sections you need",
        "",
        "### Step 2: ANALYZE blast radius with graph tools",
        "For EACH file you plan to modify, you MUST call:",
        "  get_change_context(file_path, symbol_name)",
        "This returns risk score, callers, dependents, test coverage, and coupled files in ONE call.",
        "",
        "Pay attention to:",
        "- coupled_files with score > 0.7 → you probably need to change those files too",
        "- will_break entries → plan how to update callers",
        "- risk_score > 0.5 → be extra careful, this file is load-bearing",
        "- test_files → mention these in your plan so the writer knows what tests to verify",
        "",
        "### Step 3: OUTPUT a plan",
        "The plan must include:",
        "  - Which files to modify/create (with FULL relative paths)",
        "  - What specific changes to make in each file",
        "  - Why each change is needed",
        "  - Risk assessment from get_change_context (coupled files, callers that will break)",
        "  - Which test files cover the changes",
        "",
        "## Tool Priority (use in this order)",
        "1. search_symbols / find_files → locate code",
        "2. file_outline → understand structure without reading full files",
        "3. get_change_context → blast radius analysis (MANDATORY for each target file)",
        "4. read_file with line range → read only what you need",
        "5. git_log → understand why code is the way it is (optional)",
        "",
        "Do NOT write code. Only explore and produce a plan.",
        "Keep the plan concise — it will be passed to a code-writing agent.",
    ])

    return "\n".join(sections)
