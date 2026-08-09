"""
Lean Agent v2 — Single conversation with tool swapping.

Fixes the context loss problem from v1's two-conversation approach.
One conversation, tool set changes mid-stream:

  Turns 1-N:   EXPLORE tools (find_files, search_code, read_file, list_directory,
               file_outline, done_exploring)
  Turns N+1-M: WRITE tools (read_file, write_file, search_code, run_command,
               run_tests, get_diff, finish)

Key: file content read during exploration STAYS in the conversation.
When the agent writes, it has the actual code right there in history.

This matches Claude Code's approach: one conversation, no context loss.
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import time

import structlog
import anthropic

from lean_agent.graph_context import build_graph_context, expand_from_files, expand_from_read, detect_missing_files
from lean_agent.cache import apply_rolling_cache
from lean_agent.agent import (
    EXPLORE_TOOLS, WRITE_TOOLS,
    _summarize_old_results, CompletenessChecklist,
)

log = structlog.get_logger(__name__)


SYSTEM_PROMPT = """You are a senior software engineer fixing a bug in a production codebase.

You work in TWO PHASES in a single conversation:

## Phase 1: EXPLORE (find the files)
You have these tools: find_files, search_code, read_file, list_directory, file_outline, done_exploring

- find_files: Find files by name pattern — START HERE
- search_code: Grep for exact strings/patterns
- read_file: Read file content (this stays in your context for writing later)
- list_directory: Browse directory structure
- file_outline: See structure without full content
- done_exploring: Signal you've found everything needed

RULES:
- Call multiple tools in PARALLEL when independent
- Use find_files BEFORE search_code
- READ the files you'll need to edit — the content stays available for writing
- When ready, call done_exploring with your summary and files to modify

## Phase 2: WRITE (fix the code)
After done_exploring, your tools change to: read_file, write_file, search_code, run_command, run_tests, get_diff, finish

- The files you read in Phase 1 are STILL in your conversation
- You already have the code — WRITE the fix immediately
- Don't re-read files you already read in Phase 1
- Put ALL edits for one file in a single write_file call
- Test after writing
- Call get_diff then finish when done

IMPORTANT: Note key details (line numbers, function signatures) when you read files
in Phase 1 — you'll use them to write precise edits in Phase 2.

{graph_context}

## Ticket
**Title:** {ticket_title}
{ticket_body}"""


def run_lean_agent_v2(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
    repo_path: str,
    api_key: str | None = None,
    max_turns: int = 50,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    """
    Single-conversation agent with tool swapping.
    No context loss between explore and write phases.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    # Phase 0: Build graph context
    log.info("lean2.graph_context", repo_id=repo_id)
    graph_ctx = build_graph_context(ticket_title, ticket_body, repo_id)
    log.info("lean2.graph_context.done", chars=len(graph_ctx))

    system = SYSTEM_PROMPT.format(
        graph_context=graph_ctx if graph_ctx else "(No graph context available)",
        ticket_title=ticket_title,
        ticket_body=ticket_body,
    )

    messages = [
        {"role": "user", "content": "Find and fix the bug. Start with find_files to locate the relevant code."}
    ]

    # State
    phase = "explore"  # "explore" or "write"
    current_tools = EXPLORE_TOOLS
    modified_files = {}
    original_files = {}
    tool_log = []
    total_tokens = 0
    total_cache_read = 0
    explore_turns = 0
    write_turns = 0
    finished = False
    checklist = CompletenessChecklist()
    files_to_modify = []
    explore_summary = ""

    for turn in range(max_turns):
        turn_num = turn + 1

        if phase == "explore":
            explore_turns += 1
        else:
            write_turns += 1

        log.info("lean2.turn", n=turn_num, phase=phase)
        print(f"[v2] turn {turn_num}/{max_turns}  phase={phase}  tokens_so_far={total_tokens:,}", flush=True)

        # Designed caching: roll 3 cache breakpoints on the newest tool_results.
        # Combined with system-prompt caching this drives hit rate from 19% → ~75%.
        # (See lean_agent/cache.py for the full design rationale.)
        apply_rolling_cache(messages)

        response = client.messages.create(
            model=model,
            max_tokens=8192,
            system=[{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=messages,
            tools=current_tools,
            temperature=0.0 if phase == "explore" else 0.1,
        )

        usage = response.usage
        total_tokens += (usage.input_tokens + usage.output_tokens)
        total_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []

        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            tool_log.append({"turn": turn_num, "phase": phase, "tool": tool_name})
            log.info("lean2.tool", tool=tool_name, phase=phase, turn=turn_num)
            print(f"  → {tool_name}({list(tool_input.keys())})", flush=True)

            # Handle done_exploring → SWAP TOOLS
            if tool_name == "done_exploring":
                explore_summary = tool_input.get("summary", "")
                files_to_modify = tool_input.get("files_to_modify", [])
                result = {"acknowledged": True, "files": files_to_modify}

                # SWAP to write tools — same conversation continues
                phase = "write"
                current_tools = WRITE_TOOLS
                log.info("lean2.phase_swap", from_phase="explore", to_phase="write",
                         files=files_to_modify, turn=turn_num)

                # Inject write instructions into the tool result
                result["_instructions"] = (
                    "Phase switched to WRITE. You now have write_file, run_command, "
                    "run_tests, get_diff, finish tools. The files you read above are "
                    "still in your context — write the fix NOW without re-reading."
                )

            # Handle finish
            elif tool_name == "finish":
                summary = tool_input.get("summary", "")
                allowed, reason = checklist.check_finish_allowed(summary)
                if allowed:
                    finished = True
                    if reason:  # released with warnings
                        summary = f"{summary}\n\n{reason}"
                    result = {"acknowledged": True, "summary": summary}
                    log.info("lean2.finish", turn=turn_num, block_count=checklist.finish_block_count)
                else:
                    result = {"acknowledged": False, "error": reason}
                    log.info("lean2.finish_blocked", turn=turn_num,
                             block_count=checklist.finish_block_count)

            # Handle all other tools
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

                # Graph expansion after find_files (deps, callers, tests, risk, coupling)
                if tool_name == "find_files" and isinstance(result, dict):
                    found = result.get("files", [])
                    if found:
                        expansion = expand_from_files(found, repo_id)
                        if expansion:
                            result["_graph_expansion"] = expansion

                # Graph expansion after read_file (class attributes, hierarchy, methods)
                if tool_name == "read_file" and isinstance(result, dict):
                    fp = tool_input.get("file_path", "")
                    if fp:
                        class_info = expand_from_read(fp, repo_id)
                        if class_info:
                            result["_class_details"] = class_info

                # Completeness detection after write_file
                if tool_name == "write_file" and isinstance(result, dict) and result.get("success"):
                    fp = tool_input.get("file_path", "")
                    checklist.on_write_file(fp)
                    content = modified_files.get(fp, "")
                    if fp and content:
                        warnings = detect_missing_files(fp, content, repo_id, modified_files)
                        if warnings:
                            checklist.add_warnings(warnings, fp)
                            msg = checklist.get_checklist_message()
                            if msg:
                                result["_completeness_checklist"] = msg
                            log.info("lean2.completeness_warning", file=fp, count=len(warnings))

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            log.info("lean2.finished", turn=turn_num, files=len(modified_files))
            break

        if response.stop_reason == "end_turn" and phase == "write":
            log.info("lean2.end_turn", turn=turn_num)
            break

    # Results
    total_turns = explore_turns + write_turns
    log.info("lean2.done",
             explore_turns=explore_turns,
             write_turns=write_turns,
             total_turns=total_turns,
             total_tokens=total_tokens,
             files_changed=len(modified_files),
             finished=finished)

    return {
        "success": finished,
        "explore_turns": explore_turns,
        "write_turns": write_turns,
        "total_turns": total_turns,
        "total_tokens": total_tokens,
        "total_cache_read": total_cache_read,
        "explore_tools": [t for t in tool_log if t["phase"] == "explore"],
        "write_tools": [t for t in tool_log if t["phase"] == "write"],
        "files_to_modify": files_to_modify,
        "files_changed": list(modified_files.keys()),
        "explore_summary": explore_summary,
        "graph_context_chars": len(graph_ctx),
        "finished": finished,
    }
