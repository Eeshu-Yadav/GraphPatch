"""Deterministic autofix catalog — pattern → fix mappings.

Applied BEFORE sending errors to the LLM, saving tokens on predictable fixes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import structlog

log = structlog.get_logger(__name__)


@dataclass
class AutofixRule:
    name: str
    pattern: str                    # Regex matching error output
    fix_fn: Callable[[Path, str, str], bool]  # (repo_path, file_path, error) → applied?
    file_extensions: list[str]      # [".py", ".ts", ...]
    confidence: float = 0.95


def _fix_trailing_whitespace(repo_path: Path, file_path: str, error: str) -> bool:
    fp = repo_path / file_path
    if not fp.exists():
        return False
    content = fp.read_text(encoding="utf-8", errors="replace")
    fixed = "\n".join(line.rstrip() for line in content.split("\n"))
    if fixed != content:
        fp.write_text(fixed, encoding="utf-8")
        return True
    return False


def _fix_missing_newline(repo_path: Path, file_path: str, error: str) -> bool:
    fp = repo_path / file_path
    if not fp.exists():
        return False
    content = fp.read_text(encoding="utf-8", errors="replace")
    if content and not content.endswith("\n"):
        fp.write_text(content + "\n", encoding="utf-8")
        return True
    return False


def _fix_unused_import(repo_path: Path, file_path: str, error: str) -> bool:
    """Remove a specific unused import line."""
    # Extract the import name from error like: "F401 `json` imported but unused"
    match = re.search(r"`(\w+)`.*imported but unused", error)
    if not match:
        return False

    import_name = match.group(1)
    fp = repo_path / file_path
    if not fp.exists():
        return False

    lines = fp.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    new_lines = []
    removed = False

    for line in lines:
        stripped = line.strip()
        # Match: import json / from x import json / from x import (... json ...)
        if (stripped == f"import {import_name}"
                or re.match(rf"from\s+\S+\s+import\s+{import_name}\s*$", stripped)):
            removed = True
            continue
        new_lines.append(line)

    if removed:
        fp.write_text("".join(new_lines), encoding="utf-8")
    return removed


def _fix_blank_lines(repo_path: Path, file_path: str, error: str) -> bool:
    """Fix E302 (expected 2 blank lines) and E303 (too many blank lines)."""
    fp = repo_path / file_path
    if not fp.exists():
        return False

    content = fp.read_text(encoding="utf-8", errors="replace")
    # Collapse 3+ blank lines to 2
    fixed = re.sub(r"\n{4,}", "\n\n\n", content)
    if fixed != content:
        fp.write_text(fixed, encoding="utf-8")
        return True
    return False


# ── Rule Registry ─────────────────────────────────────────────────────────────

RULES: list[AutofixRule] = [
    AutofixRule(
        name="trailing_whitespace",
        pattern=r"W291|trailing whitespace",
        fix_fn=_fix_trailing_whitespace,
        file_extensions=["*"],
    ),
    AutofixRule(
        name="missing_newline",
        pattern=r"W292|No newline at end of file|final newline",
        fix_fn=_fix_missing_newline,
        file_extensions=["*"],
    ),
    AutofixRule(
        name="unused_import",
        pattern=r"F401.*imported but unused",
        fix_fn=_fix_unused_import,
        file_extensions=[".py"],
    ),
    AutofixRule(
        name="blank_lines",
        pattern=r"E302|E303|blank line",
        fix_fn=_fix_blank_lines,
        file_extensions=[".py"],
    ),
]


def try_autofixes(
    error_output: str,
    changed_files: list[str],
    repo_path: Path,
) -> list[str]:
    """Try all autofix rules against errors. Returns list of applied fix names."""
    applied = []

    for rule in RULES:
        if not re.search(rule.pattern, error_output, re.IGNORECASE):
            continue

        for fp in changed_files:
            ext = Path(fp).suffix
            if "*" not in rule.file_extensions and ext not in rule.file_extensions:
                continue

            try:
                if rule.fix_fn(repo_path, fp, error_output):
                    applied.append(f"{rule.name} on {fp}")
                    log.debug("autofix.applied", rule=rule.name, file=fp)
            except Exception as e:
                log.debug("autofix.error", rule=rule.name, file=fp, error=str(e))

    return applied
