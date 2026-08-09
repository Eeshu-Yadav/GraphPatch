"""
Graph auto-injection — replaces 13 graph tools with zero-turn context.

Hooks into tool execution: after certain tools return results,
automatically enriches the result with graph data.

No tool calls needed by the agent. No extra turns consumed.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


def inject_graph_context(
    tool_name: str,
    tool_args: dict,
    tool_result: dict,
    repo_id: str,
) -> dict:
    """
    After a tool executes, enrich its result with graph data.
    Returns the modified result dict (or original if no enrichment).

    Injection points:
      find_files → dependencies, callers, tests, risk, coupling
      read_file  → class attributes, hierarchy, methods
      write_file → completeness warnings (handled separately)
    """
    if not isinstance(tool_result, dict):
        return tool_result

    try:
        if tool_name == "find_files":
            found = tool_result.get("files", [])
            if found:
                from lean_agent.graph_context import expand_from_files
                expansion = expand_from_files(found, repo_id)
                if expansion:
                    tool_result["_graph_context"] = expansion
                    log.debug("graph_injection.find_files", files=len(found), context_chars=len(expansion))

        elif tool_name == "read_file":
            fp = tool_args.get("file_path", "")
            if fp:
                from lean_agent.graph_context import expand_from_read
                class_info = expand_from_read(fp, repo_id)
                if class_info:
                    tool_result["_graph_context"] = class_info
                    log.debug("graph_injection.read_file", file=fp, context_chars=len(class_info))

    except Exception as e:
        log.debug("graph_injection.error", tool=tool_name, error=str(e)[:100])

    return tool_result
