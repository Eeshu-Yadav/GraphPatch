"""
Git operations: clone, pull, file enumeration.
"""
from __future__ import annotations

import os
from pathlib import Path

import structlog
from git import GitCommandError, InvalidGitRepositoryError, Repo

from src.config import settings

log = structlog.get_logger(__name__)


def get_repo_path(repo_id: str) -> Path:
    return Path(settings.repo_cache_dir) / repo_id.replace("/", "_")


def clone_or_pull(repo_url: str, repo_id: str, branch: str = "main") -> Path:
    """
    Clone repo if not present, otherwise pull latest.
    Also accepts a local directory path — if repo_url is a local folder, use it directly.
    Returns the local path to the repo.
    """
    # Support local paths: if repo_url points to an existing directory, use it directly
    candidate = Path(repo_url)
    if candidate.is_dir():
        log.info("git.local_path", repo_id=repo_id, path=str(candidate))
        return candidate

    local_path = get_repo_path(repo_id)

    if local_path.exists():
        try:
            repo = Repo(local_path)
            origin = repo.remotes.origin
            origin.pull(branch)
            log.info("git.pulled", repo_id=repo_id, branch=branch)
        except (InvalidGitRepositoryError, GitCommandError) as e:
            log.warning("git.pull.failed", repo_id=repo_id, error=str(e))
            # Nuke and re-clone on failure
            import shutil
            shutil.rmtree(local_path)
            _clone(repo_url, local_path, branch, repo_id)
    else:
        _clone(repo_url, local_path, branch, repo_id)

    return local_path


def _clone(repo_url: str, local_path: Path, branch: str, repo_id: str) -> None:
    local_path.mkdir(parents=True, exist_ok=True)
    try:
        Repo.clone_from(repo_url, str(local_path), branch=branch, depth=200)
    except GitCommandError:
        # Branch not found — clone default branch instead
        import shutil
        shutil.rmtree(local_path)
        local_path.mkdir(parents=True, exist_ok=True)
        log.warning("git.branch.not_found", branch=branch, fallback="default")
        Repo.clone_from(repo_url, str(local_path), depth=200)
    log.info("git.cloned", repo_id=repo_id, url=repo_url)


def list_all_files(repo_path: Path) -> set[str]:
    """Return all files in the repo as a set of relative paths (POSIX, no leading slash)."""
    files: set[str] = set()
    for root, dirs, file_names in os.walk(repo_path):
        # Prune .git and other hidden dirs early
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in file_names:
            abs_path = Path(root) / fname
            rel = abs_path.relative_to(repo_path)
            files.add(rel.as_posix())
    return files


def get_file_content(repo_path: Path, relative_path: str) -> str:
    """Read a file from the cloned repo."""
    abs_path = repo_path / relative_path
    return abs_path.read_text(encoding="utf-8", errors="replace")
