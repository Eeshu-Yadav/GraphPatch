"""
Budget tracker + finish nudge.

Budget tracker: injects remaining turn count + pressure hint into every tool
result. Agent self-regulates without hard cutoffs (from BATS paper).

Finish nudge: injected N turns after first write_file. Escalates every 5 turns.
Solves the run_command loop problem seen in v2 and v3 on django-10554.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


# ── Budget tracker ─────────────────────────────────────────────────────────────

_BUDGET_HINTS = {
    "HIGH":     "Explore freely. Check multiple files if needed.",
    "MEDIUM":   "Focus on confirmed targets only. Start writing soon.",
    "LOW":      "Write the fix NOW. No more exploration.",
    "CRITICAL": "Call finish() immediately. Do not run more commands.",
}


def inject_budget(result: dict, turn: int, max_turns: int) -> dict:
    """
    Append _budget key to every tool result.
    pct = remaining / max_turns → HIGH / MEDIUM / LOW / CRITICAL
    """
    remaining = max_turns - turn
    pct = remaining / max_turns if max_turns > 0 else 0.0

    if pct >= 0.7:    level = "HIGH"
    elif pct >= 0.3:  level = "MEDIUM"
    elif pct >= 0.1:  level = "LOW"
    else:             level = "CRITICAL"

    result["_budget"] = f"[{level}: {remaining}/{max_turns} turns left] {_BUDGET_HINTS[level]}"
    return result


# ── Finish nudge ───────────────────────────────────────────────────────────────

_NUDGE_MESSAGES = {
    1: "You've written the fix and tested it. Call finish() now with a summary of what changed.",
    2: "REMINDER: The changes look complete. Stop running more tests and call finish().",
    3: "URGENT: You are in a loop. The fix is written and tested. Call finish() RIGHT NOW.",
    4: "FINAL WARNING: Do not run any more commands. Call finish() immediately.",
}


def inject_nudge(
    result: dict,
    write_turns: int,
    first_write_turn: int | None,
    nudge_start: int = 10,
) -> dict:
    """
    Append _finish_nudge key after (nudge_start) turns past first write_file.
    Escalates every 5 turns.

    nudge_start: turns after first write before nudging begins.
                 Config A=10, Config B=15, Config C=20.
    """
    if first_write_turn is None:
        return result

    turns_since_write = write_turns - first_write_turn
    if turns_since_write < nudge_start:
        return result

    # Level 1 at nudge_start, +1 every 5 turns
    level = 1 + (turns_since_write - nudge_start) // 5
    level = min(level, 4)

    msg = _NUDGE_MESSAGES[level]
    result["_finish_nudge"] = msg

    log.info("nudge.injected", level=level, write_turns=write_turns,
             turns_since_write=turns_since_write)
    return result
