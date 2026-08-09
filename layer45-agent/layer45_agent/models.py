"""Data models for the agent loop."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from layer45_agent.implementation import Implementation


class EditHistory:
    """
    C6: Checkpoint/rollback mechanism for hypothesis-based fixing.

    Before trying a fix hypothesis, call checkpoint() to save state.
    If the fix fails tests, call rollback() to restore clean state.
    If the fix passes, call commit() to accept it.

    Usage:
        history.checkpoint(modified_files, original_files)
        # ... agent writes fix H1 ...
        # ... tests fail ...
        history.rollback(modified_files, original_files, repo_path)
        # modified_files is now back to pre-H1 state
        # ... agent writes fix H2 ...
    """

    def __init__(self):
        self._snapshots: list[tuple[dict, dict]] = []  # stack of (modified, original) states
        self.attempt_count: int = 0
        self.attempt_diffs: list[str] = []  # diff summary of each failed attempt

    def checkpoint(self, modified_files: dict[str, str], original_files: dict[str, str]) -> None:
        """Save current file state before trying a hypothesis."""
        self._snapshots.append((
            copy.deepcopy(modified_files),
            copy.deepcopy(original_files),
        ))
        self.attempt_count += 1

    def rollback(
        self,
        modified_files: dict[str, str],
        original_files: dict[str, str],
        repo_path: Path | None = None,
    ) -> bool:
        """Revert to last checkpoint. Returns True if rollback happened."""
        if not self._snapshots:
            return False

        saved_modified, saved_original = self._snapshots.pop()

        # Capture what was tried (for failure analysis)
        diff_lines = []
        for fp in modified_files:
            if fp not in saved_modified:
                diff_lines.append(f"+ new file: {fp}")
            elif modified_files[fp] != saved_modified.get(fp, ""):
                diff_lines.append(f"~ changed: {fp}")
        self.attempt_diffs.append("\n".join(diff_lines) if diff_lines else "no changes")

        # Restore in-memory state
        modified_files.clear()
        modified_files.update(saved_modified)
        original_files.clear()
        original_files.update(saved_original)

        # Restore files on disk (for sandbox volume mount)
        if repo_path:
            for fp, content in saved_modified.items():
                abs_path = Path(repo_path) / fp
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(content, encoding="utf-8")
            # Delete files that were created in the failed attempt
            for fp in list(modified_files.keys()):
                if fp not in saved_modified:
                    abs_path = Path(repo_path) / fp
                    if abs_path.exists():
                        abs_path.unlink()

        return True

    def commit(self) -> None:
        """Accept current state, discard checkpoint."""
        if self._snapshots:
            self._snapshots.pop()

    def get_failure_summary(self) -> str:
        """Summary of failed attempts for the agent's context."""
        if not self.attempt_diffs:
            return ""
        lines = [f"Previous fix attempts that FAILED ({len(self.attempt_diffs)} total):"]
        for i, diff in enumerate(self.attempt_diffs, 1):
            lines.append(f"  Attempt {i}: {diff}")
        lines.append("Try a DIFFERENT approach — don't repeat what failed.")
        return "\n".join(lines)

    @property
    def has_checkpoint(self) -> bool:
        return len(self._snapshots) > 0


@dataclass
class AgentConfig:
    model: str = "claude-sonnet-4-20250514"  # default
    explore_model: str = "claude-sonnet-4-20250514"  # Phase 1: exploration + reasoning (was Haiku, now Sonnet for vague tickets)
    plan_model: str = "claude-opus-4-6"  # Phase 2: complex planning decisions
    write_model: str = "claude-sonnet-4-20250514"  # Phase 3+4: code gen + verify
    api_key: str = ""
    max_iterations: int = 200            # Effectively unlimited — let the agent iterate until it converges
    max_explore_iterations: int = 50     # No hard cap — vague tickets need deep exploration
    max_write_no_test: int = 15          # Relaxed — agent may need many write attempts
    max_total_tokens: int = 5_000_000    # 5M tokens — no artificial ceiling
    compression_threshold: int = 160_000  # Keep compression — it helps the agent, doesn't limit it
    max_output_tokens_per_turn: int = 16384  # Doubled — let agent reason more per turn
    temperature: float = 0.1
    test_cmd_hint: str = ""               # Specific test command for reproduction (e.g., "pytest tests/test_foo.py -xvs")
    max_repair_iterations: int = 10       # 10 repair cycles — enough for complex multi-file fixes
    use_sandbox: bool = True              # Use Docker sandbox for isolated execution (auto-detects Docker)


@dataclass
class ToolCallRecord:
    iteration: int
    tool_name: str
    args: dict
    result: dict
    timestamp: float


@dataclass
class ExplorationCache:
    """Reusable exploration results to avoid re-exploring on retry."""
    files_read: dict[str, str] = field(default_factory=dict)       # path → content summary
    symbols_found: list[str] = field(default_factory=list)          # symbol names
    files_modified: dict[str, str] = field(default_factory=dict)    # path → content (from prev attempt)
    tool_summaries: list[str] = field(default_factory=list)         # "read_file(src/foo.ts) → 120 lines"
    diff_summary: str = ""                                          # what was changed last time

    def to_prompt(self) -> str:
        """Convert cache to a prompt section for the agent."""
        if not self.files_read and not self.symbols_found:
            return ""
        lines = ["## Previous Exploration (reuse this — do NOT re-explore these):\n"]
        if self.files_read:
            lines.append(f"**Files already read ({len(self.files_read)}):**")
            for path, summary in list(self.files_read.items())[:20]:
                lines.append(f"  - `{path}`: {summary}")
        if self.symbols_found:
            lines.append(f"\n**Symbols found:** {', '.join(self.symbols_found[:30])}")
        if self.diff_summary:
            lines.append(f"\n**Previous attempt's changes:**\n```\n{self.diff_summary[:2000]}\n```")
        if self.tool_summaries:
            lines.append(f"\n**Tool history ({len(self.tool_summaries)} calls):**")
            for s in self.tool_summaries[-15:]:
                lines.append(f"  - {s}")
        return "\n".join(lines)


@dataclass
class AgentResult:
    ticket_id: str
    repo_id: str
    implementation: Implementation
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    iterations: int = 0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    success: bool = True
    error: str | None = None
    exploration_cache: ExplorationCache = field(default_factory=ExplorationCache)
