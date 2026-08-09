"""
Adaptive agent — wraps v4 with automatic tier routing.

Classifies the ticket (4-layer fallback), picks Config A/B/C,
then delegates to run_lean_agent_v4 with the right settings.

Usage:
    from lean_agent.agent_adaptive import run_adaptive_agent

    result = run_adaptive_agent(
        ticket_title="Invalid URLs are getting through validation",
        ticket_body="...",
        repo_id="django/django",
        repo_path="/path/to/repo",
    )
    print(result["tier"])            # "easy" / "medium" / "hard"
    print(result["tier_upgraded_from"])  # set if Layer 4 upgraded it
    print(result["files_changed"])
    print(result["success"])
"""
from __future__ import annotations

import os
import time

import structlog
import anthropic

from lean_agent.classifier import route_issue
from lean_agent.agent_v4 import run_lean_agent_v4, tools_for_tier

log = structlog.get_logger(__name__)


# ── Config per tier ────────────────────────────────────────────────────────────
# max_turns       = explore cap + write cap (v4 uses a single total counter)
# nudge_after_write = write turns after first write_file before finish nudge
# These are tuned from test data (see ADAPTIVE_CONTEXT_FULL.md)

_CONFIGS = {
    "easy": {
        "max_turns":        40,
        "nudge_after_write": 10,
    },
    "medium": {
        "max_turns":        55,
        "nudge_after_write": 15,
    },
    "hard": {
        "max_turns":        80,
        "nudge_after_write": 20,
    },
}


def _tools_for_tier(tier: str) -> list[dict]:
    """Exposed so agent_v4's upgrade path can call it without circular import."""
    return tools_for_tier(tier)


# ── Main entry point ───────────────────────────────────────────────────────────

def run_adaptive_agent(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
    repo_path: str,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514",
    force_tier: str | None = None,   # bypass classifier — for testing
) -> dict:
    """
    Classify the ticket, pick config, run v4.

    Returns v4 result dict plus:
      "tier_classified"  — what the classifier returned
      "tier_final"       — may differ if Layer 4 upgraded it
      "classifier_signals" — what heuristic signals fired
      "elapsed_s"        — wall time
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    start = time.time()

    # ── Layers 1-3: classify ───────────────────────────────────────────────────
    if force_tier:
        tier = force_tier
        signals: dict = {"forced": True}
        log.info("adaptive.tier_forced", tier=tier)
    else:
        tier, signals = route_issue(ticket_title, ticket_body, client)
        log.info("adaptive.tier_classified", tier=tier, signals=signals)

    config = _CONFIGS[tier]

    log.info("adaptive.running",
             tier=tier,
             max_turns=config["max_turns"],
             nudge_after=config["nudge_after_write"],
             graph_tools=[t["name"] for t in tools_for_tier(tier)])

    # ── Run v4 with tier config ────────────────────────────────────────────────
    result = run_lean_agent_v4(
        ticket_title=ticket_title,
        ticket_body=ticket_body,
        repo_id=repo_id,
        repo_path=repo_path,
        api_key=api_key,
        model=model,
        max_turns=config["max_turns"],
        tier=tier,
        extra_write_tools=None,          # None → infer from tier inside v4
        nudge_after_write=config["nudge_after_write"],
        enable_upgrade=True,             # always enable Layer 4
    )

    elapsed = round(time.time() - start, 1)

    result["tier_classified"]    = tier
    result["tier_final"]         = result.get("tier", tier)
    result["classifier_signals"] = signals
    result["elapsed_s"]          = elapsed

    log.info("adaptive.done",
             tier_classified=tier,
             tier_final=result["tier_final"],
             upgraded=result.get("tier_upgraded_from"),
             turns=result["total_turns"],
             tokens=result["total_tokens"],
             cache_read=result["total_cache_read"],
             files_changed=result["files_changed"],
             success=result["success"],
             elapsed_s=elapsed)

    return result
