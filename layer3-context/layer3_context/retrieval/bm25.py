"""BM25 + semantic + graph rerank for bug-report → source-file retrieval.

Stages:
  1. BM25 over per-file documents built from path tokens + symbol names + summaries.
     [Zhou et al. 2012, "BugLocator"]
  2. Identifier-mention boost: files defining symbols named in the ticket get weighted up.
     [Wong et al. 2014, "Boosting Bug-Report-Oriented Fault Localization"]
  3. Semantic similarity: embed ticket text, compare against file-summary embeddings in Qdrant.
     Catches cross-layer bugs where no term overlap exists but the files share purpose.
  4. Graph rerank: top candidates that are git-coupled with each other get a small mutual boost.
     [Youm et al. 2015, uses change histories as a retrieval signal]

Semantic stage degrades gracefully if Qdrant/Ollama is unavailable — BM25 + graph still run.
"""
from __future__ import annotations

import re
from functools import lru_cache

import structlog
from rank_bm25 import BM25Okapi

log = structlog.get_logger(__name__)


_WORD_RE = re.compile(r"[a-zA-Z0-9]+")
_IDENTIFIER_RE = re.compile(r"\b[A-Z][a-zA-Z0-9]{2,}\b|\b[a-z][a-z0-9_]{2,}\b")
_PATH_RE = re.compile(r"[a-zA-Z0-9_/\-]+\.[a-z]{1,4}\b")


def _tokenize(text: str) -> list[str]:
    """Split on non-alphanumeric, lowercase, drop 1-char tokens."""
    return [t.lower() for t in _WORD_RE.findall(text) if len(t) >= 2]


def _doc_text(path: str, symbols: list[str], summary: str | None) -> str:
    """Build the retrieval document for a file."""
    parts = [path]
    parts.extend(re.split(r"[/_\.\-]", path))
    parts.extend(s for s in symbols if s)
    if summary:
        parts.append(summary)
    return " ".join(parts)


@lru_cache(maxsize=5)
def _build_index(repo_id: str) -> tuple[BM25Okapi, list[str], dict[str, float]]:
    """Build a BM25 index from graph data. Cached per repo (rebuild only on re-index)."""
    from src.graph import client as g

    rows = g.run(
        """
        MATCH (f:File {repo_id: $rid})
        OPTIONAL MATCH (f)-[:CONTAINS]->(s)
        WHERE s:Class OR s:Function
        RETURN f.path AS path,
               f.summary AS summary,
               coalesce(f.centrality, 0.0) AS centrality,
               collect(DISTINCT s.name)[..25] AS symbols
        """,
        {"rid": repo_id},
    )

    paths: list[str] = []
    corpus: list[list[str]] = []
    centralities: dict[str, float] = {}

    for r in rows:
        path = r.get("path")
        if not path:
            continue
        symbols = [s for s in (r.get("symbols") or []) if s]
        summary = r.get("summary") or ""
        tokens = _tokenize(_doc_text(path, symbols, summary))
        if not tokens:
            continue
        paths.append(path)
        corpus.append(tokens)
        centralities[path] = r.get("centrality") or 0.0

    bm25 = BM25Okapi(corpus) if corpus else BM25Okapi([[""]])
    log.info("bm25.index_built", repo_id=repo_id, files=len(paths))
    return bm25, paths, centralities


def invalidate_index(repo_id: str | None = None) -> None:
    """Call after re-indexing the repo so the cache rebuilds."""
    _build_index.cache_clear()


def locate_files(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
    top_k: int = 15,
    min_score: float = 0.05,
) -> list[dict]:
    """Rank files by relevance to the ticket using BM25 + identifier boost + graph rerank.

    Returns a list of dicts with: path, score, bm25, centrality, identifier_hits, coupling_boost.
    Empty list if nothing scores above min_score.
    """
    bm25, paths, centralities = _build_index(repo_id)
    if not paths:
        return []

    text = f"{ticket_title}\n{ticket_body}"
    query_tokens = _tokenize(text)
    if not query_tokens:
        return []

    # Stage 1: BM25 scores
    bm25_raw = bm25.get_scores(query_tokens)
    max_bm25 = max(bm25_raw) if len(bm25_raw) and max(bm25_raw) > 0 else 1.0

    # Stage 2: identifier boost — files defining symbols named in the ticket
    mentioned = set(_IDENTIFIER_RE.findall(text))
    _COMMON_STOPWORDS = {
        "the", "and", "for", "with", "this", "that", "from", "when", "where", "what",
        "test", "tests", "error", "bug", "fix", "issue", "should", "would", "could",
        "function", "method", "class", "object", "value", "result", "True", "False", "None",
    }
    mentioned = {m for m in mentioned if m not in _COMMON_STOPWORDS}

    id_hits: dict[str, int] = {}
    if mentioned:
        from src.graph import client as g
        id_rows = g.run(
            """
            MATCH (n) WHERE (n:Class OR n:Function) AND n.repo_id = $rid
              AND n.name IN $names
            RETURN n.file_path AS p
            """,
            {"rid": repo_id, "names": list(mentioned)[:40]},
        )
        for r in id_rows:
            p = r.get("p")
            if p:
                id_hits[p] = id_hits.get(p, 0) + 1
    max_id = max(id_hits.values()) if id_hits else 1

    # Path-mention boost: ticket references a file path explicitly
    mentioned_paths = set(_PATH_RE.findall(text))

    # Stage 3: semantic similarity against file-summary embeddings
    # (LLM-generated summaries in Qdrant — catches cross-layer bugs where no term overlap exists)
    semantic_scores: dict[str, float] = {}
    try:
        from src.semantic.embeddings import embed_single
        from src.semantic.vector_store import search as qdrant_search
        vec = embed_single(text[:2000])
        if vec:
            sem_hits = qdrant_search(
                query_vector=vec,
                repo_id=repo_id,
                entity_types=["File"],
                limit=200,
                min_score=0.0,
            )
            for h in sem_hits:
                fp = h.get("file_path")
                if fp:
                    semantic_scores[fp] = h.get("score") or 0.0
    except Exception as e:
        log.warning("bm25.semantic_failed", error=str(e)[:200])
    max_semantic = max(semantic_scores.values()) if semantic_scores else 1.0

    # Stage 4: weighted combine
    max_centrality = max(centralities.values()) if centralities else 1.0

    # Union of paths in BM25 corpus and paths that scored well semantically
    # (semantic can surface files with summaries that BM25 missed because none of the
    # ticket's query tokens appear in the file's symbols/path)
    all_paths = set(paths) | set(semantic_scores.keys())
    path_index = {p: i for i, p in enumerate(paths)}

    candidates: list[dict] = []
    for path in all_paths:
        idx = path_index.get(path)
        bm25_n = (bm25_raw[idx] / max_bm25) if idx is not None and max_bm25 > 0 else 0.0
        cent_n = (centralities.get(path, 0.0) / max_centrality) if max_centrality > 0 else 0.0
        id_n = (id_hits.get(path, 0) / max_id) if max_id > 0 else 0.0
        path_hit = any(mp in path for mp in mentioned_paths)
        sem_raw = semantic_scores.get(path, 0.0)
        sem_n = (sem_raw / max_semantic) if max_semantic > 0 else 0.0

        score = (
            0.40 * bm25_n
            + 0.15 * id_n
            + 0.25 * sem_n
            + 0.15 * cent_n
            + (0.05 if path_hit else 0.0)
        )

        if score >= min_score:
            candidates.append({
                "path": path,
                "score": round(score, 3),
                "bm25": round(bm25_n, 3),
                "semantic": round(sem_n, 3),
                "centrality": round(cent_n, 3),
                "identifier_hits": id_hits.get(path, 0),
                "path_mention": path_hit,
                "coupling_boost": 0.0,
            })

    # Stage 4: graph coupling rerank among top candidates
    top_for_rerank = sorted(candidates, key=lambda r: r["score"], reverse=True)[:30]
    top_paths = {r["path"]: r for r in top_for_rerank}
    if len(top_paths) > 1:
        from src.graph import client as g
        coupling_rows = g.run(
            """
            MATCH (a:File {repo_id: $rid})-[c:COUPLED_WITH]->(b:File {repo_id: $rid})
            WHERE a.path IN $paths AND b.path IN $paths
            RETURN a.path AS a, b.path AS b, coalesce(c.jaccard, c.score, 0.0) AS j
            """,
            {"rid": repo_id, "paths": list(top_paths.keys())},
        )
        for r in coupling_rows:
            j = r.get("j") or 0.0
            if j >= 0.1:
                boost = 0.10 * j
                top_paths[r["a"]]["score"] = round(top_paths[r["a"]]["score"] + boost, 3)
                top_paths[r["a"]]["coupling_boost"] = round(
                    top_paths[r["a"]]["coupling_boost"] + boost, 3
                )

    final = sorted(candidates, key=lambda r: r["score"], reverse=True)[:top_k]
    return final
