"""Model router — dynamic model selection based on complexity, env vars, and cost budgets.

Replaces hardcoded model assignment per node with signal-based routing.
Supports env-var overrides for each node for easy experimentation.
"""
from __future__ import annotations

import os
import structlog

log = structlog.get_logger(__name__)

# ── Default models per node ──────────────────────────────────────────────────
# Sonnet for review (downgraded from Opus — Opus is 5x more expensive and
# catches few issues on minion-constrained output). Opus reserved for complex tasks.
_MODEL_DEFAULTS = {
    "explore":     "claude-haiku-4-5-20251001",
    "write_code":  "claude-sonnet-4-20250514",
    "fix_lint":    "claude-haiku-4-5-20251001",
    "fix_tests":   "claude-sonnet-4-20250514",
    "fix_review":  "claude-sonnet-4-20250514",
    "code_review": "claude-sonnet-4-20250514",
}

# ── Env-var overrides (set any of these to force a specific model) ───────────
_ENV_KEYS = {
    "explore":     "MINION_EXPLORE_MODEL",
    "write_code":  "MINION_WRITE_MODEL",
    "fix_lint":    "MINION_FIX_LINT_MODEL",
    "fix_tests":   "MINION_FIX_TESTS_MODEL",
    "fix_review":  "MINION_FIX_REVIEW_MODEL",
    "code_review": "MINION_REVIEW_MODEL",
}

# ── Complexity-based upgrades ────────────────────────────────────────────────
# For "complex" tickets, upgrade specific nodes to more capable models
_COMPLEXITY_UPGRADES = {
    "complex": {
        "explore": "claude-sonnet-4-20250514",       # Haiku → Sonnet for complex exploration
        "code_review": "claude-opus-4-20250514",     # Sonnet → Opus for complex review
    },
}

# ── Cost budgets per complexity tier ─────────────────────────────────────────
COST_BUDGETS = {
    "trivial":  {"max_tokens": 50_000,  "max_tool_calls_explore": 6,  "max_tool_calls_write": 10},
    "simple":   {"max_tokens": 80_000,  "max_tool_calls_explore": 8,  "max_tool_calls_write": 12},
    "moderate": {"max_tokens": 150_000, "max_tool_calls_explore": 10, "max_tool_calls_write": 16},
    "complex":  {"max_tokens": 250_000, "max_tool_calls_explore": 14, "max_tool_calls_write": 20},
}


def get_model(node_name: str, complexity: str = "moderate") -> str:
    """Select model for a node based on env-var override, complexity, or default.

    Priority:
    1. Environment variable (MINION_*_MODEL) — always wins
    2. Complexity-based upgrade — for "complex" tickets only
    3. Default — cost-efficient baseline
    """
    # Priority 1: Env-var override
    env_key = _ENV_KEYS.get(node_name, "")
    if env_key:
        env_val = os.environ.get(env_key)
        if env_val:
            log.debug("model_router.env_override", node=node_name, model=env_val)
            return env_val

    # Priority 2: Complexity upgrade
    upgrades = _COMPLEXITY_UPGRADES.get(complexity, {})
    if node_name in upgrades:
        model = upgrades[node_name]
        log.debug("model_router.complexity_upgrade", node=node_name,
                   complexity=complexity, model=model)
        return model

    # Priority 3: Default
    return _MODEL_DEFAULTS.get(node_name, "claude-sonnet-4-20250514")


def get_cost_budget(complexity: str = "moderate") -> dict:
    """Get token/tool budget for a complexity tier."""
    return COST_BUDGETS.get(complexity, COST_BUDGETS["moderate"])
