"""
Lean Agent v3 — v2 + graph tools during write phase.

EXPLORE: 6 tools (find, search, read, list, outline, done_exploring)
         + auto graph injection after find_files and read_file

WRITE:   7 write tools + 5 graph tools = 12 tools
         write_file, read_file, search_code, run_command, run_tests, get_diff, finish
         + get_impact, get_callers, get_dependencies, get_test_coverage, get_risk_score

         Agent can check blast radius BEFORE writing.
         Agent can find callers BEFORE changing a function signature.
         Agent can find tests BEFORE running them.

Single conversation — no context loss.
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


# Graph tools available during WRITE phase only
GRAPH_TOOLS_FOR_WRITE = [
    {
        "name": "get_impact",
        "description": "BLAST RADIUS: What breaks if you change this function/class? Shows all callers up to N hops. Call BEFORE modifying a function.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string", "description": "Function or class name"},
                "depth": {"type": "integer", "description": "How many hops (default 2)"},
            },
            "required": ["symbol_name"],
        },
    },
    {
        "name": "get_callers",
        "description": "Who calls this function? Shows direct and transitive callers with file locations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_name": {"type": "string", "description": "Function name"},
                "depth": {"type": "integer", "description": "Hops (default 1)"},
            },
            "required": ["symbol_name"],
        },
    },
    {
        "name": "get_dependencies",
        "description": "What does this file import? What imports this file? Shows the dependency graph.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_test_coverage",
        "description": "Which test files cover this source file? Use to know what tests to run after changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Source file path"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "get_risk_score",
        "description": "How risky is changing this file? Shows centrality, number of dependents, test coverage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "File path"},
            },
            "required": ["file_path"],
        },
    },
]

# Combined write tools
WRITE_TOOLS_WITH_GRAPH = WRITE_TOOLS + GRAPH_TOOLS_FOR_WRITE


SYSTEM_PROMPT = """You are a senior software engineer fixing a bug in a production codebase.

You work in TWO PHASES in a single conversation:

## Phase 1: EXPLORE (find the files)
Tools: find_files, search_code, read_file, list_directory, file_outline, done_exploring

- find_files FIRST, search_code SECOND, read_file for details
- Call multiple tools in PARALLEL when independent
- READ the files you'll edit — the content stays for writing
- Call done_exploring when ready

## Phase 2: WRITE (fix the code)
After done_exploring, your tools change. You now have:

WRITE tools: read_file, write_file, search_code, run_command, run_tests, get_diff, finish
GRAPH tools: get_impact, get_callers, get_dependencies, get_test_coverage, get_risk_score

IMPORTANT — Before modifying a function:
- Call get_impact(symbol) to see what breaks (blast radius)
- Call get_callers(symbol) to see who uses it
- Call get_test_coverage(file) to know which tests to run

After writing:
- Test your changes
- Call get_diff then finish

Note key details (line numbers, function names) when reading files — you'll need them for precise edits.
Old tool results will be cleared to save context — write down what matters.

{graph_context}

## Ticket
**Title:** {ticket_title}
{ticket_body}"""


def run_lean_agent_v3(
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
    Explore: 6 tools. Write: 12 tools (7 write + 5 graph).
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    # Phase 0: Graph context
    log.info("lean3.graph_context", repo_id=repo_id)
    graph_ctx = build_graph_context(ticket_title, ticket_body, repo_id)
    log.info("lean3.graph_context.done", chars=len(graph_ctx))

    system = SYSTEM_PROMPT.format(
        graph_context=graph_ctx if graph_ctx else "(No graph context available)",
        ticket_title=ticket_title,
        ticket_body=ticket_body,
    )

    messages = [
        {"role": "user", "content": "Find and fix the bug. Start with find_files to locate the relevant code."}
    ]

    phase = "explore"
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

        log.info("lean3.turn", n=turn_num, phase=phase)
        print(f"[v3] turn {turn_num}/{max_turns}  phase={phase}  tokens_so_far={total_tokens:,}", flush=True)

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
            log.info("lean3.tool", tool=tool_name, phase=phase, turn=turn_num)
            print(f"  → {tool_name}({list(tool_input.keys())})", flush=True)

            # Handle done_exploring → SWAP to write + graph tools
            if tool_name == "done_exploring":
                explore_summary = tool_input.get("summary", "")
                files_to_modify = tool_input.get("files_to_modify", [])
                result = {"acknowledged": True, "files": files_to_modify}

                phase = "write"
                current_tools = WRITE_TOOLS_WITH_GRAPH  # 7 write + 5 graph
                log.info("lean3.phase_swap", to="write+graph",
                         files=files_to_modify, turn=turn_num,
                         tools=len(current_tools))

                result["_instructions"] = (
                    "Phase switched to WRITE. You now have write + graph tools.\n"
                    "BEFORE modifying a function: call get_impact(symbol) for blast radius.\n"
                    "BEFORE running tests: call get_test_coverage(file) to find test files.\n"
                    "The files you read above are still in context — write the fix."
                )

            # Handle finish with circuit breaker
            elif tool_name == "finish":
                summary = tool_input.get("summary", "")
                allowed, reason = checklist.check_finish_allowed(summary)
                if allowed:
                    finished = True
                    if reason:
                        summary = f"{summary}\n\n{reason}"
                    result = {"acknowledged": True, "summary": summary}
                    log.info("lean3.finish", turn=turn_num)
                else:
                    result = {"acknowledged": False, "error": reason}
                    log.info("lean3.finish_blocked", turn=turn_num,
                             count=checklist.finish_block_count)

            # Handle graph tools (execute via layer45 graph queries)
            elif tool_name in ("get_impact", "get_callers", "get_dependencies",
                               "get_test_coverage", "get_risk_score"):
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
                log.info("lean3.graph_tool", tool=tool_name, turn=turn_num)

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

                # Auto graph injection after find_files
                if tool_name == "find_files" and isinstance(result, dict):
                    found = result.get("files", [])
                    if found:
                        expansion = expand_from_files(found, repo_id)
                        if expansion:
                            result["_graph_context"] = expansion

                # Auto graph injection after read_file
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
                            log.info("lean3.completeness", file=fp, count=len(warnings))

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if finished:
            log.info("lean3.finished", turn=turn_num, files=len(modified_files))
            break

        if response.stop_reason == "end_turn" and phase == "write":
            log.info("lean3.end_turn", turn=turn_num)
            break

    total_turns = explore_turns + write_turns
    log.info("lean3.done",
             explore_turns=explore_turns, write_turns=write_turns,
             total_turns=total_turns, total_tokens=total_tokens,
             files_changed=len(modified_files), finished=finished)

    return {
        "success": finished,
        "explore_turns": explore_turns,
        "write_turns": write_turns,
        "total_turns": total_turns,
        "total_tokens": total_tokens,
        "total_cache_read": total_cache_read,
        "explore_tools": [t for t in tool_log if t["phase"] == "explore"],
        "write_tools": [t for t in tool_log if t["phase"] == "write"],
        "graph_tools_used": [t for t in tool_log if t["tool"] in
                            ("get_impact", "get_callers", "get_dependencies",
                             "get_test_coverage", "get_risk_score")],
        "files_to_modify": files_to_modify,
        "files_changed": list(modified_files.keys()),
        "explore_summary": explore_summary,
        "graph_context_chars": len(graph_ctx),
        "finished": finished,
    }
