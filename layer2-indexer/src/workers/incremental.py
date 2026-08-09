"""
Incremental index update — triggered by GitHub push webhooks.
Only re-indexes changed files, then re-stitches affected importers.
"""
from __future__ import annotations

import structlog

from src.workers.celery_app import app

log = structlog.get_logger(__name__)


@app.task(name="src.workers.incremental.handle_push", bind=True)
def handle_push(self, repo_id: str, payload: dict) -> dict:
    """
    Process a GitHub push event:
    1. Extract changed/deleted files
    2. Handle force push (compute full diff)
    3. Delete removed files from graph + vector store
    4. Re-index changed files
    5. Re-stitch affected importers
    """
    from src.vcs.diff import extract_changed_files, compute_force_push_diff
    from src.vcs.clone import clone_or_pull, list_all_files, get_repo_path
    from src.graph.stitcher import stitch_file
    from src.workers.index_worker import index_file
    from src.graph import client as g
    from src.semantic.vector_store import delete_file_entities

    repo_url = payload.get("repository", {}).get("clone_url", "")
    branch = payload.get("ref", "refs/heads/main").replace("refs/heads/", "")

    # Pull latest
    repo_path = clone_or_pull(repo_url, repo_id, branch)
    all_files = list_all_files(repo_path)

    # Extract changed files
    changed, deleted = extract_changed_files(payload)

    # Handle force push sentinel
    if changed and changed[0].startswith("__FORCE_PUSH__:"):
        _, before_sha, after_sha = changed[0].split(":")
        changed, deleted = compute_force_push_diff(repo_path, before_sha, after_sha)
        log.info("incremental.force_push", changed=len(changed), deleted=len(deleted))

    # Delete removed files
    for file_path in deleted:
        g.delete_file_subgraph(repo_id, file_path)
        delete_file_entities(repo_id, file_path)
        log.info("incremental.deleted", file=file_path)

    # Re-index changed files
    for file_path in changed:
        index_file.apply_async(
            args=[repo_id, file_path, str(repo_path)],
            queue="indexing",
            priority=9,  # high priority over full-index tasks
        )

    # Re-stitch changed files + their importers (after a short delay for workers to finish)
    stitch_after_update.apply_async(
        args=[repo_id, str(repo_path), list(all_files), changed],
        countdown=60,  # wait 60s for index_file tasks to complete
    )

    log.info("incremental.dispatched", repo_id=repo_id, changed=len(changed), deleted=len(deleted))
    return {"changed": len(changed), "deleted": len(deleted)}


@app.task(name="src.workers.incremental.stitch_after_update")
def stitch_after_update(repo_id: str, repo_root: str, all_files: list[str], changed_files: list[str]) -> None:
    """Re-stitch each changed file after incremental index."""
    from src.graph.stitcher import stitch_file
    all_files_set = set(all_files)
    for file_path in changed_files:
        stitch_file(repo_id, repo_root, all_files_set, file_path)
    log.info("stitch.incremental.done", repo_id=repo_id, files=len(changed_files))
