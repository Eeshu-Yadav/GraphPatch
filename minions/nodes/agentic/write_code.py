"""[A] write_code — Code generation from plan. Sonnet model (Opus only for plan, not write)."""
from __future__ import annotations

import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)

# Sonnet for write — Opus is 5x more expensive and not needed for code gen
# Existing codebase uses Sonnet for Phase 3+ (write/verify)
WRITE_MODEL = "claude-sonnet-4-20250514"
MAX_TOOL_CALLS = 16


def _build_implementation(ctx: PipelineContext):
    """Convert modified_files dict into L5 Implementation object."""
    from layer45_agent.implementation import Implementation, FileResult

    file_results = []
    for fp, new_content in ctx.modified_files.items():
        original = ctx.original_files.get(fp, "")
        if new_content == original:
            continue
        change_type = "create" if not original else "modify"
        file_results.append(FileResult(
            file_path=fp,
            change_type=change_type,
            original_content=original,
            modified_content=new_content,
            explanation="Modified by minion write_code node",
        ))

    return Implementation(
        ticket_id=ctx.ticket_id,
        repo_id=ctx.repo_id,
        plan_summary=ctx.plan[:500] if ctx.plan else "Agent implementation",
        file_results=file_results,
        model_used=WRITE_MODEL,
    )


def execute(ctx: PipelineContext) -> NodeResult:
    from minions.agents.base import BaseAgent
    from minions.tools.write_tools import WRITE_TOOLS

    if not ctx.plan:
        return NodeResult(success=False, error="No plan available")

    prompt = _build_prompt(ctx)

    agent = BaseAgent(
        model=WRITE_MODEL,
        tools=WRITE_TOOLS,
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
    ctx.implementation = _build_implementation(ctx)

    # Sync written files into cache so fix nodes don't re-read
    for fp, content in result.modified_files.items():
        ctx.file_cache[fp] = content
    ctx.files_read_summary.update(result.files_read)

    files_changed = len(ctx.implementation.file_results)
    log.info("write_code.done", files=files_changed,
             tools_used=result.tool_calls_made, tokens=result.total_tokens)

    return NodeResult(
        success=(files_changed > 0),
        tokens_used=result.total_tokens,
        error="" if files_changed > 0 else "Writer produced no file changes",
    )


def _build_prompt(ctx: PipelineContext) -> str:
    sections = [
        "You are a code writer. Your ONLY job is to call write_file. Do NOT explore or search.",
        "",
        "## Plan (from explorer — already verified)",
        ctx.plan,
    ]

    if ctx.profile:
        sections.extend([
            "",
            "## Project Profile",
            f"- **Language:** {ctx.profile.get('language', 'unknown')}",
            f"- **Test command:** `{ctx.profile.get('test_command', 'auto-detect')}`",
        ])
        if ctx.profile.get("notes"):
            sections.append(f"- **Notes:** {ctx.profile['notes']}")

    # Inject file contents from cross-node cache so writer doesn't re-read
    if ctx.file_cache:
        sections.extend(["", "## File Contents (already read — do NOT re-read these)"])
        for fp, content in ctx.file_cache.items():
            # Cap each file at 200 lines to keep prompt manageable
            lines = content.splitlines()
            if len(lines) > 200:
                preview = "\n".join(lines[:200])
                sections.append(f"\n### {fp} (first 200 of {len(lines)} lines)")
                sections.append(f"```\n{preview}\n... [{len(lines) - 200} more lines]\n```")
            else:
                sections.append(f"\n### {fp}")
                sections.append(f"```\n{content}\n```")

    if ctx.exploration_summary:
        sections.extend([
            "",
            "## Explorer's Analysis",
            ctx.exploration_summary[:2000],
        ])

    if ctx.directory_rules:
        sections.extend([
            "",
            "## Directory Rules",
            ctx.directory_rules[:2000],
        ])

    sections.extend([
        "",
        "## RULES — READ CAREFULLY",
        "",
        "### Pre-Write (before your first write_file)",
        "- If modifying a function signature, call get_impact(symbol_name) to see what breaks.",
        "- If you're unsure about the change scope, call get_change_context(file_path) — it returns",
        "  risk, callers, dependents, tests, and coupled files in one call.",
        "- Use think() to plan multi-edit sequences before writing.",
        "- Use checkpoint('before_changes') before risky changes — restore() if things go wrong.",
        "",
        "### Writing",
        "1. Call write_file — that is your primary job.",
        "2. Files above are already loaded — use them directly. Do NOT call read_file on files shown above.",
        "3. You may read_file ONCE for files NOT shown above, then immediately write_file.",
        "4. ONE write_file call per file, ALL edits in the edits array.",
        "5. Use small, unique search strings (3-5 lines of exact context).",
        "6. Match existing code style EXACTLY.",
        "",
        "### Post-Write",
        "- Call lint_check(file_path) after each write_file to catch issues immediately.",
        "- If lint fails, fix with undo_edit + re-write, not patch-on-patch.",
        "- Call self_review() before finish() to catch debug code and unintended changes.",
        "",
        "### Do NOT",
        "- Do NOT search_code — the explorer already did that.",
        "- Do NOT create new utility files or wrappers.",
    ])

    return "\n".join(sections)
