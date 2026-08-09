"""
Vague-ify a SWE-bench problem_statement before feeding it to the agent.

SWE-bench Verified tickets typically name the exact file path, traceback, and
sometimes the root cause. That tells the agent where to look — defeating the
purpose of testing how well it *discovers* the bug.

This module rewrites a ticket so that:
  • Specific file paths are removed (django/db/backends/sqlite3/base.py → "the database backend")
  • Module paths in tracebacks are stripped (but symptom description stays)
  • Class / function names are generalized if they're specific identifiers
  • The symptom / behavior / reproduction steps stay intact

Uses Haiku (cheap, deterministic low-temp) — a single call per ticket.
Total cost for 20 tickets ≈ $0.02.
"""
from __future__ import annotations

import structlog
import anthropic

log = structlog.get_logger(__name__)


_VAGIFY_SYSTEM = """\
You rewrite software bug reports to REMOVE specific code pointers while preserving \
the symptom description.

REMOVE:
- Specific file paths (e.g. "django/db/backends/sqlite3/base.py")
- Full module paths in tracebacks (keep the error message, drop the File: lines)
- Class or function names that are specific project identifiers \
(keep common words like "list", "dict", "model", "request")
- "Root Cause: ..." sections that name a file or function
- Stack trace frames (keep the final exception message, not the chain)
- Git commit hashes, PR numbers, branch names
- Code snippets longer than 2 lines IF they directly show the fix location

KEEP:
- Description of what the user did (steps to reproduce)
- Description of what happens vs what should happen
- Error messages (but without file paths pointing at the cause)
- Short code snippets that show the PUBLIC API call, not internals
- Examples of inputs/outputs

TONE: like a user or support engineer describing the problem — they know the \
symptom but NOT the internal file layout.

OUTPUT: plain text, the rewritten ticket body. No preamble, no quotes, no markdown \
headers. Just the rewrite.
"""


def vagify(title: str, body: str, client: anthropic.Anthropic,
           model: str = "claude-haiku-4-5-20251001",
           max_chars: int = 3000) -> tuple[str, str]:
    """
    Rewrite (title, body) into a vague version. Returns (vague_title, vague_body).
    On any error, returns the original (fail-open).
    """
    body = body[:max_chars]
    prompt = f"BUG REPORT TITLE:\n{title}\n\nBUG REPORT BODY:\n{body}\n\nRewrite the body only:"
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_VAGIFY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
        )
        vague_body = resp.content[0].text.strip()
        # Also vagify the title — drop file paths, keep the symptom
        vague_title = _strip_paths_from_title(title)
        log.info("vagify.done", orig_chars=len(body), vague_chars=len(vague_body))
        return vague_title, vague_body
    except Exception as e:
        log.info("vagify.failed", error=str(e)[:100])
        return title, body   # fail-open: original ticket


def _strip_paths_from_title(title: str) -> str:
    """Drop path-like tokens from the title (rare, but cheap to do deterministically)."""
    import re
    # Strip things like "django__django-13807" → just remove slashes
    return re.sub(r"[a-zA-Z_]+/[a-zA-Z_/]+\.py", "", title).strip()
