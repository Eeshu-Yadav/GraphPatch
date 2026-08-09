"""
Lean Agent v6 — v5 + TDD protocol + independent verification agent.

Three changes from v5:

1. TDD prompt: agent writes a reproduction test FIRST, verifies it fails,
   writes the fix, verifies test passes. The test IS the verification —
   not self-review of the diff (which has confirmation bias).

2. Softened W3: get_diff is recommended in the prompt (like Claude Code),
   NOT enforced as a hard gate. Hard gates on self-review waste turns
   without improving fix quality (proven on django-14053: 3 blocks, still
   wrong content, still unresolved).

3. Independent verification agent: when the builder agent calls finish(),
   a SEPARATE LLM call reviews the bug description + diff adversarially.
   Adapted from Claude Code's VERIFICATION_AGENT pattern:
     - Fresh context (no shared memory → no confirmation bias)
     - Adversarial ("try to break it, not confirm it works")
     - Returns VERDICT: PASS/FAIL with reasoning
     - On FAIL: reasoning is injected back to builder, who gets another try
     - On PASS: finish is allowed

Heritage: v5 (stable tools, rolling cache, tier config, budget, nudge).
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re

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
from lean_agent.agent_v5 import unified_tools_for_tier, _SYSTEM_TEMPLATE
from lean_agent.agent_v4 import _WRITE_TOOLS_SECTION
from lean_agent.budget import inject_budget, inject_nudge
from lean_agent.cache import apply_rolling_cache
from lean_agent.classifier import upgrade_config
from lean_agent.tier_config import TierConfig

log = structlog.get_logger(__name__)

_GRAPH_TOOL_NAMES = frozenset(t["name"] for t in GRAPH_TOOLS_FOR_WRITE)


# ── TDD + honest-reporting prompt addendum ────────────────────────────────────

_V6_ADDENDUM = """

## How to fix bugs (test-driven protocol)
1. Write a MINIMAL reproduction test that demonstrates the bug.
   Add it to an EXISTING test file (not a new file — reuse the project's test setup).
   Run it. It should FAIL on the current code. If it passes, your test is wrong — revise.

2. Write the fix. Keep it minimal — change only what the bug requires.

3. Run your reproduction test again. It must PASS now.
   Also run any existing tests in the same test file to catch regressions.
   If anything fails, fix it before proceeding.

4. Call get_diff to review your changes, then call finish.

## Honest reporting
Before reporting a task complete, verify it actually works: run the test, check
the output. If tests fail, say so — do not finish claiming success. If you did
not run a verification step, say that rather than implying it succeeded.
"""


# ── Independent verification sub-agent ────────────────────────────────────────
# Full multi-turn agent (not just a single API call). Adapted from Claude Code's
# VERIFICATION_AGENT. Can run tools (read_file, run_tests, run_command) but
# CANNOT write files. See lean_agent/verification_agent.py for full implementation.


def _build_system(tier: str, graph_ctx: str, title: str, body: str) -> str:
    base = _SYSTEM_TEMPLATE.format(
        write_tools_section=_WRITE_TOOLS_SECTION.get(tier, _WRITE_TOOLS_SECTION["hard"]),
        graph_context=graph_ctx if graph_ctx else "(No graph context available)",
        ticket_title=title,
        ticket_body=body,
    )
    return base + _V6_ADDENDUM


def _phase_swap_instructions(tier: str, files: list[str]) -> str:
    return (
        f"Phase switched to WRITE ({tier} config).\n"
        f"Follow the test-driven protocol:\n"
        f"  1. Write a reproduction test in an EXISTING test file → run → confirm FAIL\n"
        f"  2. Write the fix\n"
        f"  3. Run reproduction test + existing tests → confirm PASS\n"
        f"  4. get_diff → finish\n"
        f"Files to modify: {files}"
    )


# ── Main agent loop ────────────────────────────────────────────────────────────

def run_lean_agent_v6(
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
    # v6-specific
    enable_verifier: bool = True,
    max_verifier_rejects: int = 2,
) -> dict:
    """
    v6 = v5 + TDD prompt + independent verification agent.

    enable_verifier: spawn a separate Haiku call to adversarially review the
                     diff when finish() is called. FAIL → inject reasoning,
                     agent gets another try. After max_verifier_rejects failures,
                     finish is released with warnings.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    log.info("lean6.graph_context", repo_id=repo_id, tier=tier)
    graph_ctx = build_graph_context(ticket_title, ticket_body, repo_id)
    log.info("lean6.graph_context.done", chars=len(graph_ctx))

    system_text = _build_system(tier, graph_ctx, ticket_title, ticket_body)
    system_block = {
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral"},
    }

    current_tools = unified_tools_for_tier(tier)
    log.info("lean6.tools_unified", n=len(current_tools), tier=tier)

    messages = [
        {"role": "user", "content": "Find and fix the bug. Start with find_files to locate the relevant code."}
    ]

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

    # v6 verifier tracking
    verifier_rejects: int = 0
    verifier_verdicts: list[dict] = []

    turn = 0
    while turn < max_turns:
        turn_num = turn + 1
        if phase == "explore":
            explore_turns += 1
        else:
            write_turns += 1

        log.info("lean6.turn", n=turn_num, phase=phase, tier=current_tier)
        print(f"[v6] turn {turn_num}/{max_turns}  phase={phase}  tier={current_tier}  tokens_so_far={total_tokens:,}", flush=True)

        apply_rolling_cache(messages)

        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[system_block],
            messages=messages,
            tools=current_tools,
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
            log.info("lean6.tool", tool=tool_name, phase=phase, turn=turn_num)
            print(f"  → {tool_name}({list(tool_input.keys())})", flush=True)

            if tool_name == "done_exploring":
                explore_summary = tool_input.get("summary", "")
                files_to_modify = tool_input.get("files_to_modify", [])

                if enable_upgrade:
                    new_tier = upgrade_config(files_to_modify, current_tier)
                    if new_tier:
                        tier_upgraded_from = current_tier
                        current_tier = new_tier
                        new_cfg = TierConfig.for_tier(new_tier)
                        if new_cfg.max_turns > max_turns:
                            log.info("lean6.cfg_upgraded",
                                     from_tier=tier_upgraded_from, to_tier=new_tier,
                                     max_turns=f"{max_turns}→{new_cfg.max_turns}",
                                     nudge_after_write=f"{nudge_after_write}→{new_cfg.nudge_after_write}")
                            max_turns = new_cfg.max_turns
                            nudge_after_write = new_cfg.nudge_after_write
                            checklist.tier = new_cfg.tier
                            checklist.release_at = checklist._RELEASE_AT.get(new_cfg.tier, checklist.release_at)
                            checklist.strict_until = checklist._STRICT_UNTIL.get(new_cfg.tier, checklist.strict_until)
                        existing = {t["name"] for t in current_tools}
                        added = [t for t in new_cfg.tools if t["name"] not in existing]
                        if added:
                            current_tools = current_tools + added
                            log.info("lean6.tools_extended",
                                     from_tier=tier_upgraded_from, to_tier=new_tier,
                                     added=[t["name"] for t in added])

                phase = "write"
                log.info("lean6.phase_flip", tier=current_tier, files=files_to_modify, turn=turn_num)

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

                # ── Independent verification sub-agent (before checklist) ─────
                if enable_verifier and verifier_rejects < max_verifier_rejects:
                    # Flush modified files to disk so verifier sees them
                    for fp, content in modified_files.items():
                        abs_path = Path(repo_path) / fp
                        abs_path.parent.mkdir(parents=True, exist_ok=True)
                        abs_path.write_text(content, encoding="utf-8")

                    from lean_agent.verification_agent import run_verification
                    verdict, reasoning, v_log = run_verification(
                        bug_description=ticket_body,
                        files_changed=list(modified_files.keys()),
                        approach_summary=summary,
                        repo_path=repo_path,
                        repo_id=repo_id,
                        api_key=api_key,
                        model=model,
                    )
                    verifier_verdicts.append({
                        "turn": turn_num, "verdict": verdict,
                        "reasoning": reasoning[:800],
                        "verifier_turns": len(v_log),
                    })
                    log.info("lean6.verifier",
                             turn=turn_num, verdict=verdict,
                             verifier_turns=len(v_log),
                             reasoning=reasoning[:200])

                    if verdict == "FAIL":
                        verifier_rejects += 1
                        result = {
                            "acknowledged": False,
                            "error": (
                                f"INDEPENDENT VERIFICATION FAILED (attempt "
                                f"{verifier_rejects}/{max_verifier_rejects}).\n\n"
                                f"A separate verification agent ran tests and "
                                f"checked your changes. Findings:\n\n"
                                f"{reasoning[:2000]}\n\n"
                                f"Fix the issues identified above, then call finish() again."
                            ),
                        }
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                        continue
                    # verdict == PASS or PARTIAL or ERROR → proceed to checklist

                # ── Completeness checklist (existing) ────────────────────
                allowed, reason = checklist.check_finish_allowed(summary)
                if allowed:
                    finished = True
                    if reason:
                        summary = f"{summary}\n\n{reason}"
                    result = {"acknowledged": True, "summary": summary}
                    log.info("lean6.finish", turn=turn_num,
                             verifier_rejects=verifier_rejects)
                else:
                    result = {"acknowledged": False, "error": reason}
                    log.info("lean6.finish_blocked_checklist", turn=turn_num,
                             count=checklist.finish_block_count)

            elif tool_name in _GRAPH_TOOL_NAMES:
                from layer45_agent.tools import execute_tool
                result = execute_tool(
                    name=tool_name, args=tool_input,
                    repo_path=Path(repo_path), repo_id=repo_id,
                    modified_files=modified_files, original_files=original_files,
                    sandbox=None,
                )

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
            log.info("lean6.finished", turn=turn_num, files=len(modified_files),
                     verifier_rejects=verifier_rejects)
            break

        if response.stop_reason == "end_turn" and phase == "write":
            log.info("lean6.end_turn", turn=turn_num)
            break

        turn += 1

    total_turns = explore_turns + write_turns
    log.info("lean6.done",
             tier=current_tier, tier_upgraded_from=tier_upgraded_from,
             explore_turns=explore_turns, write_turns=write_turns,
             total_turns=total_turns,
             total_tokens=total_tokens,
             cache_read=total_cache_read, cache_create=total_cache_create,
             files_changed=len(modified_files), finished=finished,
             verifier_rejects=verifier_rejects,
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
        "verifier_rejects": verifier_rejects,
        "verifier_verdicts": verifier_verdicts,
    }
