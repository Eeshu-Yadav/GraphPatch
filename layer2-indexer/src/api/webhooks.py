"""
GitHub App webhook receiver.
Verifies HMAC-SHA256 signature, then dispatches to workers.
"""
from __future__ import annotations

import hashlib
import hmac

import structlog
from fastapi import APIRouter, Header, HTTPException, Request

from src.config import settings
from src.workers.incremental import handle_push

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/webhooks")


def _verify_signature(body: bytes, signature: str) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not settings.github_webhook_secret:
        return True  # skip verification in dev (no secret set)
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str = Header(default=""),
) -> dict:
    body = await request.body()

    if not _verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    if x_github_event != "push":
        return {"status": "ignored", "event": x_github_event}

    payload = await request.json()
    repo = payload.get("repository", {})
    repo_id = repo.get("full_name", "")      # e.g. "org/repo-name"
    default_branch = repo.get("default_branch", "main")
    pushed_branch = payload.get("ref", "").replace("refs/heads/", "")

    # Only index pushes to the default branch
    if pushed_branch != default_branch:
        return {"status": "ignored", "reason": f"branch {pushed_branch} is not default"}

    if not repo_id:
        raise HTTPException(status_code=400, detail="Could not extract repo id")

    # Dispatch incremental update to worker (non-blocking)
    handle_push.apply_async(
        args=[repo_id, payload],
        queue="indexing",
        priority=9,
    )

    log.info("webhook.dispatched", repo_id=repo_id, branch=pushed_branch)
    return {"status": "queued", "repo_id": repo_id}
