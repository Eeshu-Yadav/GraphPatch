"""
Graph-based retrieval — look up symbols and expand context via Memgraph.
Uses batch queries to minimize round trips (1 query per function instead of N).
"""
from __future__ import annotations

import structlog
from src.graph import queries as graph_q

log = structlog.get_logger(__name__)


def lookup_symbols(repo_id: str, names: list[str]) -> list[dict]:
    """
    Look up symbols by name in Memgraph (single batch query).
    Returns list of dicts with name, file_path, entity_type, summary, centrality, docstring, line_start.
    """
    if not names:
        return []
    results = graph_q.lookup_symbols_batch(repo_id, names)
    log.debug("graph.lookup", names=len(names), found=len(results))
    return results


def get_call_graph(repo_id: str, symbol_names: list[str]) -> dict[str, list[dict]]:
    """
    For each symbol, get its direct callers with file paths (single batch query).
    Returns {symbol_name: [{"name": "caller", "file": "path.py"}, ...]}
    """
    if not symbol_names:
        return {}
    rows = graph_q.get_callers_batch(repo_id, symbol_names)
    call_graph: dict[str, list[dict]] = {}
    for row in rows:
        call_graph.setdefault(row["target_name"], []).append({
            "name": row["caller"],
            "file": row.get("file", ""),
            "importance": row.get("importance", 0.0),
        })
    return call_graph


def get_file_contexts(repo_id: str, file_paths: list[str]) -> list[dict]:
    """Get file summaries for a list of paths (single batch query)."""
    if not file_paths:
        return []
    summaries = graph_q.get_file_summaries_batch(repo_id, file_paths)
    return [vars(s) for s in summaries]


def get_dependencies(repo_id: str, file_paths: list[str]) -> dict[str, dict]:
    """Get dependency graph for a list of files (single batch query)."""
    if not file_paths:
        return {}
    rows = graph_q.get_file_dependencies_batch(repo_id, file_paths)
    deps: dict[str, dict] = {}
    for row in rows:
        # Filter out None values from collect()
        dependencies = [d for d in row["dependencies"] if d]
        dependents = [d for d in row["dependents"] if d]
        deps[row["file"]] = {
            "file": row["file"],
            "dependencies": dependencies,
            "dependents": dependents,
        }
    return deps


def get_test_files(repo_id: str, file_paths: list[str]) -> list[str]:
    """Get test files covering any of the given source files (single batch query)."""
    if not file_paths:
        return []
    return graph_q.get_test_files_batch(repo_id, file_paths)


def get_impact_summary(repo_id: str, symbol_names: list[str]) -> dict[str, dict]:
    """
    For each symbol, get impact assessment (what breaks if changed).
    Returns {symbol_name: {"total_callers": N, "affected_files": N, "risk": "high|medium|low"}}
    """
    if not symbol_names:
        return {}
    result = {}
    for name in symbol_names[:5]:  # Cap at 5 to limit Memgraph queries
        try:
            impact = graph_q.get_impact(repo_id, name, depth=2)
            total = len(impact.get("will_break", [])) + len(impact.get("may_break", []))
            affected = impact.get("total_affected_files", 0)
            risk = "high" if total > 10 else "medium" if total > 3 else "low"
            result[name] = {
                "total_callers": total,
                "affected_files": affected,
                "risk": risk,
                "will_break": [f"{r['symbol']} ({r['file']})" for r in impact.get("will_break", [])[:5]],
            }
        except Exception as e:
            log.debug("graph.impact_failed", symbol=name, error=str(e))
    return result


def get_coupled_files(repo_id: str, file_paths: list[str]) -> list[dict]:
    """Get historically co-changed files for the given source files (single batch query)."""
    if not file_paths:
        return []
    rows = graph_q.get_git_coupling_batch(repo_id, file_paths, min_score=0.1)
    # Deduplicate: keep highest score per coupled file
    seen: dict[str, dict] = {}
    for row in rows:
        key = row["file"]
        if key not in seen or row["score"] > seen[key]["score"]:
            seen[key] = {"file": row["file"], "score": row["score"], "commits": row["commits"]}
    return sorted(seen.values(), key=lambda x: x["score"], reverse=True)
