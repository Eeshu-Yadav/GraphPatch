"""
Claude Code Agent — MCP Server for Knowledge Graph Tools.

Exposes the codebase knowledge graph (Memgraph + Qdrant) as individual,
composable MCP tools that Claude Code calls directly via its native tool-use.

Instead of wrapping the entire pipeline in a single run_pipeline_pr call,
each graph capability is a separate tool. Claude Code orchestrates them
with its native Read/Write/Edit/Bash/Grep/Glob tools.

Tools:
  Context Assembly:
    - assemble_context    — Full L3 multi-strategy retrieval + RRF fusion
    - build_graph_context — Pure graph keyword lookup (no LLM, fast)

  Graph Query (on-demand deep-dives):
    - search_symbols      — Semantic vector search (Qdrant)
    - get_change_context  — Composite pre-change analysis (risk + deps + tests + coupling + impact)
    - get_callers         — Transitive callers from Memgraph
    - get_impact          — will_break / may_break analysis
    - get_dependencies    — Import graph (deps + dependents)
    - get_test_coverage   — TEST_FOR edges
    - get_coupled_files   — Git co-change analysis
    - get_risk_score      — Composite risk number
    - get_class_hierarchy — Inheritance tree

  Indexing:
    - index_repo          — Clone + index a new repo
    - get_pipeline_status — Health check all services

Usage (stdio transport, auto-started by Claude Code):
    python -m claude-code-agent.server
"""
from __future__ import annotations

import json
import os
import sys
import logging
import subprocess
import traceback

from pathlib import Path

# Load .env from monorepo root
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(_env_path))

# CRITICAL: MCP uses stdout for JSON-RPC — redirect ALL logging to stderr
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

import structlog
structlog.configure(
    processors=[structlog.dev.ConsoleRenderer(colors=False)],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

log = structlog.get_logger(__name__)

from claude_code_agent import tracer

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="codebase-graph",
    instructions=(
        "This server provides knowledge graph tools for codebase analysis. "
        "Use assemble_context to get a rich context map for any ticket. "
        "Use individual graph tools (get_change_context, get_impact, etc.) for deep-dives. "
        "Use index_repo to index a new repository before querying. "
        "Use start_session/finish_session to trace your work for monitoring."
    ),
)


def _traced(tool_name: str, args: dict, fn):
    """Wrapper that traces a tool call with timing."""
    import time as _time
    start = _time.time()
    result = fn()
    duration_ms = int((_time.time() - start) * 1000)
    preview = result[:500] if isinstance(result, str) else str(result)[:500]
    tracer.log_tool_call(tool_name, args, preview, duration_ms)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# CONTEXT ASSEMBLY — replaces 20+ turns of manual exploration
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def assemble_context(
    repo_id: str,
    title: str,
    body: str,
) -> str:
    """
    Assemble rich context for a ticket from the knowledge graph.

    Runs multi-strategy retrieval (intent classification + semantic search + keyword extraction),
    fuses results with Reciprocal Rank Fusion (intent gets 3x weight), then enriches with
    call graph, dependencies, test coverage, git coupling, and impact analysis.

    Returns a markdown context map with: relevant symbols, files, call graph,
    dependencies, test files, coupled files, and risk assessment.

    Call this FIRST before exploring a codebase for any ticket. It replaces
    20+ turns of manual exploration with a single pre-computed context injection.

    Args:
        repo_id: GitHub repo in owner/repo format (e.g. "django/django")
        title: Issue title
        body: Issue description
    """
    try:
        from layer3_context.models.ticket import Ticket
        from layer3_context.assembly.assembler import assemble

        ticket = Ticket(ticket_id="CLAUDE-CODE", title=title, body=body, repo_id=repo_id)
        bundle = assemble(ticket, max_symbols=15, max_files=8)
        return bundle.to_prompt_text(max_symbols=15, max_files=8)
    except Exception:
        return f"Context assembly failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def build_graph_context(
    repo_id: str,
    title: str,
    body: str,
) -> str:
    """
    Build a pre-computed context block from the knowledge graph using ticket keywords.

    Unlike assemble_context (which uses LLM intent classification), this is pure graph
    queries with zero LLM calls. Faster and cheaper. Returns markdown describing:
    - Files matching ticket keywords (by filename)
    - Dependencies (who imports them, what they import)
    - Test files covering those sources
    - High-centrality files in relevant directories (by PageRank)
    - Registration/init files that may need updates

    Use this as a quick alternative to assemble_context, or combine both.

    Args:
        repo_id: GitHub repo in owner/repo format
        title: Issue title
        body: Issue description
    """
    try:
        from lean_agent.graph_context import build_graph_context as _build

        result = _build(title, body, repo_id)
        return result if result else "No graph context found for these keywords."
    except Exception:
        return f"Graph context failed:\n```\n{traceback.format_exc()}\n```"


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH QUERY TOOLS — on-demand deep-dives
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def search_symbols(
    query: str,
    repo_id: str,
    limit: int = 10,
) -> str:
    """
    Semantic search over all indexed code symbols (functions, classes, files).

    Powered by vector embeddings — finds code by description, not just name.
    Example: search_symbols("authentication middleware", "django/django") finds
    code even if it's named authGuard or _check_perms.

    Args:
        query: Natural language description of what you're looking for
        repo_id: GitHub repo in owner/repo format
        limit: Max results (default 10)
    """
    try:
        from src.semantic.embeddings import embed_single
        from src.semantic.vector_store import search

        vector = embed_single(query)
        if not vector:
            return "Failed to generate embedding for query."
        results = search(
            query_vector=vector,
            repo_id=repo_id,
            limit=limit,
            min_score=0.35,
        )
        for sym in results:
            if isinstance(sym, dict) and "docstring" in sym and sym["docstring"]:
                sym["docstring"] = sym["docstring"][:100]
        return json.dumps({"symbols": results, "total": len(results)}, indent=2, default=str)
    except Exception:
        return f"Symbol search failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_change_context(
    file_path: str,
    repo_id: str,
    symbol_name: str = "",
) -> str:
    """
    Pre-change analysis for a file you plan to modify. Returns ALL of:
    - Risk score (centrality x dependents x test penalty)
    - Dependencies (what this file imports, what imports it)
    - Test coverage (which test files cover this file)
    - Coupled files (files that historically change together via git)
    - If symbol_name provided: callers that will break, impact analysis

    Call this BEFORE writing any file to understand the blast radius.
    This single call replaces get_risk_score + get_impact + get_dependencies +
    get_test_coverage + get_coupled_files individually.

    Args:
        file_path: Relative file path within the repo
        repo_id: GitHub repo in owner/repo format
        symbol_name: Optional symbol you plan to change (adds caller/impact analysis)
    """
    try:
        from src.graph.queries import (
            get_risk_score, get_file_dependencies, get_test_files,
            get_git_coupling, get_impact, get_file_summary,
        )

        result = {}
        result["risk"] = get_risk_score(repo_id, file_path)

        deps = get_file_dependencies(repo_id, file_path)
        result["dependencies"] = deps.get("dependencies", [])[:10]
        result["dependents"] = deps.get("dependents", [])[:10]
        result["test_files"] = get_test_files(repo_id, file_path)

        coupling = get_git_coupling(repo_id, file_path, min_score=0.1)
        trimmed_coupling = []
        strong = []
        for c in coupling[:10]:
            entry = {
                "file": c.get("file"),
                "jaccard": c.get("jaccard"),
                "confidence": c.get("confidence"),
                "co_count": c.get("co_count"),
                "recency_days": c.get("recency_days"),
            }
            trimmed_coupling.append(entry)
            if (c.get("confidence") or 0) >= 0.3:
                strong.append(c.get("file"))
        result["coupled_files"] = trimmed_coupling
        if strong:
            result["multi_file_warning"] = (
                f"Git history shows this file co-changes >=30% with: {', '.join(strong[:5])}. "
                "Non-trivial fixes here usually also edit those files."
            )

        if symbol_name:
            impact = get_impact(repo_id, symbol_name, depth=2)
            result["will_break"] = impact.get("will_break", [])[:10]
            result["may_break"] = impact.get("may_break", [])[:5]
            result["total_affected_files"] = impact.get("total_affected_files", 0)

        summary = get_file_summary(repo_id, file_path)
        if summary:
            result["file_info"] = {
                "language": summary.language,
                "lines": summary.lines,
                "centrality": summary.centrality,
                "summary": summary.summary,
            }

        return json.dumps(result, indent=2, default=str)
    except Exception:
        return f"Change context failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_callers(
    symbol_name: str,
    repo_id: str,
    depth: int = 1,
) -> str:
    """
    Find all functions that call a given symbol, up to N hops deep.
    Use before renaming a function or changing its signature.

    Args:
        symbol_name: Name of the function/class
        repo_id: GitHub repo in owner/repo format
        depth: Hop depth (1 = direct callers only)
    """
    try:
        from src.graph.queries import get_callers as _get_callers

        result = _get_callers(repo_id, symbol_name, depth=depth)
        return json.dumps({"callers": result, "total": len(result)}, indent=2, default=str)
    except Exception:
        return f"Get callers failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_impact(
    symbol_name: str,
    repo_id: str,
    depth: int = 2,
) -> str:
    """
    Analyze what breaks if a symbol is changed. Returns static callers
    (will_break) and dynamic dispatch callers (may_break).
    ALWAYS call this before changing a function signature.

    Args:
        symbol_name: Name of the function/class
        repo_id: GitHub repo in owner/repo format
        depth: How many call-chain hops to trace (default 2)
    """
    try:
        from src.graph.queries import get_impact as _get_impact

        result = _get_impact(repo_id, symbol_name, depth=depth)
        if isinstance(result, dict):
            for key in ("affected_files", "affected_symbols", "impacts"):
                if key in result and isinstance(result[key], list) and len(result[key]) > 20:
                    result[key] = result[key][:20]
                    result[f"{key}_truncated"] = True
        return json.dumps(result, indent=2, default=str)
    except Exception:
        return f"Impact analysis failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_dependencies(
    file_path: str,
    repo_id: str,
) -> str:
    """
    Get the import dependency graph for a file: what it imports and what imports it.
    Use before modifying a file to avoid breaking consumers.

    Args:
        file_path: Relative file path within the repo
        repo_id: GitHub repo in owner/repo format
    """
    try:
        from src.graph.queries import get_file_dependencies

        result = get_file_dependencies(repo_id, file_path)
        for key in ("imports", "imported_by", "dependencies", "dependents"):
            if key in result and isinstance(result[key], list) and len(result[key]) > 30:
                result[key] = result[key][:30]
                result[f"{key}_truncated"] = True
        return json.dumps(result, indent=2, default=str)
    except Exception:
        return f"Dependencies query failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_test_coverage(
    file_path: str,
    repo_id: str,
) -> str:
    """
    Find which test files cover a given source file (via TEST_FOR edges).
    Use to know which tests to run after modifying a file.

    Args:
        file_path: Relative path of the source file
        repo_id: GitHub repo in owner/repo format
    """
    try:
        from src.graph.queries import get_test_files

        result = get_test_files(repo_id, file_path)
        return json.dumps({"test_files": result}, indent=2, default=str)
    except Exception:
        return f"Test coverage query failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_coupled_files(
    file_path: str,
    repo_id: str,
    min_score: float = 0.1,
    min_confidence: float = 0.0,
) -> str:
    """
    Files historically co-changed with file_path, ranked by Jaccard similarity.

    Each entry includes:
      - jaccard: symmetric overlap (0-1)
      - confidence: directional P(other | this file), e.g. 0.4 means the other file
        also changed in 40% of the commits that touched this one
      - lift: how many times more likely than chance (>2 is meaningful)
      - co_count, total_a, total_b: raw counts
      - last_co_date, last_co_sha, recency_days: when they last co-changed

    Empty result with 0 recency_days on all candidates means no coupling data has been
    computed for this repo yet — call index_repo or ask the maintainer to re-index.

    Args:
        file_path: Relative file path
        repo_id: GitHub repo in owner/repo format
        min_score: Minimum Jaccard score (default 0.1)
        min_confidence: Minimum directional confidence (default 0.0 = no filter)
    """
    try:
        from src.graph.queries import get_git_coupling

        rows = get_git_coupling(
            repo_id, file_path,
            min_score=min_score, min_confidence=min_confidence,
        )
        enriched = []
        for r in rows[:15]:
            conf = r.get("confidence") or 0.0
            co = r.get("co_count") or 0
            total_a = r.get("total_a") or 0
            interp = (
                f"changed together in {conf * 100:.0f}% of commits touching this file "
                f"({co}/{total_a})" if total_a else None
            )
            entry = {
                "file": r.get("file"),
                "jaccard": r.get("jaccard"),
                "confidence": conf,
                "lift": r.get("lift"),
                "co_count": co,
                "total_a": total_a,
                "total_b": r.get("total_b"),
                "last_co_date": r.get("last_co_date"),
                "last_co_sha": r.get("last_co_sha"),
                "recency_days": r.get("recency_days"),
            }
            if interp:
                entry["interpretation"] = interp
            enriched.append(entry)

        payload = {"source_file": file_path, "coupled_files": enriched, "total": len(enriched)}
        if enriched:
            strong = [e for e in enriched if (e.get("confidence") or 0) >= 0.3]
            if strong:
                payload["multi_file_warning"] = (
                    f"{len(strong)} file(s) change together with this one >=30% of the time. "
                    f"If this edit is a non-trivial fix, consider also editing: "
                    + ", ".join(e["file"] for e in strong[:5])
                )
        else:
            payload["note"] = (
                "No coupling data found. Either the repo has weak historical coupling for this "
                "file, or git coupling has not yet been computed for this repo."
            )
        return json.dumps(payload, indent=2, default=str)
    except Exception:
        return f"Coupling query failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_risk_score(
    file_path: str,
    repo_id: str,
) -> str:
    """
    Composite risk score: centrality x dependents x test penalty.
    Higher score = more careful changes needed.

    Args:
        file_path: Relative file path
        repo_id: GitHub repo in owner/repo format
    """
    try:
        from src.graph.queries import get_risk_score as _get_risk_score

        result = _get_risk_score(repo_id, file_path)
        return json.dumps(result, indent=2, default=str)
    except Exception:
        return f"Risk score query failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_class_hierarchy(
    class_name: str,
    repo_id: str,
) -> str:
    """
    Get inheritance hierarchy: parent classes, child classes, and methods.
    Use to understand polymorphism and avoid breaking subclasses.

    Args:
        class_name: Name of the class
        repo_id: GitHub repo in owner/repo format
    """
    try:
        from src.graph import client as g

        parents = g.run(
            """MATCH (c:Class {name: $name, repo_id: $repo_id})-[:INHERITS*1..5]->(parent:Class)
               RETURN DISTINCT parent.name AS name, parent.file_path AS file,
                      coalesce(parent.line_start, 0) AS line_start""",
            {"name": class_name, "repo_id": repo_id},
        )
        children = g.run(
            """MATCH (c:Class {name: $name, repo_id: $repo_id})<-[:INHERITS*1..3]-(child:Class)
               RETURN DISTINCT child.name AS name, child.file_path AS file,
                      coalesce(child.line_start, 0) AS line_start""",
            {"name": class_name, "repo_id": repo_id},
        )
        methods = g.run(
            """MATCH (f:File)-[:CONTAINS]->(fn:Function)
               WHERE fn.repo_id = $repo_id AND fn.qualified_name STARTS WITH $prefix
               RETURN fn.name AS name, fn.file_path AS file, fn.line_start AS line_start
               ORDER BY fn.line_start LIMIT 30""",
            {"repo_id": repo_id, "prefix": class_name + "."},
        )
        result = {"class": class_name, "parents": parents, "children": children, "methods": methods}
        return json.dumps(result, indent=2, default=str)
    except Exception:
        return f"Class hierarchy query failed:\n```\n{traceback.format_exc()}\n```"


# ═══════════════════════════════════════════════════════════════════════════
# SESSION TRACING — start/finish/monitor sessions
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def start_session(
    repo_id: str,
    title: str,
    ticket_id: str = "",
) -> str:
    """
    Start tracing a fix-ticket session. Call this at the beginning of /fix-ticket.

    Traces every subsequent graph tool call (what was queried, results, latency).
    Call finish_session when done to write the trace file.

    Args:
        repo_id: GitHub repo in owner/repo format
        title: Issue title (for the trace log)
        ticket_id: Optional ticket ID
    """
    tracer.start_session(repo_id, title, ticket_id)
    return f"Session started for {repo_id}: {title}"


@mcp.tool()
def finish_session(
    success: bool = True,
    files_changed: str = "",
    pr_url: str = "",
    error: str = "",
) -> str:
    """
    Finish tracing the current session and write the trace file.

    Call this after creating the PR (or if the fix failed).

    Args:
        success: Whether the fix was successful
        files_changed: Comma-separated list of files that were modified
        pr_url: URL of the created PR (if any)
        error: Error message (if failed)
    """
    files = [f.strip() for f in files_changed.split(",") if f.strip()] if files_changed else []
    path = tracer.finish_session(
        success=success,
        files_changed=files,
        pr_url=pr_url,
        error=error,
    )
    if path:
        return f"Session trace written to: {path}"
    return "No active session to finish."


@mcp.tool()
def get_session_status() -> str:
    """
    Get the current in-progress session status.

    Use from a SEPARATE Claude Code session to monitor what the fixing session is doing.
    Returns: repo, ticket, wall time, total tool calls, last tool called, tool counts.
    """
    summary = tracer.get_current_summary()
    if not summary:
        return "No active session."
    return json.dumps(summary, indent=2, default=str)


@mcp.tool()
def list_session_traces(
    limit: int = 10,
) -> str:
    """
    List recent session traces from claude_code_agent/traces/.

    Use from a monitoring session to see past runs: which tickets were fixed,
    how many tool calls, wall time, success/failure.

    Args:
        limit: Max traces to list (default 10, most recent first)
    """
    trace_dir = Path(__file__).parent / "traces"
    if not trace_dir.exists():
        return "No traces directory found."

    traces = sorted(trace_dir.glob("*.json"), reverse=True)[:limit]
    if not traces:
        return "No traces found."

    results = []
    for t in traces:
        try:
            data = json.loads(t.read_text())
            summary = data.get("summary", {})
            results.append({
                "file": t.name,
                "ticket_id": data.get("ticket_id", "?"),
                "repo_id": data.get("repo_id", "?"),
                "title": data.get("title", "?")[:80],
                "success": summary.get("success"),
                "wall_time_s": summary.get("wall_time_s"),
                "total_tool_calls": summary.get("total_tool_calls"),
                "graph_calls": summary.get("graph_calls"),
                "files_changed": summary.get("files_changed", []),
                "pr_url": summary.get("pr_url", ""),
            })
        except Exception:
            results.append({"file": t.name, "error": "Failed to parse"})

    return json.dumps(results, indent=2, default=str)


@mcp.tool()
def read_session_trace(
    filename: str,
) -> str:
    """
    Read the full contents of a specific session trace.

    Use to deep-dive into what a session did: every tool call, args, results, timing.

    Args:
        filename: Trace filename (from list_session_traces output)
    """
    trace_dir = Path(__file__).parent / "traces"
    path = trace_dir / filename

    if not path.exists():
        return f"Trace file not found: {filename}"

    try:
        data = json.loads(path.read_text())
        return json.dumps(data, indent=2, default=str)
    except Exception:
        return f"Failed to parse trace: {traceback.format_exc()}"


# ═══════════════════════════════════════════════════════════════════════════
# INDEXING & STATUS
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
def index_repo(
    repo_url: str,
    repo_id: str,
) -> str:
    """
    Clone and index a GitHub repository into the knowledge graph.
    Must be run before any graph queries on a new repo. Takes ~2-5 minutes.

    Args:
        repo_url: Full GitHub clone URL (e.g. "https://github.com/django/django")
        repo_id: Short identifier in owner/repo format (e.g. "django/django")
    """
    try:
        indexer_dir = str(Path(__file__).parent.parent / "layer2-indexer")
        venv_python = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")

        result = subprocess.run(
            [venv_python, "-m", "src.cli", "index",
             "--repo", repo_url, "--id", repo_id,
             "--sync", "--skip-descriptions"],
            cwd=indexer_dir,
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            return f"Indexed {repo_id} successfully.\n\n```\n{result.stdout[-2000:]}\n```"
        else:
            return f"Indexing failed (exit {result.returncode}):\n```\n{result.stderr[-2000:]}\n```"
    except Exception:
        return f"index_repo failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def get_pipeline_status() -> str:
    """Check that all services (Qdrant, Memgraph, Ollama, Redis) are reachable."""
    import socket
    import urllib.request

    lines = ["## Pipeline Status\n"]

    def tcp_check(host: str, port: int) -> bool:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return True
        except Exception:
            return False

    mg_ok = tcp_check(os.environ.get("MEMGRAPH_HOST", "localhost"),
                      int(os.environ.get("MEMGRAPH_PORT", "7687")))
    lines.append(f"- Memgraph (graph DB): {'OK' if mg_ok else 'DOWN'}")

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_host = redis_url.split("//")[-1].split(":")[0]
    redis_port = int(redis_url.split(":")[-1]) if ":" in redis_url.split("//")[-1] else 6379
    redis_ok = tcp_check(redis_host, redis_port)
    lines.append(f"- Redis: {'OK' if redis_ok else 'DOWN'}")

    for name, url in [("Qdrant", os.environ.get("QDRANT_URL", "http://localhost:6333")),
                      ("Ollama", os.environ.get("OLLAMA_URL", "http://localhost:11434"))]:
        try:
            urllib.request.urlopen(url, timeout=2)
            lines.append(f"- {name}: OK")
        except Exception:
            lines.append(f"- {name}: DOWN")

    lines.append("\n**Environment:**")
    for var in ["ANTHROPIC_API_KEY", "GITHUB_TOKEN"]:
        val = os.environ.get(var, "")
        masked = f"{val[:6]}..." if len(val) > 6 else ("(not set)" if not val else val)
        lines.append(f"- `{var}`: {masked}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
