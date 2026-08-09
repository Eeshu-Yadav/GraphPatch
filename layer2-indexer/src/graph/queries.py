"""
Pre-built Cypher query functions used by the API and downstream agents.
All queries have a 100ms timeout enforced at the Memgraph level.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.graph import client as g


@dataclass
class ImpactResult:
    file: str
    symbol: str
    depth: int
    importance: float
    is_dynamic: bool = False


@dataclass
class CouplingResult:
    file: str
    score: float
    commit_count: int


@dataclass
class FileSummary:
    path: str
    language: str
    lines: int
    centrality: float
    summary: str
    is_test: bool


def get_impact(repo_id: str, symbol_name: str, depth: int = 3) -> dict:
    """
    What breaks if symbol_name changes?
    Returns callers up to `depth` hops, sorted by importance.
    """
    rows = g.run(
        f"""
        MATCH (target)
        WHERE target.name = $name AND target.repo_id = $repo_id
        WITH target
        MATCH path = (target)<-[:CALLS*1..{depth}]-(caller)
        WHERE caller.repo_id = $repo_id
        RETURN caller.file_path AS file,
               caller.name AS symbol,
               length(path) AS depth,
               coalesce(caller.centrality, 0.0) AS importance,
               any(r IN relationships(path) WHERE r.is_dynamic = true) AS is_dynamic
        ORDER BY depth ASC, importance DESC
        LIMIT 50
        """,
        {"name": symbol_name, "repo_id": repo_id},
    )

    will_break = []
    may_break = []
    for r in rows:
        result = ImpactResult(
            file=r["file"],
            symbol=r["symbol"],
            depth=r["depth"],
            importance=r["importance"],
            is_dynamic=r["is_dynamic"],
        )
        if r["is_dynamic"]:
            may_break.append(result)
        else:
            will_break.append(result)

    return {
        "symbol": symbol_name,
        "will_break": [vars(r) for r in will_break],
        "may_break": [vars(r) for r in may_break],
        "total_affected_files": len({r.file for r in will_break + may_break}),
    }


def get_file_dependencies(repo_id: str, file_path: str) -> dict:
    """
    Return files this file depends on (imports) and files that depend on it (dependents).
    """
    rows = g.run(
        """
        MATCH (f:File)
        WHERE f.path = $path AND f.repo_id = $repo_id
        OPTIONAL MATCH (f)-[:IMPORTS]->(:Module)-[:RESOLVES_TO]->(dep:File)
        OPTIONAL MATCH (f)<-[:RESOLVES_TO]-(:Module)<-[:IMPORTS]-(dependent:File)
        RETURN DISTINCT
          dep.path AS dependency,
          dependent.path AS dependent
        """,
        {"path": file_path, "repo_id": repo_id},
    )

    dependencies = list({r["dependency"] for r in rows if r["dependency"]})
    dependents = list({r["dependent"] for r in rows if r["dependent"]})
    return {"file": file_path, "dependencies": dependencies, "dependents": dependents}


def get_callers(repo_id: str, symbol_name: str, depth: int = 1) -> list[dict]:
    """Who calls this function? depth=1 for direct callers, depth=2+ for transitive."""
    return g.run(
        f"""
        MATCH (target)
        WHERE target.name = $name AND target.repo_id = $repo_id
        WITH target
        MATCH path = (caller)-[:CALLS*1..{depth}]->(target)
        WHERE caller.repo_id = $repo_id AND caller <> target
        RETURN DISTINCT caller.name AS caller,
               caller.file_path AS file,
               coalesce(caller.centrality, 0.0) AS importance,
               length(path) AS depth
        ORDER BY importance DESC
        LIMIT 30
        """,
        {"name": symbol_name, "repo_id": repo_id},
    )


def get_test_files(repo_id: str, file_path: str) -> list[str]:
    """Return test files that cover the given source file."""
    rows = g.run(
        """
        MATCH (src:File)
        WHERE src.path = $path AND src.repo_id = $repo_id
        WITH src
        MATCH (test:File)-[:TEST_FOR]->(src)
        WHERE test.is_test = true
        RETURN test.path AS test_file
        """,
        {"path": file_path, "repo_id": repo_id},
    )
    return [r["test_file"] for r in rows]


def get_top_files(repo_id: str, path_prefix: str = "", limit: int = 10) -> list[dict]:
    """Return highest centrality files in a path prefix (useful for domain discovery)."""
    return g.run(
        """
        MATCH (f:File {repo_id: $repo_id})
        WHERE ($prefix = '' OR f.path STARTS WITH $prefix)
          AND f.is_test = false
        RETURN f.path AS path, f.centrality AS centrality,
               f.language AS language, f.lines AS lines,
               coalesce(f.summary, '') AS summary
        ORDER BY f.centrality DESC
        LIMIT $limit
        """,
        {"repo_id": repo_id, "prefix": path_prefix, "limit": limit},
    )


def get_file_summary(repo_id: str, file_path: str) -> FileSummary | None:
    rows = g.run(
        """
        MATCH (f:File {path: $path, repo_id: $repo_id})
        RETURN f.path AS path, f.language AS language, f.lines AS lines,
               coalesce(f.centrality, 0.0) AS centrality,
               coalesce(f.summary, '') AS summary, f.is_test AS is_test
        """,
        {"path": file_path, "repo_id": repo_id},
    )
    if not rows:
        return None
    r = rows[0]
    return FileSummary(
        path=r["path"],
        language=r["language"],
        lines=r["lines"],
        centrality=r["centrality"],
        summary=r["summary"],
        is_test=r["is_test"],
    )


def get_git_coupling(
    repo_id: str,
    file_path: str,
    min_score: float = 0.1,
    min_confidence: float = 0.0,
) -> list[dict]:
    """Return files historically co-changed with file_path, ranked by Jaccard.

    Each entry includes: jaccard, confidence (directional, P(other|this)),
    lift, co_count, last_co_date, recency_days.
    """
    rows = g.run(
        """
        MATCH (f:File {path: $path, repo_id: $repo_id})-[c:COUPLED_WITH]->(other:File)
        WHERE coalesce(c.jaccard, c.score, 0.0) >= $min_score
          AND coalesce(c.confidence, 0.0) >= $min_conf
        RETURN other.path     AS file,
               coalesce(c.jaccard, c.score, 0.0) AS jaccard,
               coalesce(c.confidence, 0.0)       AS confidence,
               coalesce(c.lift, 0.0)             AS lift,
               coalesce(c.co_count, c.commit_count, 0) AS co_count,
               c.total_a       AS total_a,
               c.total_b       AS total_b,
               c.last_co_date  AS last_co_date,
               c.last_co_sha   AS last_co_sha,
               c.recency_days  AS recency_days
        ORDER BY jaccard DESC
        LIMIT 20
        """,
        {"path": file_path, "repo_id": repo_id,
         "min_score": min_score, "min_conf": min_confidence},
    )
    return rows


def get_risk_score(repo_id: str, file_path: str) -> dict:
    """
    Composite risk score for a file.
    risk = centrality × (1 + dependents/10) × test_penalty
    where test_penalty = 2.0 if no tests, 1.0 if tests exist.
    """
    rows = g.run(
        """
        MATCH (f:File {path: $path, repo_id: $repo_id})
        OPTIONAL MATCH (dep:File {repo_id: $repo_id})-[:IMPORTS]->(:Module)-[:RESOLVES_TO]->(f)
        WITH f, count(DISTINCT dep) AS dependents
        OPTIONAL MATCH (test:File)-[:TEST_FOR]->(f)
        WITH f, dependents, count(DISTINCT test) AS test_count
        RETURN f.path AS path,
               coalesce(f.centrality, 0.0) AS centrality,
               dependents,
               test_count,
               coalesce(f.centrality, 0.0) * (1.0 + dependents / 10.0)
                 * CASE WHEN test_count = 0 THEN 2.0 ELSE 1.0 END AS risk_score
        """,
        {"path": file_path, "repo_id": repo_id},
    )
    if not rows:
        return {"path": file_path, "centrality": 0.0, "dependents": 0, "test_count": 0, "risk_score": 0.0}
    r = rows[0]
    return {
        "path": r["path"],
        "centrality": r["centrality"],
        "dependents": r["dependents"],
        "test_count": r["test_count"],
        "risk_score": round(r["risk_score"], 4),
    }


# ---------------------------------------------------------------------------
# Batch query functions — single Cypher query for multiple files/symbols
# ---------------------------------------------------------------------------

def get_file_summaries_batch(repo_id: str, file_paths: list[str]) -> list[FileSummary]:
    """Batch fetch file summaries for multiple paths in a single query."""
    if not file_paths:
        return []
    rows = g.run(
        """
        MATCH (f:File)
        WHERE f.repo_id = $repo_id AND f.path IN $paths
        RETURN f.path AS path, f.language AS language, f.lines AS lines,
               coalesce(f.centrality, 0.0) AS centrality,
               coalesce(f.summary, '') AS summary, f.is_test AS is_test
        """,
        {"repo_id": repo_id, "paths": file_paths},
    )
    return [
        FileSummary(
            path=r["path"], language=r["language"], lines=r["lines"],
            centrality=r["centrality"], summary=r["summary"], is_test=r["is_test"],
        )
        for r in rows
    ]


def get_callers_batch(repo_id: str, symbol_names: list[str]) -> list[dict]:
    """Batch fetch direct callers for multiple symbols in a single query."""
    if not symbol_names:
        return []
    return g.run(
        """
        UNWIND $names AS name
        MATCH (target)
        WHERE target.name = name AND target.repo_id = $repo_id
        WITH name AS target_name, target
        MATCH (caller)-[:CALLS]->(target)
        WHERE caller.repo_id = $repo_id AND caller <> target
        RETURN DISTINCT target_name,
               caller.name AS caller,
               caller.file_path AS file,
               coalesce(caller.centrality, 0.0) AS importance
        ORDER BY target_name, importance DESC
        """,
        {"repo_id": repo_id, "names": symbol_names},
    )


def get_file_dependencies_batch(repo_id: str, file_paths: list[str]) -> list[dict]:
    """Batch fetch dependencies + dependents for multiple files."""
    if not file_paths:
        return []
    return g.run(
        """
        MATCH (f:File)
        WHERE f.repo_id = $repo_id AND f.path IN $paths
        OPTIONAL MATCH (f)-[:IMPORTS]->(:Module)-[:RESOLVES_TO]->(dep:File)
        OPTIONAL MATCH (f)<-[:RESOLVES_TO]-(:Module)<-[:IMPORTS]-(dependent:File)
        RETURN f.path AS file,
               collect(DISTINCT dep.path) AS dependencies,
               collect(DISTINCT dependent.path) AS dependents
        """,
        {"repo_id": repo_id, "paths": file_paths},
    )


def get_test_files_batch(repo_id: str, file_paths: list[str]) -> list[str]:
    """Batch fetch test files covering multiple source files."""
    if not file_paths:
        return []
    rows = g.run(
        """
        MATCH (src:File)
        WHERE src.repo_id = $repo_id AND src.path IN $paths
        WITH src
        MATCH (test:File)-[:TEST_FOR]->(src)
        WHERE test.is_test = true
        RETURN DISTINCT test.path AS test_file
        """,
        {"repo_id": repo_id, "paths": file_paths},
    )
    return [r["test_file"] for r in rows]


def get_git_coupling_batch(repo_id: str, file_paths: list[str], min_score: float = 0.1) -> list[dict]:
    """Batch fetch co-changed files for multiple source files."""
    if not file_paths:
        return []
    return g.run(
        """
        MATCH (f:File)
        WHERE f.repo_id = $repo_id AND f.path IN $paths
        WITH f
        MATCH (f)-[c:COUPLED_WITH]->(other:File)
        WHERE c.score >= $min_score
        RETURN f.path AS source_file, other.path AS file,
               c.score AS score, c.commit_count AS commits
        ORDER BY c.score DESC
        """,
        {"repo_id": repo_id, "paths": file_paths, "min_score": min_score},
    )


def lookup_symbols_batch(repo_id: str, names: list[str]) -> list[dict]:
    """Batch lookup symbols by name in a single query."""
    if not names:
        return []
    return g.run(
        """
        UNWIND $names AS name
        MATCH (s)
        WHERE s.repo_id = $repo_id AND s.name = name
          AND (s:Function OR s:Class)
        RETURN s.name AS name,
               coalesce(s.qualified_name, s.name) AS qualified_name,
               s.file_path AS file_path,
               labels(s)[0] AS entity_type,
               coalesce(s.summary, '') AS summary,
               coalesce(s.centrality, 0.0) AS centrality,
               coalesce(s.docstring, '') AS docstring,
               coalesce(s.line_start, 0) AS line_start
        ORDER BY centrality DESC
        """,
        {"repo_id": repo_id, "names": names},
    )


def get_pr_reviewers(repo_id: str, file_paths: list[str]) -> dict:
    """
    Given files changed in a PR, return owners from OWNED_BY edges.
    Returns: {reviewers: [{owner, patterns: [str]}]}
    """
    if not file_paths:
        return {"reviewers": []}

    rows = g.run(
        """
        MATCH (f:File {repo_id: $repo_id})-[r:OWNED_BY]->(o:Owner)
        WHERE f.path IN $paths
        RETURN o.name AS owner, collect(DISTINCT r.pattern) AS patterns
        ORDER BY owner
        """,
        {"repo_id": repo_id, "paths": file_paths},
    )
    return {"reviewers": [{"owner": r["owner"], "patterns": r["patterns"]} for r in rows]}


def get_class_hierarchy(repo_id: str, class_name: str) -> dict:
    """Get parent classes (supers) and child classes (subclasses) via INHERITS edges."""
    parents = g.run(
        """
        MATCH (c:Class {name: $name, repo_id: $repo_id})-[:INHERITS*1..5]->(parent:Class)
        WHERE parent.repo_id = $repo_id
        RETURN DISTINCT parent.name AS name, parent.file_path AS file,
               coalesce(parent.line_start, 0) AS line_start
        """,
        {"name": class_name, "repo_id": repo_id},
    )
    children = g.run(
        """
        MATCH (c:Class {name: $name, repo_id: $repo_id})<-[:INHERITS*1..3]-(child:Class)
        WHERE child.repo_id = $repo_id
        RETURN DISTINCT child.name AS name, child.file_path AS file,
               coalesce(child.line_start, 0) AS line_start
        """,
        {"name": class_name, "repo_id": repo_id},
    )
    return {"class": class_name, "parents": parents, "children": children}
