"""
Single source of truth for per-tier agent configuration.

Why this exists
───────────────
The classifier's tier prediction (easy / medium / hard) drives THREE values:

  • max_turns         — total turn budget
  • nudge_after_write — when finish-nudge starts escalating
  • tools             — which tools the agent has access to

Originally these were set independently:
  - max_turns + nudge_after_write captured at agent start (immutable)
  - tools mutated reactively when Layer 4 upgraded the tier

That asymmetry left a bug: a mid-run upgrade extended the tool list but
left max_turns / nudge_after_write stale. Issue 4 of Batch 3 (django-14007)
got upgraded medium→hard at done_exploring but kept its 55-turn medium cap,
so it ran out of budget before reaching the hard-tier finish-nudge.

The designed solution
─────────────────────
A `TierConfig` dataclass owns all three values. The agent holds ONE current
config; on tier change it adopts the new tier's config atomically. No more
asymmetric mutation; impossible to forget a value.

Usage in an agent loop:

    cfg = TierConfig.for_tier("medium")
    while turn < cfg.max_turns:
        ...
        if upgrade_fired:
            cfg = TierConfig.for_tier("hard")  # all 3 update together

Tier boundaries (max_turns / nudge_after_write) are tuned from test data
and live HERE, not in CONFIGS dict scattered across modules.
"""
from __future__ import annotations

from dataclasses import dataclass


# Tier numbers — tuned from observed agent behaviour on SWE-bench.
# • easy: small bug, single file, agent should be brief
# • medium: small refactor, 1-2 files, finish-nudge fires earlier
# • hard: multi-file refactor / new feature, more budget + later nudge
_TIER_NUMBERS = {
    "easy":   {"max_turns": 40, "nudge_after_write": 10},
    "medium": {"max_turns": 55, "nudge_after_write": 15},
    "hard":   {"max_turns": 80, "nudge_after_write": 20},
}


@dataclass(frozen=True)
class TierConfig:
    """All tier-derived parameters as a single immutable bundle."""
    tier: str
    max_turns: int
    nudge_after_write: int
    tools: list[dict]                # full unified tool list for this tier

    @classmethod
    def for_tier(cls, tier: str) -> "TierConfig":
        """Construct the canonical config for a tier."""
        nums = _TIER_NUMBERS[tier]
        # Local import — agent_v5 imports tier_config at module load, so
        # delaying the v5 import here breaks the circular dependency.
        from lean_agent.agent_v5 import unified_tools_for_tier
        return cls(
            tier=tier,
            max_turns=nums["max_turns"],
            nudge_after_write=nums["nudge_after_write"],
            tools=unified_tools_for_tier(tier),
        )

    def upgrade_to(self, new_tier: str) -> "TierConfig | None":
        """Return new config if `new_tier` is strictly larger than current; else None."""
        order = {"easy": 0, "medium": 1, "hard": 2}
        if order[new_tier] > order[self.tier]:
            return TierConfig.for_tier(new_tier)
        return None
