"""
Lean Agent v4 — Adaptive context routing.

Adds to v3:
  1. tier-aware system prompt   — only mentions tools the agent actually has
  2. extra_write_tools param    — controls which graph tools are callable
  3. budget tracker             — _budget injected into every tool result
  4. finish nudge               — _finish_nudge injected after first write
  5. Layer 4 post-explore upgrade — auto-upgrades config if more files found

Called by agent_adaptive.py with the right config per tier.
Can also be used directly for specific-tier testing.

Single conversation — no context loss between explore and write.
"""
from __future__ import annotations

from pathlib import Path
import json
import os

import structlog
import anthropic

from lean_agent.graph_context import (
    build_graph_context, expand_from_files, expand_from_read, detect_missing_files,
)
from lean_agent.agent import (
    EXPLORE_TOOLS, WRITE_TOOLS,
    _summarize_old_results, CompletenessChecklist,
)
from lean_agent.agent_v3 import GRAPH_TOOLS_FOR_WRITE   # reuse definitions
from lean_agent.budget import inject_budget, inject_nudge
from lean_agent.cache import apply_rolling_cache
from lean_agent.classifier import upgrade_config

log = structlog.get_logger(__name__)

# ── Tool name sets ─────────────────────────────────────────────────────────────

_GRAPH_TOOL_NAMES = frozenset(t["name"] for t in GRAPH_TOOLS_FOR_WRITE)

# Lookup: tool name → tool schema dict
_GRAPH_TOOL_BY_NAME = {t["name"]: t for t in GRAPH_TOOLS_FOR_WRITE}

# Preset subsets per tier
_GRAPH_TOOLS_MEDIUM = [
    _GRAPH_TOOL_BY_NAME["get_test_coverage"],
    _GRAPH_TOOL_BY_NAME["get_dependencies"],
]
_GRAPH_TOOLS_HARD = GRAPH_TOOLS_FOR_WRITE   # all 5


def tools_for_tier(tier: str) -> list[dict]:
    """Return the graph tool subset for a given tier."""
    if tier == "easy":   return []
    if tier == "medium": return _GRAPH_TOOLS_MEDIUM
    return _GRAPH_TOOLS_HARD   # hard


# ── System prompt ──────────────────────────────────────────────────────────────

_WRITE_TOOLS_SECTION = {
    "easy": (
        "WRITE tools: read_file, write_file, search_code, run_command, run_tests, get_diff, finish\n\n"
        "The files you read during exploration are still in context. Write the fix directly."
    ),
    "medium": (
        "WRITE tools: read_file, write_file, search_code, run_command, run_tests, get_diff, finish\n"
        "GRAPH tools: get_test_coverage, get_dependencies\n\n"
        "- Use get_test_coverage(file) before running tests to get the exact test path\n"
        "- Use get_dependencies(file) when you need to check what to import\n"
        "- The files you read are still in context — write the fix"
    ),
    "hard": (
        "WRITE tools: read_file, write_file, search_code, run_command, run_tests, get_diff, finish\n"
        "GRAPH tools: get_impact, get_callers, get_dependencies, get_test_coverage, get_risk_score\n\n"
        "IMPORTANT — Before modifying any function:\n"
        "- Call get_impact(symbol) to see what breaks (blast radius)\n"
        "- Call get_callers(symbol) to see who uses it\n"
        "- Call get_test_coverage(file) to find the exact tests to run\n"
        "- The files you read are still in context — write the fix"
    ),
}

_SYSTEM_TEMPLATE = """\
You are a senior software engineer fixing a bug in a production codebase.

You work in TWO PHASES in a single conversation:

## Phase 1: EXPLORE (find the files)
Tools: find_files, search_code, read_file, list_directory, file_outline, done_exploring

- find_files FIRST, search_code SECOND, read_file for details
- Call multiple tools in PARALLEL when independent (e.g. find_files + search_code together)
- READ the files you will edit — the content stays in context for writing
- Call done_exploring when ready

## Phase 2: WRITE (fix the code)
After done_exploring, your tools change. You now have:

{write_tools_section}

After writing:
- Test your changes with run_tests or run_command
- Review with get_diff
- Call finish() with a summary of what changed

Note key details (line numbers, function signatures) when reading — you will need them.
Old tool results are summarised to save context — write down what matters in your response.

{graph_context}

## Ticket
**Title:** {ticket_title}
{ticket_body}"""


def _build_system(tier: str, graph_ctx: str, title: str, body: str) -> str:
    return _SYSTEM_TEMPLATE.format(
        write_tools_section=_WRITE_TOOLS_SECTION.get(tier, _WRITE_TOOLS_SECTION["hard"]),
        graph_context=graph_ctx if graph_ctx else "(No graph context available)",
        ticket_title=title,
        ticket_body=body,
    )


def _phase_swap_instructions(tier: str, files: list[str]) -> str:
    hints = {
        "easy": (
            "Phase switched to WRITE (easy config — 7 tools).\n"
            "The files you read are in context. Write the fix directly.\n"
            f"Files: {files}"
        ),
        "medium": (
            "Phase switched to WRITE (medium config — 9 tools).\n"
            "Extra tools: get_test_coverage, get_dependencies.\n"
            "Use get_test_coverage before running targeted tests.\n"
            f"Files: {files}"
        ),
        "hard": (
            "Phase switched to WRITE (hard config — 12 tools).\n"
            "Extra tools: get_impact, get_callers, get_dependencies, get_test_coverage, get_risk_score.\n"
            "BEFORE changing any function: call get_impact(symbol) for blast radius.\n"
            f"Files: {files}"
        ),
    }
    return hints.get(tier, hints["hard"])


# ── Main agent loop ────────────────────────────────────────────────────────────

def run_lean_agent_v4(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
    repo_path: str,
    api_key: str | None = None,
    max_turns: int = 60,
    model: str = "claude-sonnet-4-20250514",
    # ── Adaptive config ────────────────────────────────────────────────────────
    tier: str = "medium",
    extra_write_tools: list | None = None,   # None = infer from tier
    nudge_after_write: int = 15,
    enable_upgrade: bool = True,
) -> dict:
    """
    Single-conversation adaptive agent.

    tier              — starting config: "easy" | "medium" | "hard"
    extra_write_tools — graph tools to add to WRITE_TOOLS
                        None = infer from tier (recommended)
                        []   = no graph tools (force easy)
    nudge_after_write — write turns after first write_file before finish nudge starts
    enable_upgrade    — Layer 4: auto-upgrade if more files found than tier expects
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    # Resolve write toolset from tier if not explicitly passed
    if extra_write_tools is None:
        extra_write_tools = tools_for_tier(tier)
    write_toolset = WRITE_TOOLS + extra_write_tools

    # Phase 0: graph context (no API call, pure graph queries)
    log.info("lean4.graph_context", repo_id=repo_id, tier=tier)
    graph_ctx = build_graph_context(ticket_title, ticket_body, repo_id)
    log.info("lean4.graph_context.done", chars=len(graph_ctx))

    # System prompt — cached via ephemeral cache_control.
    # The same text is reused across all turns: only the first turn pays for
    # cache creation (~5 min TTL). All subsequent turns read from cache.
    system_text = _build_system(tier, graph_ctx, ticket_title, ticket_body)
    system_block = {
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral"},   # cache the full system prompt
    }

    messages = [
        {"role": "user", "content": "Find and fix the bug. Start with find_files to locate the relevant code."}
    ]

    # State
    phase = "explore"
    current_tier = tier
    current_tools = EXPLORE_TOOLS
    modified_files: dict[str, str] = {}
    original_files: dict[str, str] = {}
    tool_log: list[dict] = []
    total_tokens = 0
    total_cache_read = 0
    total_cache_create = 0
    explore_turns = 0
    write_turns = 0
    first_write_turn: int | None = None
    finished = False
    checklist = CompletenessChecklist(tier=current_tier)
    files_to_modify: list[str] = []
    explore_summary = ""
    tier_upgraded_from: str | None = None

    for turn in range(max_turns):
        turn_num = turn + 1
        if phase == "explore":
            explore_turns += 1
        else:
            write_turns += 1

        log.info("lean4.turn", n=turn_num, phase=phase, tier=current_tier)
        print(f"[v4] turn {turn_num}/{max_turns}  phase={phase}  tier={current_tier}  tokens_so_far={total_tokens:,}", flush=True)

        # Designed caching: roll 3 cache breakpoints on the newest tool_results.
        # Combined with system-prompt caching this drives hit rate from 19% → ~75%.
        # (See lean_agent/cache.py for the full design rationale.)
        apply_rolling_cache(messages)

        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[system_block],   # ephemeral cache — paid once, reused every turn
            messages=messages,
            tools=current_tools,
            temperature=0.0 if phase == "explore" else 0.1,
        )

        usage = response.usage
        total_tokens     += usage.input_tokens + usage.output_tokens
        total_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        total_cache_create += getattr(usage, "cache_creation_input_tokens", 0) or 0

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []

        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            tool_log.append({"turn": turn_num, "phase": phase, "tool": tool_name})
            log.info("lean4.tool", tool=tool_name, phase=phase, turn=turn_num)
            print(f"  → {tool_name}({list(tool_input.keys())})", flush=True)

            # ── done_exploring → phase swap ─────────────────────────────────
            if tool_name == "done_exploring":
                explore_summary = tool_input.get("summary", "")
                files_to_modify = tool_input.get("files_to_modify", [])

                # Layer 4: post-explore upgrade (ground truth)
                if enable_upgrade:
                    new_tier = upgrade_config(files_to_modify, current_tier)
                    if new_tier:
                        tier_upgraded_from = current_tier
                        current_tier = new_tier
                        extra_write_tools = tools_for_tier(new_tier)
                        write_toolset = WRITE_TOOLS + extra_write_tools
                        log.info("lean4.upgraded",
                                 from_tier=tier_upgraded_from,
                                 to_tier=current_tier,
                                 files=len(files_to_modify))

                phase = "write"
                current_tools = write_toolset
                log.info("lean4.phase_swap",
                         tier=current_tier,
                         files=files_to_modify,
                         n_tools=len(current_tools),
                         turn=turn_num)

                result: dict = {
                    "acknowledged": True,
                    "files": files_to_modify,
                    "_instructions": _phase_swap_instructions(current_tier, files_to_modify),
                }
                if tier_upgraded_from:
                    result["_config_upgrade"] = (
                        f"Config auto-upgraded {tier_upgraded_from} → {current_tier}: "
                        f"{len(files_to_modify)} files found. "
                        f"Additional graph tools are now available."
                    )

            # ── finish with circuit-breaker gate ────────────────────────────
            elif tool_name == "finish":
                summary = tool_input.get("summary", "")
                allowed, reason = checklist.check_finish_allowed(summary)
                if allowed:
                    finished = True
                    if reason:
                        summary = f"{summary}\n\n{reason}"
                    result = {"acknowledged": True, "summary": summary}
                    log.info("lean4.finish", turn=turn_num)
                else:
                    result = {"acknowledged": False, "error": reason}
                    log.info("lean4.finish_blocked",
                             turn=turn_num, count=checklist.finish_block_count)

            # ── graph tools ──────────────────────────────────────────────────
            elif tool_name in _GRAPH_TOOL_NAMES:
                from layer45_agent.tools import execute_tool
                result = execute_tool(
                    name=tool_name,
                    args=tool_input,
                    repo_path=Path(repo_path),
                    repo_id=repo_id,
                    modified_files=modified_files,
                    original_files=original_files,
                    sandbox=None,
                )
                log.info("lean4.graph_tool", tool=tool_name, turn=turn_num)

            # ── all other tools ──────────────────────────────────────────────
            else:
                from layer45_agent.tools import execute_tool
                result = execute_tool(
                    name=tool_name,
                    args=tool_input,
                    repo_path=Path(repo_path),
                    repo_id=repo_id,
                    modified_files=modified_files,
                    original_files=original_files,
                    sandbox=None,
                )

                # Auto-inject: graph expansion after find_files
                if tool_name == "find_files" and isinstance(result, dict):
                    found = result.get("files", [])
                    if found:
                        expansion = expand_from_files(found, repo_id)
                        if expansion:
                            result["_graph_context"] = expansion

                # Auto-inject: class details after read_file
                if tool_name == "read_file" and isinstance(result, dict):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        class_info = expand_from_read(fp, repo_id)
                        if class_info:
                            result["_class_details"] = class_info

                # Auto post-write: completeness detection after write_file
                if (tool_name == "write_file"
                        and isinstance(result, dict)
                        and result.get("success")):
                    fp = tool_input.get("file_path", "")
                    checklist.on_write_file(fp)
                    if first_write_turn is None:
                        first_write_turn = write_turns
                    content = modified_files.get(fp, "")
                    if fp and content:
                        warnings = detect_missing_files(fp, content, repo_id, modified_files)
                        if warnings:
                            checklist.add_warnings(warnings, fp)
                            msg = checklist.get_checklist_message()
                            if msg:
                                result["_completeness_checklist"] = msg
                            log.info("lean4.completeness", file=fp, count=len(warnings))

            # Budget tracker — every result, every phase
            result = inject_budget(result, turn_num, max_turns)

            # Finish nudge — write phase only, after threshold
            if phase == "write":
                result = inject_nudge(
                    result, write_turns, first_write_turn, nudge_after_write
                )

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            log.info("lean4.finished", turn=turn_num, files=len(modified_files))
            break

        if response.stop_reason == "end_turn" and phase == "write":
            log.info("lean4.end_turn", turn=turn_num)
            break

    total_turns = explore_turns + write_turns
    log.info("lean4.done",
             tier=current_tier,
             tier_upgraded_from=tier_upgraded_from,
             explore_turns=explore_turns,
             write_turns=write_turns,
             total_turns=total_turns,
             total_tokens=total_tokens,
             cache_read=total_cache_read,
             cache_create=total_cache_create,
             files_changed=len(modified_files),
             finished=finished)

    return {
        "success": finished,
        "tier": current_tier,
        "tier_upgraded_from": tier_upgraded_from,
        "explore_turns": explore_turns,
        "write_turns": write_turns,
        "total_turns": total_turns,
        "total_tokens": total_tokens,
        "total_cache_read": total_cache_read,
        "total_cache_create": total_cache_create,
        "explore_tools":    [t for t in tool_log if t["phase"] == "explore"],
        "write_tools":      [t for t in tool_log if t["phase"] == "write"],
        "graph_tools_used": [t for t in tool_log if t["tool"] in _GRAPH_TOOL_NAMES],
        "files_to_modify": files_to_modify,
        "files_changed":   list(modified_files.keys()),
        "explore_summary": explore_summary,
        "graph_context_chars": len(graph_ctx),
        "finished": finished,
    }
