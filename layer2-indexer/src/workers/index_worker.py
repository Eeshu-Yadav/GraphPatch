"""
Core index worker tasks.

index_file   — parse + graph + embed a single file (idempotent via hash check)
full_index   — clone repo + index all files
incremental  — webhook-triggered update for changed files
"""
from __future__ import annotations

import structlog

from src.workers.celery_app import app
from src.config import settings
from src.parsing.detector import detect_language
from src.parsing.filter import should_skip
from src.parsing.extractor import extract
from src.graph import builder as graph_builder, client as g
from src.semantic import descriptions as desc_module, embeddings, vector_store
from src.models.symbol import Language

log = structlog.get_logger(__name__)


@app.task(name="src.workers.index_worker.index_file", bind=True, max_retries=3)
def index_file(self, repo_id: str, file_path: str, repo_root: str) -> dict:
    """
    Index a single file: parse → graph → descriptions → embed.
    Idempotent: skips if content hash unchanged.
    Uses Redis distributed lock to prevent concurrent writes to same file.
    """
    import redis as redis_lib

    redis_client = redis_lib.from_url(settings.redis_url)
    lock_key = f"index_lock:{repo_id}:{file_path.replace('/', '_')}"

    # Skip distributed lock in eager/sync mode — single process, no concurrency
    from celery import current_app as _celery_app
    eager_mode = _celery_app.conf.task_always_eager

    if not eager_mode:
        acquired = redis_client.set(lock_key, "1", ex=30, nx=True)
        if not acquired:
            log.info("index.lock.wait", file=file_path)
            raise self.retry(countdown=35, max_retries=3)
    else:
        acquired = True

    try:
        from pathlib import Path
        abs_path = Path(repo_root) / file_path

        if not abs_path.exists():
            log.warning("index.file.missing", file=file_path)
            return {"status": "skipped", "reason": "file_not_found"}

        content = abs_path.read_text(encoding="utf-8", errors="replace")
        size = abs_path.stat().st_size

        # Language detection
        language = detect_language(file_path, content)
        if language == Language.UNKNOWN:
            return {"status": "skipped", "reason": "unsupported_language"}

        # File filter check
        if should_skip(file_path, size):
            return {"status": "skipped", "reason": "filtered"}

        # Idempotency check — skip if content unchanged
        import hashlib
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        stored_hash = g.get_file_hash(repo_id, file_path)
        if stored_hash == content_hash:
            return {"status": "skipped", "reason": "unchanged"}

        # --- PARSE ---
        file_symbols = extract(file_path, content, language)

        # --- GRAPH: delete old subgraph + insert fresh ---
        g.delete_file_subgraph(repo_id, file_path)
        graph_builder.insert_all(repo_id, file_symbols)

        # --- SEMANTIC: generate descriptions (Gemini Flash) ---
        desc_module.enrich_file(repo_id, file_symbols, content)

        # --- EMBED: generate vectors (nomic-embed-text) + upsert to Qdrant ---
        if file_symbols.symbols:
            texts = [
                f"{sym.qualified_name}: {sym.summary or sym.docstring or sym.name}"
                for sym in file_symbols.symbols
            ]
            vectors = embeddings.embed_texts(texts)
            vector_store.delete_file_entities(repo_id, file_path)
            vector_store.upsert_file_entities(repo_id, file_symbols, vectors)

            # Also embed the file-level summary
            from src.graph.queries import get_file_summary
            fs = get_file_summary(repo_id, file_path)
            if fs and fs.summary:
                file_vector = embeddings.embed_single(fs.summary)
                vector_store.upsert_file_summary(
                    repo_id, file_path, language.value, fs.summary, file_vector
                )

        log.info(
            "index.file.done",
            repo_id=repo_id,
            file=file_path,
            symbols=len(file_symbols.symbols),
        )
        return {"status": "indexed", "symbols": len(file_symbols.symbols)}

    except Exception as exc:
        log.error("index.file.error", file=file_path, error=str(exc))
        if eager_mode:
            raise  # in sync mode just raise directly, no retry
        raise self.retry(exc=exc, countdown=10)
    finally:
        if not eager_mode:
            redis_client.delete(lock_key)


@app.task(name="src.workers.index_worker.full_index")
def full_index(repo_id: str, repo_url: str, branch: str = "main") -> dict:
    """
    Full index of a repository.
    1. Clone/pull repo
    2. Setup graph indexes + Qdrant collection
    3. Upsert repository node
    4. Index all files in parallel (fan out to index_file tasks)
    5. Stitch imports + run PageRank
    6. Compute git coupling
    """
    from src.vcs.clone import clone_or_pull, list_all_files
    from src.graph.stitcher import stitch_repo
    from src.graph.pagerank import run_pagerank
    from src.graph import client as g

    log.info("full_index.starting", repo_id=repo_id)

    # Setup infra
    g.setup_indexes()
    vector_store.setup_collection()

    # Clone / pull
    repo_path = clone_or_pull(repo_url, repo_id, branch)
    all_files = list_all_files(repo_path)

    # Upsert repo node
    graph_builder.upsert_repository(repo_id, repo_id.split("/")[-1], repo_url, branch)

    # Fan out: index each file as a separate task
    tasks = []
    skipped = 0
    for file_path in all_files:
        from src.parsing.detector import detect_language
        from src.parsing.filter import should_skip

        if should_skip(file_path):
            skipped += 1
            continue
        language = detect_language(file_path)
        if language.value == "unknown":
            skipped += 1
            continue

        tasks.append(
            index_file.s(repo_id, file_path, str(repo_path))
        )

    log.info("full_index.dispatched", repo_id=repo_id, tasks=len(tasks), skipped=skipped)

    # Run in parallel with Celery group
    from celery import group
    job = group(tasks)
    result = job.apply_async()
    result.get(timeout=600, propagate=False)  # wait up to 10 min

    # After all files indexed: stitch + pagerank
    all_files_set = list_all_files(repo_path)
    stitch_repo(repo_id, str(repo_path), all_files_set)
    run_pagerank(repo_id)

    # Git coupling (runs on worker, not blocking full index)
    from src.workers.coupling_worker import recompute_coupling
    recompute_coupling.delay(repo_id, str(repo_path))

    log.info("full_index.done", repo_id=repo_id)
    return {"status": "done", "repo_id": repo_id, "files_indexed": len(tasks), "skipped": skipped}
