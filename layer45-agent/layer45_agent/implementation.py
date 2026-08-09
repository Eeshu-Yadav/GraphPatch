"""Implementation and FileResult models — shared data contract for pipeline output.

These models represent the output of the agent loop (layer45-agent) and are consumed
by layer6-validator (validation) and layer7-pr-publisher (PR creation).

Previously lived in layer5-implementer, which was removed when the one-shot
pipeline was replaced by the ReAct agent loop.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field


@dataclass
class FileResult:
    file_path: str
    change_type: str  # "modify", "create", "delete"
    original_content: str = ""
    modified_content: str = ""
    explanation: str = ""


@dataclass
class Implementation:
    ticket_id: str
    repo_id: str
    plan_summary: str
    file_results: list[FileResult] = field(default_factory=list)
    model_used: str = ""

    def to_diff_text(self, max_lines: int = 500) -> str:
        """Generate unified diff text for all changed files."""
        parts = []
        for fr in self.file_results:
            if fr.change_type == "delete":
                parts.append(f"--- a/{fr.file_path}\n+++ /dev/null")
                continue
            orig_lines = fr.original_content.splitlines(keepends=True)
            mod_lines = fr.modified_content.splitlines(keepends=True)
            diff = difflib.unified_diff(
                orig_lines, mod_lines,
                fromfile=f"a/{fr.file_path}",
                tofile=f"b/{fr.file_path}",
            )
            parts.extend(diff)
            if len(parts) > max_lines:
                parts.append("... (diff truncated)\n")
                break
        return "".join(parts)
