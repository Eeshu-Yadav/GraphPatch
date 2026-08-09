from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class FileChange:
    file_path: str
    change_type: str        # "modify" | "create" | "delete"
    description: str        # what to change (human-readable)
    reason: str             # why this change is needed
    symbols_affected: list[str] = field(default_factory=list)


@dataclass
class Plan:
    ticket_id: str
    repo_id: str
    summary: str                        # 1-2 sentence plan overview
    file_changes: list[FileChange]      # ordered list of changes
    test_strategy: str                  # how to test the change
    reasoning: str                      # full LLM reasoning
    model_used: str

    def to_prompt_text(self) -> str:
        """Render plan as markdown for passing to Layer 5 (Implementation Agent)."""
        lines = [
            f"# Implementation Plan for Ticket: {self.ticket_id}",
            f"**Repository:** `{self.repo_id}`",
            f"**Summary:** {self.summary}",
            "",
            "## Changes Required",
        ]
        for i, fc in enumerate(self.file_changes, 1):
            lines.append(f"### {i}. `{fc.file_path}` ({fc.change_type})")
            lines.append(f"**What:** {fc.description}")
            lines.append(f"**Why:** {fc.reason}")
            if fc.symbols_affected:
                lines.append(f"**Symbols:** {', '.join(f'`{s}`' for s in fc.symbols_affected)}")
            lines.append("")
        lines.append(f"## Test Strategy\n{self.test_strategy}")
        lines.append(f"\n## Reasoning\n{self.reasoning}")
        return "\n".join(lines)
