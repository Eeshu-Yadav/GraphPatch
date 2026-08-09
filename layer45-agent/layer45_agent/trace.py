"""
Debug trace logger — writes a full JSON trace of every agent run.

Captures the complete loop: system prompt, every Claude request/response,
every tool call with args and results, token usage per turn, model switches.

Usage:
    AGENT_TRACE=1 python demo.py pipeline --repo ...

Trace files are written to: /home/eeshu/Desktop/context/traces/
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

TRACE_DIR = Path(os.environ.get("AGENT_TRACE_DIR", "/home/eeshu/Desktop/context/traces"))
_enabled = os.environ.get("AGENT_TRACE", "0") == "1"
_current_trace: dict | None = None


def is_enabled() -> bool:
    return os.environ.get("AGENT_TRACE", "0") == "1"


def start_trace(ticket_id: str, repo_id: str, model: str) -> None:
    """Initialize a new trace for a pipeline run."""
    global _current_trace
    if not is_enabled():
        return
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    _current_trace = {
        "ticket_id": ticket_id,
        "repo_id": repo_id,
        "default_model": model,
        "started_at": datetime.now().isoformat(),
        "system_prompt": None,
        "initial_message": None,
        "turns": [],
        "summary": {},
    }


def log_system_prompt(prompt: str) -> None:
    """Capture the full system prompt sent to Claude."""
    if not _current_trace:
        return
    _current_trace["system_prompt"] = prompt
    _current_trace["system_prompt_chars"] = len(prompt)


def log_initial_message(messages: list[dict]) -> None:
    """Capture the first user message."""
    if not _current_trace:
        return
    _current_trace["initial_message"] = _safe_serialize(messages[0]) if messages else None


def log_turn(
    iteration: int,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    assistant_text: str,
    tool_calls: list[dict],
    tool_results: list[dict],
    stop_reason: str,
    duration_ms: int,
) -> None:
    """Capture one full turn: Claude response + tool executions."""
    if not _current_trace:
        return
    turn = {
        "iteration": iteration,
        "model": model,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "cache_read": cache_read_tokens,
            "cache_creation": cache_creation_tokens,
            "cost_usd": _estimate_cost(model, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens),
        },
        "duration_ms": duration_ms,
        "stop_reason": stop_reason,
        "assistant_text": assistant_text[:2000] if assistant_text else "",
        "tool_calls": tool_calls,
        "tool_results": tool_results,
    }
    _current_trace["turns"].append(turn)


def log_event(event: str, data: dict | None = None) -> None:
    """Log a misc event (compression, oscillation, hard limit, etc.)."""
    if not _current_trace:
        return
    _current_trace["turns"].append({
        "event": event,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    })


def finish_trace(
    iterations: int,
    total_prompt_tokens: int,
    total_completion_tokens: int,
    files_changed: list[str],
    success: bool,
    error: str | None = None,
) -> str | None:
    """Finalize and write the trace to disk. Returns the trace file path."""
    global _current_trace
    if not _current_trace:
        return None

    _current_trace["finished_at"] = datetime.now().isoformat()
    _current_trace["summary"] = {
        "iterations": iterations,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_turns": len([t for t in _current_trace["turns"] if "iteration" in t]),
        "files_changed": files_changed,
        "success": success,
        "error": error,
        "total_cost_usd": sum(
            t.get("tokens", {}).get("cost_usd", 0)
            for t in _current_trace["turns"]
            if "tokens" in t
        ),
    }

    # Write to disk
    ticket_id = _current_trace["ticket_id"].replace("/", "-")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{ticket_id}.json"
    path = TRACE_DIR / filename

    with open(path, "w") as f:
        json.dump(_current_trace, f, indent=2, default=str)

    _current_trace = None
    return str(path)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> float:
    """Estimate USD cost for a single turn."""
    # Pricing per 1M tokens (as of 2025)
    rates = {
        "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
        "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_write": 18.75},
        "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
        "claude-haiku-4-5-20251001": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_write": 1.0},
    }
    r = rates.get(model, rates["claude-sonnet-4-20250514"])

    uncached_input = max(0, input_tokens - cache_read)
    cost = (
        (uncached_input / 1_000_000) * r["input"]
        + (cache_read / 1_000_000) * r["cache_read"]
        + (cache_creation / 1_000_000) * r["cache_write"]
        + (output_tokens / 1_000_000) * r["output"]
    )
    return round(cost, 6)


def _safe_serialize(obj) -> str | dict | list:
    """Convert objects to JSON-safe format, truncating large content."""
    if isinstance(obj, str):
        return obj[:5000] if len(obj) > 5000 else obj
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k == "content" and isinstance(v, str) and len(v) > 5000:
                result[k] = v[:5000] + f"... [truncated, {len(v)} chars total]"
            else:
                result[k] = _safe_serialize(v)
        return result
    if isinstance(obj, list):
        return [_safe_serialize(item) for item in obj[:50]]
    return str(obj)
