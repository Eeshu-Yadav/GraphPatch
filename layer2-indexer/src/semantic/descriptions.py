"""
LLM-based description generation using Claude Haiku (cheapest Claude model).
Bottom-up: Function summaries → Class summaries → File summaries.
Cached by content hash so re-indexing doesn't re-call the LLM for unchanged code.
"""
from __future__ import annotations

import hashlib

import anthropic
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.graph import client as g
from src.models.symbol import FileSymbols, SymbolKind

log = structlog.get_logger(__name__)

_PROMPT_VERSION = "v3"

# Simple in-process cache (also backed by Memgraph node properties)
_local_cache: dict[str, str] = {}

# Lazy-init client
_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


def _cache_key(content: str) -> str:
    return hashlib.sha256(f"{_PROMPT_VERSION}:{content}".encode()).hexdigest()[:16]


@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=1, max=5))
def _call_claude(prompt: str) -> str:
    """Call Claude Haiku for a short description."""
    client = _get_client()
    resp = client.messages.create(
        model=settings.description_llm,
        max_tokens=150,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _call_llm(prompt: str) -> str:
    """Try Claude; skip entirely if skip_descriptions is set."""
    if settings.skip_descriptions:
        raise RuntimeError("descriptions disabled")
    return _call_claude(prompt)


def describe_function(name: str, code_snippet: str, docstring: str = "") -> str:
    """Single-function description (kept for backward compat, prefer batch)."""
    cache_key = _cache_key(code_snippet)
    if cache_key in _local_cache:
        return _local_cache[cache_key]

    prompt = (
        f"Describe this function in 1-2 sentences. Focus on what it does, not how.\n"
        f"Function name: {name}\n"
        + (f"Docstring: {docstring}\n" if docstring else "")
        + f"Code:\n```\n{code_snippet[:1500]}\n```\n"
        f"Description (1-2 sentences only):"
    )

    try:
        desc = _call_llm(prompt)
    except Exception as e:
        log.warning("llm.fn.failed", name=name, error=str(e))
        desc = docstring[:200] if docstring else f"Function {name}"

    _local_cache[cache_key] = desc
    return desc


def describe_class(name: str, method_summaries: list[str], docstring: str = "") -> str:
    """Single-class description (kept for backward compat, prefer batch)."""
    cache_key = _cache_key(name + "".join(method_summaries[:5]))
    if cache_key in _local_cache:
        return _local_cache[cache_key]

    methods_text = "\n".join(f"- {s}" for s in method_summaries[:10])
    prompt = (
        f"Describe this class in 1-2 sentences based on its methods.\n"
        f"Class name: {name}\n"
        + (f"Docstring: {docstring}\n" if docstring else "")
        + f"Methods:\n{methods_text}\n"
        f"Description (1-2 sentences only):"
    )

    try:
        desc = _call_llm(prompt)
    except Exception as e:
        log.warning("llm.class.failed", name=name, error=str(e))
        desc = docstring[:200] if docstring else f"Class {name}"

    _local_cache[cache_key] = desc
    return desc


def describe_file(path: str, symbol_summaries: list[str], lines: int) -> str:
    cache_key = _cache_key(path + "".join(symbol_summaries[:10]))
    if cache_key in _local_cache:
        return _local_cache[cache_key]

    symbols_text = "\n".join(f"- {s}" for s in symbol_summaries[:15])
    prompt = (
        f"Describe this source file in 2-3 sentences. Focus on its purpose and responsibilities.\n"
        f"File: {path}\n"
        f"Contains:\n{symbols_text}\n"
        f"Description (2-3 sentences only):"
    )

    try:
        desc = _call_llm(prompt)
    except Exception as e:
        log.warning("llm.file.failed", path=path, error=str(e))
        desc = f"Source file: {path}"

    _local_cache[cache_key] = desc
    return desc


def _describe_batch(symbols_block: list[dict]) -> dict[str, str]:
    """
    Describe up to 50 symbols in ONE Haiku call.
    Returns {symbol_name: description}.

    10,000 symbols / 50 per batch = 200 API calls instead of 10,000.
    """
    # Build compact listing: "1. func_name(args) — docstring_excerpt"
    lines = []
    for i, s in enumerate(symbols_block, 1):
        kind = "class" if s["kind"] == "class" else "def"
        doc = f" — {s['docstring'][:80]}" if s.get("docstring") else ""
        sig = s.get("signature", s["name"])
        lines.append(f"{i}. [{kind}] {sig}{doc}")

    listing = "\n".join(lines)
    prompt = (
        f"For each symbol below, write a 1-sentence description of what it does.\n"
        f"Reply with ONLY numbered lines like: 1. Does X\n\n"
        f"{listing}\n\n"
        f"Descriptions:"
    )

    try:
        raw = _call_llm(prompt)
    except Exception:
        # Fallback: use docstrings
        return {s["name"]: s.get("docstring", f"{s['name']}")[:200] for s in symbols_block}

    # Parse numbered responses
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or not line[0].isdigit():
            continue
        # "1. Does X" → index=0, desc="Does X"
        dot_pos = line.find(".")
        if dot_pos < 0:
            continue
        try:
            idx = int(line[:dot_pos]) - 1
        except ValueError:
            continue
        desc = line[dot_pos + 1:].strip().lstrip("-").strip()
        if 0 <= idx < len(symbols_block):
            result[symbols_block[idx]["name"]] = desc

    # Fill in any missed symbols
    for s in symbols_block:
        if s["name"] not in result:
            result[s["name"]] = s.get("docstring", f"{s['name']}")[:200]

    return result


def enrich_file(repo_id: str, fs: FileSymbols, file_content: str) -> None:
    """
    Generate descriptions for all symbols in a file and persist to graph.

    Uses batched Haiku calls: all symbols in one file → 1 API call (up to 50).
    For files with 50+ symbols, splits into batches of 50.
    """
    file_lines = file_content.splitlines()

    def get_signature(sym) -> str:
        """Extract function signature (first line of def/class)."""
        start = max(0, sym.line_start - 1)
        if start < len(file_lines):
            line = file_lines[start].strip()
            return line[:120]
        return sym.name

    # Build batch of all symbols in this file
    all_symbols = []
    sym_lookup = {}  # name → Symbol object
    for sym in fs.symbols:
        entry = {
            "name": sym.name,
            "kind": "class" if sym.kind == SymbolKind.CLASS else "function",
            "signature": get_signature(sym),
            "docstring": sym.docstring or "",
        }
        all_symbols.append(entry)
        sym_lookup[sym.name] = sym

    if not all_symbols:
        return

    # Batch describe: 50 symbols per API call
    BATCH_SIZE = 50
    all_descriptions: dict[str, str] = {}

    for i in range(0, len(all_symbols), BATCH_SIZE):
        batch = all_symbols[i:i + BATCH_SIZE]
        cache_key = _cache_key(fs.path + str(i))
        if cache_key in _local_cache:
            # Parse cached batch result
            import json as _json
            try:
                cached = _json.loads(_local_cache[cache_key])
                all_descriptions.update(cached)
                continue
            except Exception:
                pass

        batch_result = _describe_batch(batch)
        all_descriptions.update(batch_result)

        # Cache the batch result
        import json as _json
        _local_cache[cache_key] = _json.dumps(batch_result)

    log.info("llm.batch_described", path=fs.path, symbols=len(all_descriptions),
             batches=(len(all_symbols) + BATCH_SIZE - 1) // BATCH_SIZE)

    # Apply descriptions to symbols and persist to graph
    symbol_summaries: list[str] = []
    for sym in fs.symbols:
        desc = all_descriptions.get(sym.name, sym.docstring[:200] if sym.docstring else sym.name)
        sym.summary = desc
        symbol_summaries.append(f"{sym.name}: {desc}")

        g.run_void(
            "MATCH (s {id: $id}) SET s.summary = $summary",
            {"id": sym.id, "summary": desc},
        )

    # File-level summary (1 more API call per file)
    file_summary = describe_file(fs.path, symbol_summaries, fs.lines)
    g.run_void(
        "MATCH (f:File {repo_id: $repo_id, path: $path}) SET f.summary = $summary",
        {"repo_id": repo_id, "path": fs.path, "summary": file_summary},
    )
