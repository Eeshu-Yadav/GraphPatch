"""Git co-change (coupling) analysis.

Builds COUPLED_WITH edges from historical commit data. Each edge stores:
  jaccard, confidence (directional), lift, co_count, last_co_date, last_co_sha.
"""
from __future__ import annotations

import datetime
from collections import defaultdict
from pathlib import Path

import structlog

from src.graph import client as g
from src.vcs.diff import extract_coupling_data

log = structlog.get_logger(__name__)

_TOP_K = 500
_MIN_COMMITS = 3
_MIN_CO_COUNT = 3
_MIN_JACCARD = 0.05
_MIN_LIFT = 2.0
_PROMISCUITY_THRESHOLD = 0.30
_PER_FILE_TOP = 20
_BATCH_SIZE = 1000


def compute_and_store_coupling(
    repo_id: str,
    repo_path: Path,
    max_commits: int = 10_000,
) -> None:
    """Compute coupling scores from git history, write COUPLED_WITH edges."""
    log.info("coupling.starting", repo_id=repo_id, max_commits=max_commits)

    commit_data = extract_coupling_data(repo_path, max_commits=max_commits)
    if not commit_data:
        log.info("coupling.no_data", repo_id=repo_id)
        return

    total_commits = len(commit_data)
    file_commits: dict[str, set[str]] = defaultdict(set)
    pair_data: dict[tuple[str, str], dict] = {}

    for files, sha, date in commit_data:
        files = sorted(set(files))
        for f in files:
            file_commits[f].add(sha)
        for i, a in enumerate(files):
            for b in files[i + 1:]:
                key = (a, b)
                d = pair_data.get(key)
                if d is None:
                    pair_data[key] = {"count": 1, "last_date": date, "last_sha": sha}
                else:
                    d["count"] += 1
                    if date > d["last_date"]:
                        d["last_date"] = date
                        d["last_sha"] = sha

    # Promiscuity filter: drop files that appear in >threshold of commits.
    max_allowed = total_commits * _PROMISCUITY_THRESHOLD
    promiscuous = {f for f, shas in file_commits.items() if len(shas) > max_allowed}
    if promiscuous:
        log.info("coupling.promiscuous_filtered", count=len(promiscuous),
                 examples=list(promiscuous)[:5])

    file_commits = {
        f: shas for f, shas in file_commits.items()
        if len(shas) >= _MIN_COMMITS and f not in promiscuous
    }
    if not file_commits:
        log.info("coupling.no_active_files", repo_id=repo_id)
        return

    ranked = sorted(file_commits.keys(), key=lambda f: len(file_commits[f]), reverse=True)
    top_set = set(ranked[:_TOP_K])

    now = datetime.datetime.now(datetime.timezone.utc)

    # Per-file outgoing edges (directional confidence), ranked, then batched.
    per_file_edges: dict[str, list[dict]] = defaultdict(list)

    for (a, b), d in pair_data.items():
        if d["count"] < _MIN_CO_COUNT:
            continue
        if a not in top_set or b not in top_set:
            continue

        total_a = len(file_commits[a])
        total_b = len(file_commits[b])
        co = d["count"]
        jaccard = co / (total_a + total_b - co)
        if jaccard < _MIN_JACCARD:
            continue

        p_a = total_a / total_commits
        p_b = total_b / total_commits
        p_ab = co / total_commits
        lift = p_ab / (p_a * p_b) if (p_a * p_b) > 0 else 0.0
        if lift < _MIN_LIFT:
            continue

        conf_ab = co / total_a
        conf_ba = co / total_b
        last_date = d["last_date"]
        recency = (now - last_date).days if last_date.tzinfo else (now.replace(tzinfo=None) - last_date).days

        base = {
            "jaccard": round(jaccard, 4),
            "lift": round(lift, 3),
            "co_count": co,
            "total_a": total_a,
            "total_b": total_b,
            "last_co_date": last_date.isoformat(),
            "last_co_sha": d["last_sha"],
            "recency_days": recency,
        }
        per_file_edges[a].append({
            **base, "path_a": a, "path_b": b, "confidence": round(conf_ab, 4),
        })
        per_file_edges[b].append({
            **base,
            "path_a": b, "path_b": a, "confidence": round(conf_ba, 4),
            "total_a": total_b, "total_b": total_a,
        })

    edges: list[dict] = []
    for f, items in per_file_edges.items():
        items.sort(key=lambda x: x["jaccard"], reverse=True)
        edges.extend(items[:_PER_FILE_TOP])

    if not edges:
        log.info("coupling.no_pairs", repo_id=repo_id)
        return

    g.run_void(
        "MATCH (:File {repo_id: $repo_id})-[c:COUPLED_WITH]->() DELETE c",
        {"repo_id": repo_id},
    )

    for i in range(0, len(edges), _BATCH_SIZE):
        chunk = edges[i:i + _BATCH_SIZE]
        g.run_void(
            """
            UNWIND $items AS item
            MATCH (a:File {repo_id: $repo_id, path: item.path_a})
            MATCH (b:File {repo_id: $repo_id, path: item.path_b})
            MERGE (a)-[c:COUPLED_WITH]->(b)
            SET c.jaccard       = item.jaccard,
                c.confidence    = item.confidence,
                c.lift          = item.lift,
                c.co_count      = item.co_count,
                c.total_a       = item.total_a,
                c.total_b       = item.total_b,
                c.last_co_date  = item.last_co_date,
                c.last_co_sha   = item.last_co_sha,
                c.recency_days  = item.recency_days,
                c.score         = item.jaccard
            """,
            {"items": chunk, "repo_id": repo_id},
        )

    log.info("coupling.done", repo_id=repo_id, edges=len(edges),
             files=len(per_file_edges), total_commits=total_commits)
