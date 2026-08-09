from __future__ import annotations
import structlog
from src.workers.celery_app import app

log = structlog.get_logger(__name__)


@app.task(name="src.workers.coupling_worker.recompute_coupling")
def recompute_coupling(repo_id: str, repo_path: str) -> None:
    from src.vcs.coupling import compute_and_store_coupling
    from pathlib import Path
    compute_and_store_coupling(repo_id, Path(repo_path))


@app.task(name="src.workers.coupling_worker.recompute_all_coupling")
def recompute_all_coupling() -> None:
    """Daily job: recompute coupling for all indexed repos."""
    from src.graph.client import run
    repos = run("MATCH (r:Repository) RETURN r.id AS id, r.default_branch AS branch")
    for repo in repos:
        from src.vcs.clone import get_repo_path
        repo_path = get_repo_path(repo["id"])
        if repo_path.exists():
            recompute_coupling.delay(repo["id"], str(repo_path))
            log.info("coupling.queued", repo_id=repo["id"])
