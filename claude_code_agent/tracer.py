"""
Session tracer for Claude Code agent runs.

Since Claude Code manages its own ReAct loop, we can't trace individual Claude API turns.
What we CAN trace is every MCP tool call Claude Code makes to our graph server:
- Which tools were called, with what args
- What results were returned (truncated)
- Latency per call
- Session-level metrics: total tools, files discovered, graph queries

This lets you compare sessions: "did it find the right files?", "did it check impact?",
"how many graph calls vs file reads?", "total wall time?"

Traces are written to: claude_code_agent/traces/

Usage from the MCP server:
    tracer.start_session(repo_id, title)
    tracer.log_tool_call("get_change_context", args, result, duration_ms)
    tracer.finish_session(success=True)
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

TRACE_DIR = Path(__file__).parent / "traces"

# Module-level session state (one session at a time per MCP server process)
_session: dict | None = None
_session_start: float = 0


def start_session(repo_id: str, title: str, ticket_id: str = "") -> None:
    """Start tracing a new Claude Code session."""
    global _session, _session_start
    TRACE_DIR.mkdir(parents=True, exist_ok=True)

    _session_start = time.time()
    _session = {
        "repo_id": repo_id,
        "ticket_id": ticket_id or "unknown",
        "title": title[:200],
        "started_at": datetime.now().isoformat(),
        "tool_calls": [],
        "summary": {},
    }


def log_tool_call(
    tool_name: str,
    args: dict,
    result_preview: str,
    duration_ms: int,
) -> None:
    """Log a single MCP tool call from Claude Code."""
    if not _session:
        return

    _session["tool_calls"].append({
        "tool": tool_name,
        "args": _truncate_args(args),
        "result_preview": result_preview[:1000],
        "duration_ms": duration_ms,
        "timestamp": datetime.now().isoformat(),
    })


def finish_session(
    success: bool = True,
    files_changed: list[str] | None = None,
    pr_url: str = "",
    error: str = "",
) -> str | None:
    """Finalize and write the session trace. Returns the trace file path."""
    global _session
    if not _session:
        return None

    wall_time_s = time.time() - _session_start

    # Build summary metrics
    tool_counts: dict[str, int] = {}
    total_duration_ms = 0
    files_discovered: set[str] = set()

    for tc in _session["tool_calls"]:
        tool = tc["tool"]
        tool_counts[tool] = tool_counts.get(tool, 0) + 1
        total_duration_ms += tc.get("duration_ms", 0)

        # Track files mentioned in args
        for key in ("file_path", "file_paths"):
            if key in tc["args"]:
                val = tc["args"][key]
                if isinstance(val, str):
                    files_discovered.add(val)
                elif isinstance(val, list):
                    files_discovered.update(v for v in val if isinstance(v, str))

    # Categorize tool calls
    context_tools = {"assemble_context", "build_graph_context"}
    graph_tools = {"search_symbols", "get_change_context", "get_callers", "get_impact",
                   "get_dependencies", "get_test_coverage", "get_coupled_files",
                   "get_risk_score", "get_class_hierarchy"}
    infra_tools = {"index_repo", "get_pipeline_status"}

    _session["finished_at"] = datetime.now().isoformat()
    _session["summary"] = {
        "success": success,
        "wall_time_s": round(wall_time_s, 1),
        "total_tool_calls": len(_session["tool_calls"]),
        "total_graph_latency_ms": total_duration_ms,
        "tool_counts": tool_counts,
        "context_calls": sum(tool_counts.get(t, 0) for t in context_tools),
        "graph_calls": sum(tool_counts.get(t, 0) for t in graph_tools),
        "infra_calls": sum(tool_counts.get(t, 0) for t in infra_tools),
        "files_discovered": sorted(files_discovered),
        "files_changed": files_changed or [],
        "pr_url": pr_url,
        "error": error,
    }

    # Write to disk
    ticket_slug = _session["ticket_id"].replace("/", "-").replace(" ", "_")[:40]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{ticket_slug}.json"
    path = TRACE_DIR / filename

    with open(path, "w") as f:
        json.dump(_session, f, indent=2, default=str)

    _session = None
    return str(path)


def get_current_summary() -> dict | None:
    """Get summary of the current in-progress session (for live monitoring)."""
    if not _session:
        return None

    wall_time_s = time.time() - _session_start
    tool_counts: dict[str, int] = {}
    for tc in _session["tool_calls"]:
        tool = tc["tool"]
        tool_counts[tool] = tool_counts.get(tool, 0) + 1

    return {
        "repo_id": _session["repo_id"],
        "ticket_id": _session["ticket_id"],
        "title": _session["title"],
        "wall_time_s": round(wall_time_s, 1),
        "total_tool_calls": len(_session["tool_calls"]),
        "tool_counts": tool_counts,
        "last_tool": _session["tool_calls"][-1]["tool"] if _session["tool_calls"] else None,
        "started_at": _session["started_at"],
    }


def _truncate_args(args: dict) -> dict:
    """Truncate large arg values for trace readability."""
    result = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 500:
            result[k] = v[:500] + f"... [{len(v)} chars]"
        elif isinstance(v, list) and len(v) > 20:
            result[k] = v[:20] + [f"... [{len(v)} items]"]
        else:
            result[k] = v
    return result
