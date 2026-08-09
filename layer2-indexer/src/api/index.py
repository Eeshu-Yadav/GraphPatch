"""
Manual index trigger endpoints.
POST /index/full   — trigger a full re-index of a repo
POST /index/file   — re-index a single file (useful for development)
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.workers.index_worker import full_index, index_file

router = APIRouter(prefix="/index")


class FullIndexRequest(BaseModel):
    repo_id: str        # e.g. "myorg/my-repo"
    repo_url: str       # HTTPS clone URL
    branch: str = "main"


class FileIndexRequest(BaseModel):
    repo_id: str
    file_path: str      # relative path within repo
    repo_root: str      # absolute local path to cloned repo


@router.post("/full")
def trigger_full_index(req: FullIndexRequest) -> dict:
    """Enqueue a full index job. Returns immediately."""
    task = full_index.apply_async(
        args=[req.repo_id, req.repo_url, req.branch],
        queue="indexing",
    )
    return {"status": "queued", "task_id": task.id, "repo_id": req.repo_id}


@router.post("/file")
def trigger_file_index(req: FileIndexRequest) -> dict:
    """Enqueue a single-file index job."""
    task = index_file.apply_async(
        args=[req.repo_id, req.file_path, req.repo_root],
        queue="indexing",
        priority=5,
    )
    return {"status": "queued", "task_id": task.id, "file": req.file_path}
