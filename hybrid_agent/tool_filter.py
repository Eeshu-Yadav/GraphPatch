"""
Phase-based tool filtering — the core of the hybrid approach.

Instead of giving the agent 35 tools all the time, restrict based on phase:
  EXPLORING: 8 tools (find, search, read, outline, list, batch_read, think, done_exploring)
  WRITING:   9 tools (read, write, search, run_command, run_tests, get_diff, finish, think, undo_edit)

Graph tools (13) are REMOVED entirely — they run as auto-injections instead.
"""
from __future__ import annotations

# Tools available during exploration (Claude Code approach: find → read → understand)
EXPLORE_TOOL_NAMES = {
    "find_files",
    "search_code",
    "read_file",
    "file_outline",
    "list_directory",
    "batch_read",
    "think",
    "done_exploring",  # signals transition to write phase
}

# Tools available during writing (write → test → verify → finish)
WRITE_TOOL_NAMES = {
    "read_file",
    "write_file",
    "search_code",
    "run_command",
    "run_tests",
    "get_diff",
    "finish",
    "think",
    "undo_edit",
}

# Graph tools that should NEVER be in the tool list
# (they run automatically as context injection)
GRAPH_TOOL_NAMES = {
    "search_symbols",
    "get_callers",
    "get_impact",
    "get_dependencies",
    "get_test_coverage",
    "get_coupled_files",
    "get_risk_score",
    "get_reviewers",
    "get_top_files",
    "get_file_info",
    "get_symbol_details",
    "get_class_hierarchy",
    "get_change_context",
}


def filter_tools_for_phase(all_tools: list[dict], phase: str) -> list[dict]:
    """
    Filter the tool definitions list based on the current phase.

    Args:
        all_tools: Full list of tool definitions (from ALL_TOOL_DEFS)
        phase: "EXPLORING", "WRITING", "VERIFYING", or "FINISHING"

    Returns:
        Filtered tool list with only phase-appropriate tools
    """
    if phase == "EXPLORING":
        allowed = EXPLORE_TOOL_NAMES
    elif phase in ("WRITING", "VERIFYING"):
        allowed = WRITE_TOOL_NAMES
    elif phase == "FINISHING":
        # Only finish tools
        allowed = {"get_diff", "finish", "think"}
    else:
        # Unknown phase — give write tools as safe default
        allowed = WRITE_TOOL_NAMES

    # Filter: keep tools in allowed set, exclude graph tools always
    return [
        t for t in all_tools
        if _get_tool_name(t) in allowed and _get_tool_name(t) not in GRAPH_TOOL_NAMES
    ]


def _get_tool_name(tool_def: dict) -> str:
    """Extract tool name from definition (handles both formats)."""
    return tool_def.get("name", tool_def.get("function", {}).get("name", ""))
