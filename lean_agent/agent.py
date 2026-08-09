"""
Lean Agent — Phase-based tool restriction with auto graph context.

Architecture (inspired by Claude Code's explore/execute split):

  Phase EXPLORE: 6 tools (find_files, search_code, read_file, list_directory,
                          file_outline, batch_read)
                 Graph context injected automatically.
                 Agent finds the right files. No write tools available.

  Phase WRITE:   7 tools (read_file, write_file, search_code, run_command,
                          run_tests, get_diff, finish)
                 Agent writes code, tests, iterates. No search/discovery tools.

Graph tools are NOT exposed as tools. Instead:
  - BEFORE explore: graph_context.build_graph_context() enriches system prompt
  - DURING explore: after find_files, graph_context.expand_from_files() auto-injects
  - Agent sees the ANSWERS, never the tools
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import time

import structlog
import anthropic

from lean_agent.graph_context import build_graph_context, expand_from_files, detect_missing_files


# ═══════════════════════════════════════════════════════════════════════════
# Token efficiency: summarize old tool results to prevent snowball
# ═══════════════════════════════════════════════════════════════════════════

def _summarize_old_results(messages: list[dict], keep_recent: int = 2) -> list[dict]:
    """
    Replace tool results older than `keep_recent` assistant turns
    with short summaries. Prevents the token snowball effect.

    Generalized: summarizes by result SIZE, not by tool type.
    """
    # Find indices of assistant messages (turns)
    assistant_indices = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]

    if len(assistant_indices) <= keep_recent:
        return messages  # Not enough turns to summarize

    # Everything before the last `keep_recent` assistant turns gets summarized
    cutoff_idx = assistant_indices[-keep_recent]

    for i in range(len(messages)):
        if i >= cutoff_idx:
            break  # Keep recent messages intact

        msg = messages[i]
        if msg.get("role") != "user":
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        # Summarize tool_result blocks that are large
        new_content = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                new_content.append(block)
                continue

            result_text = block.get("content", "")
            if isinstance(result_text, str) and len(result_text) > 500:
                # Summarize: keep first 200 chars + size info
                summary = result_text[:200].replace("\n", " ").strip()
                block = {**block, "content": f"[summarized: {len(result_text)} chars] {summary}..."}

            new_content.append(block)

        messages[i] = {**msg, "content": new_content}

    return messages


# ═══════════════════════════════════════════════════════════════════════════
# Completeness checklist: block finish() until warnings addressed
# ═══════════════════════════════════════════════════════════════════════════

class CompletenessChecklist:
    """
    Tracks completeness warnings with a 3-state tier-aware circuit breaker.

    STRICT (blocks 1..strict_until):           Block finish, tell agent to fix
    ESCALATED (blocks strict_until+1..release_at-1): Suggest different approach
    RELEASED (block release_at+):              Allow finish, append unresolved warnings

    Tier-aware release_at — observed in our v5 batches that agents trying vague
    tickets give up on end_turn after 2-4 blocked finish attempts. Lower release
    thresholds for easier tiers prevent the "agent quits silently" failure mode.

    Based on:
    - Claude Code Auto Mode: 3 consecutive blocks → pause to manual
    - GIRA Architecture: block, rewrite, or escalate
    - Azure Circuit Breaker: CLOSED → OPEN → HALF-OPEN states
    """

    # Release thresholds per tier — lowered after observing 4/13 v5 runs hit
    # end_turn before the prior 5-block threshold was reached.
    _RELEASE_AT = {
        "easy":   2,    # release on the 2nd blocked attempt
        "medium": 3,    # release on the 3rd
        "hard":   4,    # release on the 4th (more leniency for genuinely hard issues)
    }
    _STRICT_UNTIL = {"easy": 1, "medium": 1, "hard": 2}   # strict for first N attempts

    def __init__(self, tier: str = "medium"):
        self.items: list[dict] = []
        self.injected: bool = False
        self.finish_block_count: int = 0
        self.tier: str = tier
        self.release_at: int = self._RELEASE_AT.get(tier, 3)
        self.strict_until: int = self._STRICT_UNTIL.get(tier, 1)

    def add_warnings(self, warnings: list[str], file_path: str):
        """Add warnings from completeness detection."""
        import re
        for w in warnings:
            file_hints = re.findall(r'`([^`]+\.\w{1,4})`', w)
            file_hints += re.findall(r'`([^`]+/[^`]+)`', w)
            self.items.append({
                "warning": w[:200],
                "file_hints": list(set(file_hints)),
                "source_file": file_path,
                "resolved": False,
            })

    def on_write_file(self, file_path: str):
        """Check if a write_file resolves any checklist items."""
        for item in self.items:
            for hint in item["file_hints"]:
                if hint in file_path or file_path.endswith(hint.split("/")[-1]):
                    item["resolved"] = True
                    # Reset block count when agent makes progress
                    self.finish_block_count = max(0, self.finish_block_count - 1)

    def get_unresolved(self) -> list[dict]:
        return [item for item in self.items if not item["resolved"]]

    def get_checklist_message(self) -> str | None:
        if not self.items:
            return None
        lines = ["## Completeness Checklist (address before finishing)\n"]
        for item in self.items:
            status = "✅" if item["resolved"] else "☐"
            lines.append(f"{status} {item['warning']}")
        lines.append("\nAddress each ☐ item (fix with write_file or explain why not needed), then call finish().")
        return "\n".join(lines)

    def check_finish_allowed(self, finish_summary: str = "") -> tuple[bool, str]:
        """
        3-state circuit breaker for finish gate.
        Returns (allowed, message).
        """
        unresolved = self.get_unresolved()
        if not unresolved:
            return True, ""

        self.finish_block_count += 1
        items_text = "\n".join(f"  ☐ {item['warning']}" for item in unresolved)

        # STATE 1: STRICT — tell agent to fix
        if self.finish_block_count <= self.strict_until:
            return False, (
                f"BLOCKED: {len(unresolved)} completeness warning(s) not addressed:\n"
                f"{items_text}\n\n"
                f"Fix each with write_file:\n"
                f"  1. Read the flagged file with read_file\n"
                f"  2. Understand the pattern (e.g., how other modules are registered)\n"
                f"  3. Apply the same pattern with write_file\n"
                f"Then call finish() again."
            )

        # STATE 2: ESCALATED — suggest different approach
        if self.finish_block_count < self.release_at:
            file_hints = []
            for item in unresolved:
                file_hints.extend(item.get("file_hints", []))
            hint_text = ", ".join(f"`{h}`" for h in file_hints[:3]) if file_hints else "the flagged files"

            return False, (
                f"BLOCKED (attempt {self.finish_block_count}/{self.release_at}). You haven't fixed these:\n"
                f"{items_text}\n\n"
                f"Try a DIFFERENT approach:\n"
                f"  1. read_file({hint_text}) to see the actual content\n"
                f"  2. Look at how OTHER files in that directory are handled\n"
                f"  3. Search for the import/registration pattern with search_code\n"
                f"  4. If the warning truly doesn't apply, include an explanation in your finish summary\n"
                f"  (next blocked attempt will be RELEASED with documented gaps)"
            )

        # STATE 3: RELEASED — let through with documented gaps
        gap_text = "\n".join(f"  ⚠ UNRESOLVED: {item['warning']}" for item in unresolved)
        return True, (
            f"RELEASED after {self.finish_block_count} blocked attempts (tier={self.tier}). "
            f"{len(unresolved)} unresolved warning(s) documented:\n{gap_text}"
        )

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Tool definitions — minimal, per phase
# ═══════════════════════════════════════════════════════════════════════════

EXPLORE_TOOLS = [
    {
        "name": "find_files",
        "description": "Find files by name/glob pattern. ALWAYS start here. Examples: find_files('*validator*'), find_files('*transform*')",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match file names"},
                "path": {"type": "string", "description": "Directory to search in (optional)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "search_code",
        "description": "Search file contents with regex (like grep). Use for exact strings, imports, class names.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "file_glob": {"type": "string", "description": "File pattern filter (e.g. '*.py')"},
                "max_results": {"type": "integer", "description": "Maximum results (default 20)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file's contents. Use start_line/end_line for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "start_line": {"type": "integer", "description": "Start line (optional)"},
                "end_line": {"type": "integer", "description": "End line (optional)"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory. Use to understand project structure.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "pattern": {"type": "string", "description": "Filter pattern (optional)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_outline",
        "description": "See imports, classes, functions WITHOUT full content. 10x fewer tokens than read_file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "done_exploring",
        "description": "Signal that you've found all the files you need. Describe what you found and what changes are needed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "What you found and what needs to change"},
                "files_to_modify": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of file paths that need modification",
                },
            },
            "required": ["summary", "files_to_modify"],
        },
    },
]

WRITE_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file before editing it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Apply edits to a file using search-and-replace. ALL edits for one file in one call.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "search": {"type": "string"},
                            "replace": {"type": "string"},
                        },
                        "required": ["search", "replace"],
                    },
                },
            },
            "required": ["file_path", "edits"],
        },
    },
    {
        "name": "search_code",
        "description": "Search file contents with regex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "file_glob": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command (tests, build, scripts). Timeout: 60s.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run the project's test suite. Auto-detects test framework.",
        "input_schema": {
            "type": "object",
            "properties": {
                "test_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific test files to run (optional)",
                },
            },
        },
    },
    {
        "name": "get_diff",
        "description": "Show all changes as unified diff. Call before finish.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "finish",
        "description": "Signal completion. Include summary of what changed and why.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Summary of changes made"},
            },
            "required": ["summary"],
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════
# System prompts — per phase
# ═══════════════════════════════════════════════════════════════════════════

EXPLORE_PROMPT = """You are a code exploration specialist. Your job is to find the files that need
to be modified to address the ticket below.

You have 6 tools. Use them efficiently:
- find_files: Find files by name pattern — ALWAYS START HERE
- search_code: Grep for exact strings/patterns
- read_file: Read specific file content
- list_directory: Browse directory structure
- file_outline: See file structure without full content (saves tokens)
- done_exploring: Signal when you've found all relevant files

RULES:
- Call multiple tools in PARALLEL when they're independent
- Use find_files BEFORE search_code (file names are faster than content search)
- Use file_outline BEFORE read_file (understand structure first)
- You are READ-ONLY — you cannot modify files in this phase
- Be FAST — find the files and signal done_exploring
- The graph context below shows relationships between files — USE IT

When you've found all files that need changes, call done_exploring with:
1. A summary of what you found
2. The list of files that need modification

{graph_context}

## Ticket
**Title:** {ticket_title}
{ticket_body}"""

WRITE_PROMPT = """You are implementing changes based on the exploration below.

The files that need modification have been identified. Your job is to:
1. Read each file that needs changes
2. Write the changes using write_file
3. Test your changes with run_command or run_tests
4. Review with get_diff
5. Call finish when done

RULES:
- Read a file BEFORE editing it
- Put ALL edits for one file in a SINGLE write_file call
- Test after writing — don't just assume it works
- Call get_diff before finish to review your changes
- Call multiple tools in PARALLEL when independent
- When you read a file, NOTE any important details (function signatures, class attributes)
  in your response — old tool results will be cleared to save context space
- If you see a COMPLETENESS CHECKLIST after write_file, you MUST address each item
  before calling finish(). Either fix it with write_file or explain why it's not needed.

## Exploration Summary
{explore_summary}

## Files to Modify
{files_to_modify}

## Graph Context (dependencies and relationships)
{graph_context}

## Ticket
**Title:** {ticket_title}
{ticket_body}"""


# ═══════════════════════════════════════════════════════════════════════════
# Agent loop
# ═══════════════════════════════════════════════════════════════════════════

def run_lean_agent(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
    repo_path: str,
    api_key: str | None = None,
    max_explore_turns: int = 20,
    max_write_turns: int = 40,
    explore_model: str = "claude-sonnet-4-20250514",
    write_model: str = "claude-sonnet-4-20250514",
) -> dict:
    """
    Run the lean agent with phase-based tool restriction.

    Phase 1 (EXPLORE): Find files with 6 tools + auto graph context
    Phase 2 (WRITE): Modify files with 7 tools

    Returns dict with results, turns, tokens, files changed.
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    # ── Phase 0: Build graph context (no LLM, no tools) ────────────────
    log.info("lean.phase0.graph_context", repo_id=repo_id)
    graph_ctx = build_graph_context(ticket_title, ticket_body, repo_id)
    log.info("lean.phase0.done", context_chars=len(graph_ctx))

    # ── Phase 1: EXPLORE ────────────────────────────────────────────────
    log.info("lean.phase1.explore.start", model=explore_model, max_turns=max_explore_turns)

    explore_system = EXPLORE_PROMPT.format(
        graph_context=graph_ctx if graph_ctx else "(No graph context available — use find_files and search_code)",
        ticket_title=ticket_title,
        ticket_body=ticket_body,
    )

    explore_messages = [
        {"role": "user", "content": "Find the files that need to be modified. Start with find_files patterns based on the ticket keywords and graph context."}
    ]

    explore_summary = ""
    files_to_modify = []
    explore_turns = 0
    explore_tokens = 0
    explore_cache_read = 0
    explore_tool_log = []

    for turn in range(max_explore_turns):
        explore_turns += 1
        log.info("lean.explore.turn", n=explore_turns)

        # Rolling message cache — see lean_agent/cache.py for design rationale.
        from lean_agent.cache import apply_rolling_cache
        apply_rolling_cache(explore_messages)

        response = client.messages.create(
            model=explore_model,
            max_tokens=4096,
            system=[{
                "type": "text",
                "text": explore_system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=explore_messages,
            tools=EXPLORE_TOOLS,
            temperature=0.0,
        )

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        explore_tokens += (usage.input_tokens + usage.output_tokens)
        explore_cache_read += cache_read

        # Process response
        assistant_content = response.content
        explore_messages.append({"role": "assistant", "content": assistant_content})

        # Check for tool calls
        tool_results = []
        done = False

        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            explore_tool_log.append(tool_name)
            log.info("lean.explore.tool", tool=tool_name, turn=explore_turns)

            # Execute tool
            from layer45_agent.tools import execute_tool
            result = execute_tool(
                name=tool_name,
                args=tool_input,
                repo_path=Path(repo_path),
                repo_id=repo_id,
                modified_files={},
                original_files={},
                sandbox=None,
            )

            # Auto-expand with graph after find_files
            if tool_name == "find_files" and isinstance(result, dict):
                found_files = result.get("files", [])
                if found_files:
                    expansion = expand_from_files(found_files, repo_id)
                    if expansion:
                        result["_graph_expansion"] = expansion

            # Handle done_exploring
            if tool_name == "done_exploring":
                explore_summary = tool_input.get("summary", "")
                files_to_modify = tool_input.get("files_to_modify", [])
                result = {"acknowledged": True, "files": files_to_modify}
                done = True

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            explore_messages.append({"role": "user", "content": tool_results})

        if done:
            log.info("lean.explore.done",
                     turns=explore_turns,
                     files=len(files_to_modify),
                     summary=explore_summary[:200])
            break

        if response.stop_reason == "end_turn":
            # Agent finished without calling done_exploring — extract from text
            for block in assistant_content:
                if hasattr(block, "text") and block.text:
                    explore_summary = block.text[:500]
            log.info("lean.explore.end_turn", turns=explore_turns)
            break

    # ── Phase 2: WRITE ──────────────────────────────────────────────────
    log.info("lean.phase2.write.start",
             model=write_model,
             max_turns=max_write_turns,
             files=files_to_modify)

    files_list = "\n".join(f"- `{f}`" for f in files_to_modify) if files_to_modify else "(none identified)"

    write_system = WRITE_PROMPT.format(
        explore_summary=explore_summary,
        files_to_modify=files_list,
        graph_context=graph_ctx if graph_ctx else "",
        ticket_title=ticket_title,
        ticket_body=ticket_body,
    )

    write_messages = [
        {"role": "user", "content": "Implement the changes. Start by reading the files listed above, then write your changes."}
    ]

    modified_files = {}
    original_files = {}
    write_turns = 0
    write_tokens = 0
    write_cache_read = 0
    write_tool_log = []
    finished = False
    checklist = CompletenessChecklist()

    for turn in range(max_write_turns):
        write_turns += 1
        log.info("lean.write.turn", n=write_turns)

        # Rolling message cache — see lean_agent/cache.py for design rationale.
        from lean_agent.cache import apply_rolling_cache
        apply_rolling_cache(write_messages)

        response = client.messages.create(
            model=write_model,
            max_tokens=8192,
            system=[{
                "type": "text",
                "text": write_system,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=write_messages,
            tools=WRITE_TOOLS,
            temperature=0.1,
        )

        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        write_tokens += (usage.input_tokens + usage.output_tokens)
        write_cache_read += cache_read

        assistant_content = response.content
        write_messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []

        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            write_tool_log.append(tool_name)
            log.info("lean.write.tool", tool=tool_name, turn=write_turns)

            if tool_name == "finish":
                # ── Checklist gate: 3-state circuit breaker ──
                summary = tool_input.get("summary", "")
                allowed, reason = checklist.check_finish_allowed(summary)
                if allowed:
                    finished = True
                    # If released with warnings, append them to summary
                    if reason:
                        summary = f"{summary}\n\n{reason}"
                    result = {"acknowledged": True, "summary": summary}
                    log.info("lean.finish_allowed",
                             block_count=checklist.finish_block_count,
                             unresolved=len(checklist.get_unresolved()))
                else:
                    result = {"acknowledged": False, "error": reason}
                    log.info("lean.finish_blocked",
                             block_count=checklist.finish_block_count,
                             unresolved=len(checklist.get_unresolved()),
                             state="strict" if checklist.finish_block_count <= 2
                                   else "escalated" if checklist.finish_block_count <= 4
                                   else "released")
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

            # ── File completeness detection (after write_file) ──────
            if tool_name == "write_file" and isinstance(result, dict) and result.get("success"):
                fp = tool_input.get("file_path", "")
                content = modified_files.get(fp, "")

                # Update checklist: check if this write resolves any warning
                checklist.on_write_file(fp)

                if fp and content:
                    completeness_warnings = detect_missing_files(
                        new_file_path=fp,
                        new_file_content=content,
                        repo_id=repo_id,
                        modified_files=modified_files,
                    )
                    if completeness_warnings:
                        checklist.add_warnings(completeness_warnings, fp)
                        # Inject checklist into result so agent sees it
                        checklist_msg = checklist.get_checklist_message()
                        if checklist_msg:
                            result["_completeness_checklist"] = checklist_msg
                        log.info("lean.completeness_warning",
                                 file=fp,
                                 warnings=len(completeness_warnings))

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            write_messages.append({"role": "user", "content": tool_results})

        if finished:
            log.info("lean.write.finished", turns=write_turns, files=len(modified_files))
            break

        if response.stop_reason == "end_turn":
            log.info("lean.write.end_turn", turns=write_turns)
            break

    # ── Results ─────────────────────────────────────────────────────────
    total_turns = explore_turns + write_turns
    total_tokens = explore_tokens + write_tokens

    log.info("lean.done",
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
        "explore_tokens": explore_tokens,
        "write_tokens": write_tokens,
        "total_tokens": total_tokens,
        "explore_cache_read": explore_cache_read,
        "write_cache_read": write_cache_read,
        "total_cache_read": explore_cache_read + write_cache_read,
        "explore_tools": explore_tool_log,
        "write_tools": write_tool_log,
        "files_to_modify": files_to_modify,
        "files_changed": list(modified_files.keys()),
        "explore_summary": explore_summary,
        "graph_context_chars": len(graph_ctx),
    }
