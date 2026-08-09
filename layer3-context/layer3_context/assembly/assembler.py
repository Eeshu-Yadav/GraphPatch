"""
Context Assembly Engine.
Takes a Ticket -> runs multi-strategy retrieval -> RRF fusion -> returns ContextBundle.

Retrieval strategies (in order of priority):
1. Intent classification: LLM extracts precise target symbols from ticket
2. Semantic search: embed ticket -> Qdrant dense search
3. Keyword/entity lookup: extract names from ticket text -> graph lookup
4. Graph expansion: for top symbols, expand 1-hop call graph
5. Coupling: for top files, get historically co-changed files
"""
from __future__ import annotations

import os
import structlog

from layer3_context.models.ticket import Ticket
from layer3_context.models.context import ContextBundle, SymbolContext, FileContext, CoupledFile
from layer3_context.retrieval import keyword, semantic, graph
from layer3_context.assembly.ranker import rrf_fuse, deduplicate_files

log = structlog.get_logger(__name__)


def _classify_ticket(ticket: Ticket) -> dict | None:
    """
    One Sonnet call to classify ticket and extract precise target symbols.
    Replaces noisy regex keyword extraction (84% noise) with LLM understanding.

    Returns: {"type": "bug_fix", "target_symbols": ["URLValidator"],
              "target_files": ["django/core/validators.py"], "test_keywords": [...]}
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    import anthropic
    prompt = f"""Classify this GitHub issue and extract the key code targets.

Title: {ticket.title}
Body: {ticket.body[:800]}

Reply in JSON only:
{{
  "type": "bug_fix|feature|config_change|refactor|performance|docs",
  "complexity": "trivial|simple|moderate|complex",
  "target_symbols": ["exact class/function names to modify — only names clearly referenced in the issue"],
  "target_files": ["file paths mentioned or clearly implied"],
  "test_keywords": ["words describing expected behavior or test scenarios"],
  "confidence": 0.8
}}

Complexity guide:
- trivial: config change, version bump, typo fix (1 file, 1-5 lines)
- simple: single function bug fix (1-2 files, 5-20 lines)
- moderate: multi-function change (2-4 files, 20-50 lines)
- complex: architectural change (4+ files, 50+ lines)

confidence: 0.0-1.0 how certain you are about the target_symbols

Rules:
- target_symbols should be EXACT symbol names (class, function, variable) — NOT person names, NOT generic words
- Only include symbols that actually need to be MODIFIED to fix the issue
- If file paths are mentioned (even partial), include them in target_files
- test_keywords are words to search for in test files"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        import json
        result = json.loads(raw)
        log.info("assembly.ticket_classified", type=result.get("type"),
                 targets=result.get("target_symbols", []))
        return result
    except Exception as e:
        log.warning("assembly.classify_failed", error=str(e))
        return None


def _to_symbol_context(raw: dict, score: float = 0.0) -> SymbolContext:
    return SymbolContext(
        name=raw.get("name", ""),
        qualified_name=raw.get("qualified_name", raw.get("name", "")),
        file_path=raw.get("file_path", raw.get("file", "")),
        entity_type=raw.get("entity_type", "Function"),
        summary=raw.get("summary", ""),
        centrality=float(raw.get("centrality", 0.0)),
        score=float(raw.get("rrf_score", raw.get("score", score))),
        line_start=int(raw.get("line_start", 0)),
        docstring=raw.get("docstring", ""),
    )


import re

_CODE_TOKEN_RE = re.compile(r'[a-zA-Z_]\w*|[^\s]|\s+')


def _estimate_tokens(bundle_text: str) -> int:
    """Code-aware token estimate: counts identifiers, operators, and whitespace runs.
    Matches BPE tokenizers within ~10% for typical code (vs ~50% error with len//4).
    """
    return len(_CODE_TOKEN_RE.findall(bundle_text))


def assemble(
    ticket: Ticket,
    max_symbols: int = 30,
    max_files: int = 15,
    intent: dict | None = None,
) -> ContextBundle:
    """
    Main assembly pipeline.
    1. Extract entities from ticket text (or reuse cached intent)
    2. Semantic search for relevant symbols
    3. Graph lookup for explicitly mentioned entities
    4. RRF fusion (weighted by intent confidence)
    5. Expand context (callers, deps, tests, coupling) — depth scaled by complexity
    6. Build ContextBundle
    """
    strategies_used: list[str] = []

    # 0 — Intent classification (reuse cached or classify fresh)
    if not intent:
        intent = _classify_ticket(ticket)
    intent_graph_results: list[dict] = []
    if intent and intent.get("target_symbols"):
        intent_graph_results = graph.lookup_symbols(ticket.repo_id, intent["target_symbols"])
        if intent_graph_results:
            # Boost: give intent-matched symbols a high base score
            for r in intent_graph_results:
                r["_intent_match"] = True
            strategies_used.append("intent_classification")
        log.debug("assembly.intent.done", targets=intent["target_symbols"],
                  found=len(intent_graph_results))

    # 1 — Semantic retrieval (graceful fallback if embeddings service is down)
    log.info("assembly.semantic.start", ticket_id=ticket.ticket_id)
    search_text = ticket.full_text
    if intent and intent.get("test_keywords"):
        focus = " ".join(intent.get("target_symbols", []) + intent.get("test_keywords", []))
        search_text = f"{focus}. {ticket.full_text}"
    sem_results: list[dict] = []
    try:
        sem_results = semantic.search(search_text, ticket.repo_id, k=max_symbols)
        if sem_results:
            strategies_used.append("semantic_search")
    except Exception as e:
        log.warning("assembly.semantic.failed", error=str(e)[:200],
                    fallback="keyword+graph only")
    log.debug("assembly.semantic.done", count=len(sem_results))

    # 2 — Keyword/entity extraction + graph lookup (fallback for when LLM unavailable)
    entities = keyword.extract_entities(ticket.full_text)
    log.debug("assembly.keywords", symbols=entities["symbols"], files=entities["files"])

    keyword_graph_results: list[dict] = []
    if entities["symbols"]:
        keyword_graph_results = graph.lookup_symbols(ticket.repo_id, entities["symbols"])
        if keyword_graph_results:
            strategies_used.append("graph_lookup")
    log.debug("assembly.graph.done", count=len(keyword_graph_results))

    # 3 — Weighted RRF fusion
    # Intent-matched symbols get 3x weight, others get 1x
    # This means URLValidator (from intent) ranks above noise (from keywords)
    all_result_sets = [sem_results, keyword_graph_results]
    weights = [1.0, 1.0]

    if intent_graph_results:
        all_result_sets.append(intent_graph_results)
        weights.append(3.0)  # Intent matches are 3x more important

    fused = rrf_fuse(
        all_result_sets,
        id_field="name",
        k=60,
        weights=weights,
    )[:max_symbols]

    # Add explicitly mentioned files from intent classification
    if intent and intent.get("target_files"):
        for fp in intent["target_files"]:
            if not any(r.get("file_path") == fp or r.get("file") == fp for r in fused):
                entities["files"].append(fp)

    # Build SymbolContext list
    relevant_symbols = [_to_symbol_context(r) for r in fused if r.get("name")]

    # Populate code snippets for top 5 symbols (saves agent exploration budget)
    try:
        from layer4_planner.file_reader import get_repo_path
        repo_root = get_repo_path(ticket.repo_id)
        for sym in relevant_symbols[:5]:
            if sym.file_path and sym.line_start > 0:
                try:
                    fp = repo_root / sym.file_path
                    if fp.exists():
                        all_lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
                        start = max(0, sym.line_start - 1)
                        sym.code_snippet = "\n".join(all_lines[start:start + 8])
                except Exception:
                    pass
    except Exception:
        pass  # Layer 4 not available, skip snippets

    # 4 — Derive unique file paths from symbols
    file_paths = list(dict.fromkeys(
        s.file_path for s in relevant_symbols if s.file_path
    ))

    # Also add explicitly mentioned files from ticket
    for fp in entities["files"]:
        if fp not in file_paths:
            file_paths.append(fp)

    file_paths = file_paths[:max_files]

    # 5 — Get file-level context
    file_details = graph.get_file_contexts(ticket.repo_id, file_paths)
    # Map path -> detail
    file_map = {f["path"]: f for f in file_details}

    relevant_files: list[FileContext] = []
    for path in file_paths:
        detail = file_map.get(path, {})
        # Find the best symbol score for this file
        file_score = max(
            (s.score for s in relevant_symbols if s.file_path == path),
            default=0.0,
        )
        relevant_files.append(FileContext(
            path=path,
            language=detail.get("language", "unknown"),
            summary=detail.get("summary", ""),
            centrality=float(detail.get("centrality", 0.0)),
            score=file_score,
            is_test=bool(detail.get("is_test", False)),
            lines=int(detail.get("lines", 0)),
        ))
    relevant_files.sort(key=lambda f: f.score, reverse=True)

    # 6 — Call graph for top symbols (now returns callers with file paths)
    top_symbol_names = [s.name for s in relevant_symbols[:10]]
    call_graph = graph.get_call_graph(ticket.repo_id, top_symbol_names)

    # 6b — Impact assessment for target symbols (what breaks if changed)
    # Use intent targets if available (more precise), else top symbols
    impact_targets = (intent.get("target_symbols", []) if intent else []) or top_symbol_names[:5]
    impact_summary = graph.get_impact_summary(ticket.repo_id, impact_targets)

    # 7 — File dependencies
    source_files = [f.path for f in relevant_files if not f.is_test][:5]
    dependencies = graph.get_dependencies(ticket.repo_id, source_files)

    # 8 — Test files + read first 80 lines of each test (tests are the spec)
    test_files = graph.get_test_files(ticket.repo_id, source_files)

    test_code_snippets: dict[str, str] = {}
    if test_files:
        from layer4_planner.file_reader import get_repo_path
        try:
            repo_path = get_repo_path(ticket.repo_id)
            for tpath in test_files[:3]:  # max 3 test files
                abs_path = repo_path / tpath
                if abs_path.exists():
                    try:
                        content = abs_path.read_text(encoding="utf-8", errors="replace")
                        lines = content.splitlines()[:80]
                        test_code_snippets[tpath] = "\n".join(lines)
                    except Exception:
                        pass
        except Exception:
            pass

    # 9 — Git coupling
    raw_coupled = graph.get_coupled_files(ticket.repo_id, source_files)
    coupled_files = [
        CoupledFile(
            path=c["file"],
            score=float(c["score"]),
            commit_count=int(c.get("commits", c.get("commit_count", 0))),
        )
        for c in raw_coupled[:10]
    ]

    bundle = ContextBundle(
        ticket_id=ticket.ticket_id,
        repo_id=ticket.repo_id,
        relevant_symbols=relevant_symbols,
        relevant_files=relevant_files,
        call_graph=call_graph,
        dependencies=dependencies,
        test_files=test_files,
        coupled_files=coupled_files,
        token_estimate=0,
        strategies_used=strategies_used,
        test_code_snippets=test_code_snippets,
        impact_summary=impact_summary,
    )
    bundle.token_estimate = _estimate_tokens(bundle.to_prompt_text())

    log.info(
        "assembly.done",
        ticket_id=ticket.ticket_id,
        symbols=len(relevant_symbols),
        files=len(relevant_files),
        tokens=bundle.token_estimate,
        strategies=strategies_used,
    )
    return bundle
