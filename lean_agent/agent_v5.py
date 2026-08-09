"""
Lean Agent v5 — v4 without the tool swap (stable tool list → preserved cache).

v4 swapped tools at done_exploring (6 EXPLORE → 12 WRITE+graph). Per Anthropic
caching docs: "Changes to tools invalidate their respective cache levels and
all subsequent levels." That one swap busts the entire message cache built
during the explore phase.

v5 fixes this by exposing the full tool list from turn 1:
    EXPLORE_TOOLS ∪ WRITE_TOOLS ∪ graph_tools_for_tier(tier)
    (dedup'd by name)

Phase is tracked for logging / budget / nudge purposes only — tools never
change mid-run. The system prompt still instructs the agent to work in two
phases and call done_exploring to signal the transition, but the agent
*technically* could call write_file during explore. Tradeoff: weaker
phase enforcement, stronger cache retention.

Everything else identical to v4: adaptive tier, graph tools, budget tracker,
finish nudge, completeness checklist, rolling cache breakpoints.

Expected improvement vs v4 on the same issue:
  • Cache hit rate: 19% → ~70%+ (no invalidation at swap)
  • Total billed tokens: roughly halved
  • Turn count / success: should be similar (same agent capabilities)
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
    CompletenessChecklist,
)
from lean_agent.agent_v3 import GRAPH_TOOLS_FOR_WRITE
from lean_agent.budget import inject_budget, inject_nudge
from lean_agent.cache import apply_rolling_cache
from lean_agent.classifier import upgrade_config
from lean_agent.agent_v4 import tools_for_tier, _WRITE_TOOLS_SECTION
from lean_agent.tier_config import TierConfig

log = structlog.get_logger(__name__)

_GRAPH_TOOL_NAMES = frozenset(t["name"] for t in GRAPH_TOOLS_FOR_WRITE)


# ── Tool list merging ─────────────────────────────────────────────────────────

def _merge_tools(*tool_lists: list[dict]) -> list[dict]:
    """Union of tool lists, de-duplicated by name (first occurrence wins)."""
    seen: set[str] = set()
    out: list[dict] = []
    for lst in tool_lists:
        for tool in lst:
            name = tool.get("name")
            if name and name not in seen:
                seen.add(name)
                out.append(tool)
    return out


def unified_tools_for_tier(tier: str) -> list[dict]:
    """
    The FULL tool list available to v5 from turn 1.

    Contains EXPLORE + WRITE + tier-appropriate graph tools, dedup'd.
    This list is stable across the whole run → cache is preserved.
    """
    return _merge_tools(EXPLORE_TOOLS, WRITE_TOOLS, tools_for_tier(tier))


# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM_TEMPLATE = """\
You are a senior software engineer fixing a bug in a production codebase.

You have ALL tools available throughout, but you MUST work in two logical phases:

## Phase 1: EXPLORE (find the files)
Start here. Use these tools to understand the bug:
  find_files, search_code, read_file, list_directory, file_outline

- find_files FIRST, search_code SECOND, read_file for details
- Call multiple tools in PARALLEL when independent
- READ the files you will edit — the content stays in context for writing
- DO NOT call write_file / run_command / run_tests during Phase 1
- When ready, call done_exploring to signal the transition

## Phase 2: WRITE (fix the code)
After done_exploring, you may use these tools:

{write_tools_section}

After writing:
- Test your changes with run_tests or run_command
- Review with get_diff
- Call finish() with a summary of what changed

Note key details (line numbers, function signatures) when reading.
Old tool results are summarised to save context — write down what matters.

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
    return (
        f"Phase switched to WRITE ({tier} config).\n"
        f"Tools unchanged — now use write_file / run_command / run_tests / finish.\n"
        f"Files to modify: {files}"
    )


# ── Main agent loop ────────────────────────────────────────────────────────────

def run_lean_agent_v5(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
    repo_path: str,
    api_key: str | None = None,
    max_turns: int = 60,
    model: str = "claude-sonnet-4-20250514",
    tier: str = "medium",
    nudge_after_write: int = 15,
    enable_upgrade: bool = True,
) -> dict:
    """
    v5: v4 mechanics + stable unified tool list (no swap → cache preserved).
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    # Phase 0: graph context
    log.info("lean5.graph_context", repo_id=repo_id, tier=tier)
    graph_ctx = build_graph_context(ticket_title, ticket_body, repo_id)
    log.info("lean5.graph_context.done", chars=len(graph_ctx))

    system_text = _build_system(tier, graph_ctx, ticket_title, ticket_body)
    system_block = {
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral"},
    }

    # Unified tool list — stable for the whole run, tier-aware.
    current_tools = unified_tools_for_tier(tier)
    log.info("lean5.tools_unified", n=len(current_tools), tier=tier,
             names=[t["name"] for t in current_tools])

    messages = [
        {"role": "user", "content": "Find and fix the bug. Start with find_files to locate the relevant code."}
    ]

    # State
    phase = "explore"
    current_tier = tier
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

    turn = 0
    while turn < max_turns:
        turn_num = turn + 1
        if phase == "explore":
            explore_turns += 1
        else:
            write_turns += 1

        log.info("lean5.turn", n=turn_num, phase=phase, tier=current_tier)
        print(f"[v5] turn {turn_num}/{max_turns}  phase={phase}  tier={current_tier}  tokens_so_far={total_tokens:,}", flush=True)

        # Designed caching: roll 3 cache breakpoints on newest tool_results.
        apply_rolling_cache(messages)

        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[system_block],
            messages=messages,
            tools=current_tools,           # ← STABLE across all turns — key difference from v4
            temperature=0.0 if phase == "explore" else 0.1,
        )

        usage = response.usage
        total_tokens        += usage.input_tokens + usage.output_tokens
        total_cache_read    += getattr(usage, "cache_read_input_tokens", 0) or 0
        total_cache_create  += getattr(usage, "cache_creation_input_tokens", 0) or 0

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []

        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            tool_log.append({"turn": turn_num, "phase": phase, "tool": tool_name})
            log.info("lean5.tool", tool=tool_name, phase=phase, turn=turn_num)
            print(f"  → {tool_name}({list(tool_input.keys())})", flush=True)

            # done_exploring → phase var flip ONLY (tools unchanged)
            if tool_name == "done_exploring":
                explore_summary = tool_input.get("summary", "")
                files_to_modify = tool_input.get("files_to_modify", [])

                if enable_upgrade:
                    new_tier = upgrade_config(files_to_modify, current_tier)
                    if new_tier:
                        tier_upgraded_from = current_tier
                        current_tier = new_tier
                        # Adopt the new tier's full config (max_turns, nudge,
                        # tool list) atomically — single source of truth so all
                        # tier-derived values stay coherent after escalation.
                        new_cfg = TierConfig.for_tier(new_tier)
                        if new_cfg.max_turns > max_turns:
                            log.info("lean5.cfg_upgraded",
                                     from_tier=tier_upgraded_from, to_tier=new_tier,
                                     max_turns=f"{max_turns}→{new_cfg.max_turns}",
                                     nudge_after_write=f"{nudge_after_write}→{new_cfg.nudge_after_write}")
                            max_turns = new_cfg.max_turns
                            nudge_after_write = new_cfg.nudge_after_write
                            # Re-tune the finish-checklist thresholds for the new tier
                            # (e.g. medium release_at=3 → hard release_at=4 = more leniency).
                            checklist.tier = new_cfg.tier
                            checklist.release_at = checklist._RELEASE_AT.get(new_cfg.tier, checklist.release_at)
                            checklist.strict_until = checklist._STRICT_UNTIL.get(new_cfg.tier, checklist.strict_until)
                        # Extend (don't swap) the tool list — preserves cache.
                        existing = {t["name"] for t in current_tools}
                        added = [t for t in new_cfg.tools if t["name"] not in existing]
                        if added:
                            current_tools = current_tools + added
                            log.info("lean5.tools_extended",
                                     from_tier=tier_upgraded_from, to_tier=current_tier,
                                     added=[t["name"] for t in added])
                        else:
                            log.info("lean5.upgraded_no_tool_change",
                                     from_tier=tier_upgraded_from, to_tier=current_tier)

                phase = "write"
                log.info("lean5.phase_flip", tier=current_tier, files=files_to_modify, turn=turn_num)

                result: dict = {
                    "acknowledged": True,
                    "files": files_to_modify,
                    "_instructions": _phase_swap_instructions(current_tier, files_to_modify),
                }
                if tier_upgraded_from:
                    result["_config_upgrade"] = (
                        f"Config auto-upgraded {tier_upgraded_from} → {current_tier}: "
                        f"{len(files_to_modify)} files found."
                    )

            elif tool_name == "finish":
                summary = tool_input.get("summary", "")
                allowed, reason = checklist.check_finish_allowed(summary)
                if allowed:
                    finished = True
                    if reason:
                        summary = f"{summary}\n\n{reason}"
                    result = {"acknowledged": True, "summary": summary}
                    log.info("lean5.finish", turn=turn_num)
                else:
                    result = {"acknowledged": False, "error": reason}
                    log.info("lean5.finish_blocked", turn=turn_num, count=checklist.finish_block_count)

            elif tool_name in _GRAPH_TOOL_NAMES:
                from layer45_agent.tools import execute_tool
                result = execute_tool(
                    name=tool_name, args=tool_input,
                    repo_path=Path(repo_path), repo_id=repo_id,
                    modified_files=modified_files, original_files=original_files,
                    sandbox=None,
                )
                log.info("lean5.graph_tool", tool=tool_name, turn=turn_num)

            else:
                from layer45_agent.tools import execute_tool
                result = execute_tool(
                    name=tool_name, args=tool_input,
                    repo_path=Path(repo_path), repo_id=repo_id,
                    modified_files=modified_files, original_files=original_files,
                    sandbox=None,
                )

                if tool_name == "find_files" and isinstance(result, dict):
                    found = result.get("files", [])
                    if found:
                        expansion = expand_from_files(found, repo_id)
                        if expansion:
                            result["_graph_context"] = expansion

                if tool_name == "read_file" and isinstance(result, dict):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        class_info = expand_from_read(fp, repo_id)
                        if class_info:
                            result["_class_details"] = class_info

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
                            log.info("lean5.completeness", file=fp, count=len(warnings))

            result = inject_budget(result, turn_num, max_turns)
            if phase == "write":
                result = inject_nudge(result, write_turns, first_write_turn, nudge_after_write)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            log.info("lean5.finished", turn=turn_num, files=len(modified_files))
            break

        if response.stop_reason == "end_turn" and phase == "write":
            log.info("lean5.end_turn", turn=turn_num)
            break

        turn += 1

    total_turns = explore_turns + write_turns
    log.info("lean5.done",
             tier=current_tier, tier_upgraded_from=tier_upgraded_from,
             explore_turns=explore_turns, write_turns=write_turns,
             total_turns=total_turns,
             total_tokens=total_tokens,
             cache_read=total_cache_read, cache_create=total_cache_create,
             files_changed=len(modified_files), finished=finished,
             n_tools=len(current_tools))

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
        "n_tools": len(current_tools),
        "finished": finished,
    }
