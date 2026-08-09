"""PipelineContext — the shared state baton passed between all nodes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PipelineContext:
    """Shared state flowing through the entire blueprint.

    Each node reads the fields it needs and writes its outputs.
    This replaces the growing LLM conversation with structured data.
    """

    # ── Inputs (set once at creation) ──────────────────────
    ticket_id: str = ""
    repo_id: str = ""
    title: str = ""
    body: str = ""
    task_type: str = "bug_fix"              # bug_fix | feature | migration | test_fix
    github_token: str = ""
    fallback_to_legacy: bool = True         # Fall back to existing pipeline on failure

    # ── From L3 context assembly ───────────────────────────
    ticket: Any = None                      # layer3_context.models.ticket.Ticket
    bundle: Any = None                      # layer3_context.models.context.ContextBundle

    # ── From setup node ────────────────────────────────────
    repo_path: Path | None = None
    branch_name: str = ""
    directory_rules: str = ""               # Loaded .rules.md content
    sandbox: Any = None                     # layer45_agent.sandbox.Sandbox (if Docker available)
    profile: dict[str, Any] = field(default_factory=dict)  # Auto-detected project profile (test runner, build system, etc.)

    # ── From intent classification (cached, reused across nodes) ──
    intent: dict[str, Any] = field(default_factory=dict)   # {type, complexity, target_symbols, target_files, confidence}

    # ── Cross-node cache (reduces re-reads between explore→write) ──
    file_cache: dict[str, str] = field(default_factory=dict)  # path → content (shared across nodes)
    files_read_summary: dict[str, str] = field(default_factory=dict)  # path → "120 lines" (lightweight)

    # ── From reproduce node ────────────────────────────────
    reproduce_output: str = ""              # Test output from bug reproduction

    # ── From agentic nodes ─────────────────────────────────
    exploration_summary: str = ""           # What the explorer found
    plan: str = ""                          # Structured plan text
    modified_files: dict[str, str] = field(default_factory=dict)
    original_files: dict[str, str] = field(default_factory=dict)
    implementation: Any = None              # layer5 Implementation object

    # ── From lint node ─────────────────────────────────────
    lint_output: str = ""
    lint_errors: list[str] = field(default_factory=list)
    lint_autofixed: list[str] = field(default_factory=list)

    # ── From test node ─────────────────────────────────────
    test_output: str = ""                   # Last 4000 chars
    test_passed: bool = False
    test_counts: dict[str, int] = field(default_factory=dict)

    # ── From build node ────────────────────────────────────
    build_output: str = ""
    build_passed: bool = False

    # ── From review node ───────────────────────────────────
    review_approved: bool = False
    review_feedback: str = ""
    review_files_to_drop: list[str] = field(default_factory=list)

    # ── From autofix node ──────────────────────────────────
    autofix_applied: list[str] = field(default_factory=list)

    # ── Flow control ───────────────────────────────────────
    ci_round: int = 0
    max_ci_rounds: int = 2                  # Hard cap enforced by test_gate
    max_total_tokens: int = 150_000         # Hard token budget — escalate if exceeded
    lint_fix_attempted: bool = False        # Prevent infinite lint loops
    review_fix_attempted: bool = False      # Prevent infinite review loops
    escalated: bool = False
    draft_pr: bool = False

    # ── Output ─────────────────────────────────────────────
    pr_url: str = ""
    pr_number: int = 0
    error: str = ""
    success: bool = False

    # ── Metrics ────────────────────────────────────────────
    tokens_by_node: dict[str, int] = field(default_factory=dict)
    time_by_node: dict[str, float] = field(default_factory=dict)
    total_tokens: int = 0
    total_duration: float = 0.0
    nodes_executed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Human-readable summary of the pipeline result."""
        lines = [f"## Minion Result — {self.ticket_id}"]
        lines.append(f"**Task type:** {self.task_type}")
        lines.append(f"**Success:** {self.success}")

        if self.pr_url:
            lines.append(f"**PR:** {self.pr_url}")
        if self.error:
            lines.append(f"**Error:** {self.error}")
        if self.escalated:
            lines.append("**Escalated to human**")

        if self.modified_files:
            lines.append(f"**Files changed:** {', '.join(self.modified_files.keys())}")

        lines.append(f"\n**Nodes executed:** {' → '.join(self.nodes_executed)}")
        lines.append(f"**Total tokens:** {self.total_tokens:,}")
        lines.append(f"**CI rounds:** {self.ci_round}/{self.max_ci_rounds}")
        lines.append(f"**Duration:** {self.total_duration:.1f}s")

        if self.tokens_by_node:
            lines.append("\n**Tokens per node:**")
            for node, tokens in self.tokens_by_node.items():
                lines.append(f"  {node}: {tokens:,}")

        return "\n".join(lines)
