"""
Extract changed file lists from GitHub push webhook payloads and git diffs.
"""
from __future__ import annotations

import datetime
from pathlib import Path

import structlog
from git import Repo

log = structlog.get_logger(__name__)


def extract_changed_files(payload: dict) -> tuple[list[str], list[str]]:
    """
    Parse a GitHub push webhook payload.
    Returns (changed_files, deleted_files) as relative paths.
    Force push: computes full diff between before_sha and after_sha.
    """
    is_forced = payload.get("forced", False)
    before_sha = payload.get("before", "")
    after_sha = payload.get("after", "")

    if is_forced and before_sha and after_sha:
        log.warning("git.force_push", before=before_sha[:8], after=after_sha[:8])
        return _full_diff_marker(before_sha, after_sha)

    changed: set[str] = set()
    deleted: set[str] = set()

    for commit in payload.get("commits", []):
        changed.update(commit.get("added", []))
        changed.update(commit.get("modified", []))
        deleted.update(commit.get("removed", []))

    return list(changed - deleted), list(deleted)


def _full_diff_marker(before_sha: str, after_sha: str) -> tuple[list[str], list[str]]:
    """
    Marker for force push: returns special sentinel so worker fetches full diff from git.
    Actual diff is computed by the worker using the cloned repo.
    """
    return [f"__FORCE_PUSH__:{before_sha}:{after_sha}"], []


def compute_force_push_diff(repo_path: Path, before_sha: str, after_sha: str) -> tuple[list[str], list[str]]:
    """
    Compute actual changed/deleted files between two SHAs in a cloned repo.
    Used by the worker when handling a force push.
    """
    repo = Repo(repo_path)
    try:
        diff = repo.commit(before_sha).diff(repo.commit(after_sha))
    except Exception as e:
        log.error("git.diff.failed", before=before_sha, after=after_sha, error=str(e))
        return [], []

    changed: list[str] = []
    deleted: list[str] = []

    for d in diff:
        if d.deleted_file:
            deleted.append(d.a_path)
        else:
            changed.append(d.b_path)

    return changed, deleted


def extract_coupling_data(
    repo_path: Path,
    max_commits: int = 10_000,
    min_files: int = 2,
    max_files: int = 20,
) -> list[tuple[list[str], str, "datetime.datetime"]]:
    """Scan the last `max_commits` commits for co-change patterns.

    Returns (files, sha, date) per commit. Skips merges, root commit, and commits
    outside [min_files, max_files]. Noise filtering (promiscuous files) happens in
    coupling.py, not here.
    """
    repo = Repo(repo_path)
    results: list[tuple[list[str], str, datetime.datetime]] = []

    for commit in repo.iter_commits(max_count=max_commits):
        if len(commit.parents) != 1:
            continue
        try:
            files = [f for f in commit.stats.files.keys() if f]
        except Exception as e:
            log.debug("coupling.stats_failed", sha=commit.hexsha[:8], error=str(e)[:80])
            continue
        if not (min_files <= len(files) <= max_files):
            continue
        results.append((files, commit.hexsha, commit.committed_datetime))

    log.info("coupling.extracted", total_commits_scanned=max_commits, usable_commits=len(results))
    return results
