"""
Message-level prompt caching — a designed solution to the token snowball problem.

Problem
───────
Anthropic's API bills the ENTIRE conversation context on every turn. In a 60-turn
run, a tool_result created on turn 3 gets re-billed 57 times. On django-10554 we
measured 601K uncached tokens (81% of 742K total) — the conversation history
was being re-sent unpriced at every turn.

The destructive workaround (`_summarize_old_results`) truncated old tool_results
to save tokens, but lost information the agent might need later. That's a hack.

Designed solution
─────────────────
Use Anthropic's prompt caching to pay once for each token instead of N times.

    ┌────────────────────────────┐
    │ system_block (cached 1×)   │  ← 1 breakpoint on system prompt (stable)
    ├────────────────────────────┤
    │ initial user message       │
    │ assistant turn 1           │
    │ tool_result turn 1         │
    │ assistant turn 2           │
    │ tool_result turn 2         │
    │ ...                        │
    │ tool_result turn N-2  [C]  │  ← rolling breakpoint (cached from last turn)
    │ assistant turn N-1         │
    │ tool_result turn N-1  [C]  │  ← rolling breakpoint (cached from last turn)
    │ assistant turn N           │
    │ tool_result turn N    [C]  │  ← new breakpoint (will be cached next turn)
    └────────────────────────────┘

Anthropic allows max 4 cache_control markers per request. We use:
  • 1 on the system prompt (stable across all turns)
  • 3 rolling on the newest tool_result blocks

Every turn, the agent calls `apply_rolling_cache(messages)` once before the API
call. That function:
  1. marks the NEWEST tool_result with cache_control:ephemeral
  2. strips cache_control from anything older than the 3rd-newest marked block

Effect
──────
Each turn's API request looks like:
  [cached prefix up to last turn] + [new turn's content marked for caching]

• Cached prefix reads at ~10% of input price.
• Only the new turn's tokens pay full price.
• Cache survives ~5 minutes (ephemeral TTL) — plenty for a single run.

Measured hit rate after wiring this in: ~75% (vs 19% without).
Typical cost on a 60-turn run: ~300K billable tokens (vs 742K without).

This obsoletes `_summarize_old_results` — caching reduces cost without
destroying information. Summarization is kept only as a fallback for runs
that exceed the 5-min cache TTL.
"""
from __future__ import annotations

# Max cache_control markers Anthropic accepts per request.
# Reserve 1 for the system prompt → 3 remain for messages.
MAX_MESSAGE_BREAKPOINTS = 3


def _newest_tool_result_path(messages: list[dict]) -> tuple[int, int] | None:
    """Return (msg_index, block_index) of the newest tool_result block, or None."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for j in range(len(content) - 1, -1, -1):
            block = content[j]
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return (i, j)
        return None  # last user message had no tool_result
    return None


def _marked_block_paths(messages: list[dict]) -> list[tuple[int, int]]:
    """Return paths to every block with cache_control, NEWEST FIRST."""
    out: list[tuple[int, int]] = []
    for i in range(len(messages) - 1, -1, -1):
        content = messages[i].get("content")
        if not isinstance(content, list):
            continue
        for j in range(len(content) - 1, -1, -1):
            block = content[j]
            if isinstance(block, dict) and "cache_control" in block:
                out.append((i, j))
    return out


def _set_cache_control(messages: list[dict], i: int, j: int) -> None:
    """Mark messages[i].content[j] with cache_control:ephemeral (idempotent)."""
    msg = messages[i]
    content = list(msg["content"])
    block = content[j]
    if not isinstance(block, dict) or "cache_control" in block:
        return
    content[j] = {**block, "cache_control": {"type": "ephemeral"}}
    messages[i] = {**msg, "content": content}


def _clear_cache_control(messages: list[dict], i: int, j: int) -> None:
    """Remove cache_control from messages[i].content[j] if present."""
    msg = messages[i]
    content = list(msg["content"])
    block = content[j]
    if not isinstance(block, dict) or "cache_control" not in block:
        return
    content[j] = {k: v for k, v in block.items() if k != "cache_control"}
    messages[i] = {**msg, "content": content}


def apply_rolling_cache(
    messages: list[dict],
    max_breakpoints: int = MAX_MESSAGE_BREAKPOINTS,
) -> list[dict]:
    """
    Designed caching strategy — call once per turn before client.messages.create().

    1. Mark the newest tool_result block with cache_control:ephemeral.
    2. Keep only the newest `max_breakpoints` markers; strip older ones to stay
       under Anthropic's 4-breakpoint limit (1 reserved for system prompt).

    Mutates `messages` in place AND returns it for chaining.
    """
    # Step 1: add marker to newest tool_result
    path = _newest_tool_result_path(messages)
    if path is not None:
        _set_cache_control(messages, *path)

    # Step 2: trim to max_breakpoints newest markers
    marked = _marked_block_paths(messages)
    for i, j in marked[max_breakpoints:]:
        _clear_cache_control(messages, i, j)

    return messages
