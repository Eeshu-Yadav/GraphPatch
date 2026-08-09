"""
Issue complexity classifier — 4-layer fallback chain.

Layer 1: Heuristic (0ms, no API)     — score text signals, return tier + confidence
Layer 2: LLM assist (~$0.0003 haiku) — fires when confidence < 0.4
Layer 3: Safe default = MEDIUM       — fires when API fails or still uncertain
Layer 4: Post-explore upgrade        — fires after done_exploring (ground truth)
                                       implemented in agent_adaptive.py

The rule: always escalate when uncertain, never downgrade.
Over-classify costs 3-5 extra turns.
Under-classify wastes the entire run.
"""
from __future__ import annotations

import re
import structlog

log = structlog.get_logger(__name__)


# ── Scoring tables ─────────────────────────────────────────────────────────────

# Push toward HARD
_DESIGN_KW = {
    "refactor", "redesign", "architecture", "migrate", "rewrite",
    "deprecate", "introduce", "new feature", "add support", "implement",
    "rework", "overhaul", "replace", "new backend", "new handler",
    "new middleware", "new transform", "new endpoint",
}
_MULTI_KW = {
    "multiple", "several", "all backends", "all databases", "all files",
    "across", "throughout", "both", "each backend", "every backend",
    "any backend",
}

# Push toward EASY
_ERROR_KW = {
    "syntax error", "key error", "type error", "attribute error",
    "import error", "nameerror", "exception", "traceback", "crash",
    "raises", "stacktrace", "throws",
}
_SPECIFIC_KW = {
    "in line", "in function", "in class", "at line", "specifically in",
    "the regex", "this method", "in the validator", "this function",
    "line number", "on line",
}

# ── Layer 1: Heuristic ─────────────────────────────────────────────────────────

def _score(title: str, body: str) -> tuple[int, dict]:
    """Return (score, signals). score <= 0 = easy, 1-4 = medium, 5+ = hard."""
    text = (title + " " + body).lower()
    score = 0
    signals: dict = {}

    # File references explicitly mentioned (+1 per 2 files, +3 for 3+)
    file_refs = len(re.findall(r'\b\w+\.(py|js|ts|go|rs|java|rb|cpp|c|h)\b', text))
    signals["file_refs"] = file_refs
    if file_refs >= 3:   score += 3
    elif file_refs >= 2: score += 1

    # Architecture / design scope
    has_design = any(kw in text for kw in _DESIGN_KW)
    signals["has_design"] = has_design
    if has_design: score += 3

    # Multi-component scope
    has_multi = any(kw in text for kw in _MULTI_KW)
    signals["has_multi"] = has_multi
    if has_multi: score += 2

    # Body length (longer = more context = more complex usually)
    body_words = len(body.split())
    signals["body_words"] = body_words
    if body_words > 200: score += 1
    if body_words > 400: score += 1

    # Error keywords (localized bug — push toward easy)
    has_error = any(kw in text for kw in _ERROR_KW)
    signals["has_error"] = has_error
    if has_error: score -= 1

    # Specific location given (agent already has a roadmap)
    has_specific = any(kw in text for kw in _SPECIFIC_KW)
    signals["has_specific"] = has_specific
    if has_specific: score -= 2

    return score, signals


def classify_issue(title: str, body: str) -> tuple[str, float, dict]:
    """
    Layer 1 — heuristic classifier.

    Returns (tier, confidence, signals)
      tier       = "easy" | "medium" | "hard"
      confidence = 0.0–1.0
      signals    = what fired (for logging)
    """
    score, signals = _score(title, body)
    body_words = signals["body_words"]

    # Assign tier
    if score <= 0:   tier = "easy"
    elif score <= 4: tier = "medium"
    else:            tier = "hard"

    # Confidence: distance from nearest boundary, normalised to 0-1
    if tier == "easy":
        raw = abs(score) / 3.0          # how far below 0
    elif tier == "medium":
        raw = min(abs(score), abs(score - 5)) / 2.0  # how far from 0 or 5
    else:
        raw = (score - 4) / 3.0         # how far above 4

    confidence = min(1.0, max(0.0, raw))

    # Force low confidence when signal is thin
    no_strong_signal = (
        not signals["has_design"]
        and not signals["has_error"]
        and signals["file_refs"] == 0
    )
    if body_words < 50:       confidence = 0.0
    if body_words < 10:       confidence = 0.0
    if no_strong_signal:      confidence = min(confidence, 0.3)

    log.info("classifier.heuristic",
             tier=tier, confidence=round(confidence, 2),
             score=score, signals=signals)

    return tier, confidence, signals


# ── Layer 2: LLM classifier (haiku, ~$0.0003) ─────────────────────────────────

_CLASSIFIER_PROMPT = """Classify this software bug ticket by how many source files likely need to change.

Reply with EXACTLY one word — easy, medium, or hard — and nothing else.

easy   = 1 file, localized bug, clear location (regex fix, typo, off-by-one)
medium = 2-3 files, scope unclear but contained, behavior inconsistency
hard   = 4+ files, or architectural/design change, multi-component, new feature

Ticket title: {title}
Ticket body: {body}

Classification:"""


def llm_classify(title: str, body: str, client) -> str:
    """
    Layer 2 — single haiku call to classify when heuristic is uncertain.
    Returns "easy" | "medium" | "hard". Falls back to "medium" on any error.
    """
    try:
        import anthropic as _anthropic
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": _CLASSIFIER_PROMPT.format(
                    title=title,
                    body=body[:600],
                ),
            }],
        )
        word = resp.content[0].text.strip().lower().rstrip(".")
        if word in ("easy", "medium", "hard"):
            log.info("classifier.llm", result=word)
            return word
        log.info("classifier.llm.unexpected", raw=word)
        return "medium"
    except Exception as e:
        log.info("classifier.llm.failed", error=str(e)[:80])
        return "medium"   # Layer 3


# ── Combined entry point ───────────────────────────────────────────────────────

def route_issue(
    title: str,
    body: str,
    client=None,
    confidence_threshold: float = 0.4,
) -> tuple[str, dict]:
    """
    Full 3-layer routing (Layer 4 is in the agent loop).

    Returns (tier, signals) where tier = "easy" | "medium" | "hard".

    Layer 1: heuristic  — always runs
    Layer 2: haiku LLM  — runs when confidence < threshold AND client provided
    Layer 3: default    — "medium" when both uncertain or client unavailable
    """
    tier, confidence, signals = classify_issue(title, body)

    if confidence >= confidence_threshold:
        log.info("classifier.route", layer=1, tier=tier, confidence=round(confidence, 2))
        return tier, signals

    # Layer 2
    if client is not None:
        llm_tier = llm_classify(title, body, client)
        log.info("classifier.route", layer=2, tier=llm_tier,
                 heuristic_tier=tier, confidence=round(confidence, 2))
        return llm_tier, signals

    # Layer 3 — safe default
    log.info("classifier.route", layer=3, tier="medium",
             reason="low_confidence_no_client")
    return "medium", signals


# ── Layer 4: post-explore upgrade (called from agent loop) ────────────────────

def upgrade_config(
    files_to_modify: list[str],
    current_tier: str,
) -> str | None:
    """
    Layer 4 — called after done_exploring with the real file list.
    Returns the new tier if an upgrade is needed, or None if no change.

    Rules (always escalate, never downgrade):
      4+ files and not already hard  → hard
      2+ files and currently easy    → medium
    """
    n = len(files_to_modify)

    if n >= 4 and current_tier != "hard":
        log.info("classifier.upgrade", files=n, from_tier=current_tier, to_tier="hard")
        return "hard"

    if n >= 2 and current_tier == "easy":
        log.info("classifier.upgrade", files=n, from_tier="easy", to_tier="medium")
        return "medium"

    return None  # no upgrade needed
