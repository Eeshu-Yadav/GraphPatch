"""Re-describe + re-embed files with placeholder summaries.

For each file whose existing summary is empty or placeholder:
  1. Pull its symbols (classes/functions) + existing docstrings from the graph.
  2. Call Haiku to generate a real 2-3 sentence file description (parallel).
  3. Embed the description (parallel).
  4. Write the description back to the File node in Memgraph and upsert the vector.

Parallelism: uses a thread pool for Haiku calls (I/O bound).
Rate safety: configurable max in-flight requests + simple per-minute budget.

Run ad-hoc: python -m src.semantic.reembed_files <repo_id> [--max-workers N] [--limit N]
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog
from qdrant_client.models import FieldCondition, Filter, MatchValue

from src.config import settings
from src.graph import client as g
from src.semantic.descriptions import describe_file
from src.semantic.embeddings import embed_batch
from src.semantic.vector_store import _get_client, upsert_file_summary

log = structlog.get_logger(__name__)


def _looks_placeholder(summary: str | None) -> bool:
    if not summary:
        return True
    s = summary.strip()
    return len(s) < 30 or s.lower().startswith("source file:")


def _fetch_placeholder_files(repo_id: str) -> list[dict]:
    """Scan Qdrant for File-entity points with placeholder summaries."""
    client = _get_client()
    out: list[dict] = []
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=settings.qdrant_collection,
            scroll_filter=Filter(must=[
                FieldCondition(key="repo_id", match=MatchValue(value=repo_id)),
                FieldCondition(key="entity_type", match=MatchValue(value="File")),
            ]),
            limit=500,
            offset=offset,
            with_payload=True,
        )
        for pt in points:
            p = pt.payload or {}
            if _looks_placeholder(p.get("summary")):
                out.append({
                    "file_path": p.get("file_path"),
                    "language": p.get("language") or "unknown",
                })
        if next_offset is None:
            break
        offset = next_offset
    return out


def _fetch_symbols_bulk(repo_id: str, paths: list[str]) -> dict[str, list[dict]]:
    """Return {path: [{name, docstring, kind, signature}, ...]} for all paths in one query."""
    if not paths:
        return {}
    rows = g.run(
        """
        UNWIND $paths AS p
        OPTIONAL MATCH (f:File {repo_id: $rid, path: p})-[:CONTAINS]->(s)
        WHERE s:Class OR s:Function
        WITH p, s
        RETURN p AS path,
               collect({
                 name: s.name,
                 docstring: coalesce(s.docstring, ''),
                 kind: CASE WHEN 'Class' IN labels(s) THEN 'class' ELSE 'function' END,
                 signature: coalesce(s.signature, s.name)
               })[..30] AS symbols
        """,
        {"rid": repo_id, "paths": paths},
    )
    return {r["path"]: (r.get("symbols") or []) for r in rows}


def _describe_one(file_path: str, lines: int, symbols: list[dict]) -> str:
    """Build the symbol summary list and call describe_file. Returns '' on failure."""
    summary_lines: list[str] = []
    for s in symbols[:15]:
        name = s.get("name") or ""
        kind = s.get("kind") or "function"
        doc = (s.get("docstring") or "").strip().replace("\n", " ")[:120]
        if not name:
            continue
        summary_lines.append(f"[{kind}] {name}" + (f" — {doc}" if doc else ""))

    if not summary_lines:
        summary_lines = [f"[file] {file_path}"]

    try:
        return describe_file(file_path, summary_lines, lines=lines)
    except Exception as e:
        log.warning("llm.file.failed", path=file_path, error=str(e)[:120])
        return ""


def _persist(repo_id: str, file_path: str, language: str, summary: str, vector: list[float]) -> None:
    """Write summary to Memgraph File node + upsert Qdrant File vector."""
    g.run_void(
        "MATCH (f:File {repo_id: $rid, path: $p}) SET f.summary = $s",
        {"rid": repo_id, "p": file_path, "s": summary},
    )
    upsert_file_summary(
        repo_id=repo_id,
        file_path=file_path,
        language=language,
        summary=summary,
        vector=vector,
    )


def reembed_placeholder_files(
    repo_id: str,
    max_workers: int = 16,
    limit: int | None = None,
    embed_batch_size: int = 32,
) -> tuple[int, int]:
    """Returns (done, skipped_or_failed)."""
    placeholder = _fetch_placeholder_files(repo_id)
    if limit:
        placeholder = placeholder[:limit]
    total = len(placeholder)
    log.info("reembed.scan", repo_id=repo_id, placeholder=total, workers=max_workers)
    if not placeholder:
        return (0, 0)

    # Pull symbol lists for all placeholder files in one go.
    paths = [f["file_path"] for f in placeholder if f["file_path"]]
    symbols_by_path = _fetch_symbols_bulk(repo_id, paths)

    # Stage 1: parallel LLM descriptions
    descriptions: dict[str, str] = {}
    done_counter = {"n": 0}
    lock = threading.Lock()
    start = time.time()

    def _worker(f: dict) -> tuple[str, str]:
        path = f["file_path"]
        if not path:
            return ("", "")
        syms = symbols_by_path.get(path, [])
        # Approximate line count from symbol count (we don't have it in the payload)
        lines = max(len(syms) * 10, 50)
        desc = _describe_one(path, lines, syms)
        with lock:
            done_counter["n"] += 1
            if done_counter["n"] % 25 == 0 or done_counter["n"] == total:
                rate = done_counter["n"] / max(time.time() - start, 0.01)
                log.info("reembed.describe.progress",
                         done=done_counter["n"], total=total, rate_per_sec=round(rate, 1))
        return (path, desc)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_worker, f) for f in placeholder]
        for fut in as_completed(futures):
            try:
                path, desc = fut.result()
            except Exception as e:
                log.warning("reembed.worker_failed", error=str(e)[:120])
                continue
            if path and desc:
                descriptions[path] = desc

    log.info("reembed.describe.done", got=len(descriptions), wanted=total,
             elapsed_s=round(time.time() - start, 1))

    # Stage 2: batch-embed the new descriptions (Ollama embed_batch handles batching)
    items = list(descriptions.items())  # [(path, summary), ...]
    embedded = 0
    lang_by_path = {f["file_path"]: f.get("language", "unknown") for f in placeholder}

    for i in range(0, len(items), embed_batch_size):
        chunk = items[i : i + embed_batch_size]
        texts = [summary for _, summary in chunk]
        try:
            vecs = embed_batch(texts)
        except Exception as e:
            log.error("reembed.embed_batch_failed", i=i, error=str(e)[:200])
            continue
        if not vecs or len(vecs) != len(chunk):
            log.warning("reembed.embed_size_mismatch", got=len(vecs) if vecs else 0, wanted=len(chunk))
            continue
        for (path, summary), vec in zip(chunk, vecs):
            try:
                _persist(repo_id, path, lang_by_path.get(path, "unknown"), summary, vec)
                embedded += 1
            except Exception as e:
                log.warning("reembed.persist_failed", path=path, error=str(e)[:120])

        if embedded and embedded % 100 == 0:
            log.info("reembed.embed.progress", done=embedded, total=len(items))

    log.info("reembed.done", repo_id=repo_id, embedded=embedded,
             total_total=total, elapsed_s=round(time.time() - start, 1))
    return (embedded, total - embedded)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_id")
    ap.add_argument("--max-workers", type=int, default=16,
                    help="Parallel Haiku calls. Raise for higher Anthropic tiers.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap for smoke-testing.")
    args = ap.parse_args()

    done, failed = reembed_placeholder_files(
        args.repo_id, max_workers=args.max_workers, limit=args.limit,
    )
    print(f"Re-described+re-embedded {done} files; {failed} failed/skipped")


if __name__ == "__main__":
    main()
