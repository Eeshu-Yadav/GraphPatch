"""Directory-scoped rules loader.

Walks from repo root collecting .rules.md, .cursorrules, CLAUDE.md files.
Same format works in Minions, Cursor, and Claude Code.
"""
from __future__ import annotations

from pathlib import Path

RULE_FILE_NAMES = [
    ".rules.md",
    ".cursorrules",
    "CLAUDE.md",
    ".agent-rules.md",
]


def load_rules(repo_path: Path, changed_files: list[str] | None = None) -> str:
    """Collect all applicable rule files from repo root down to changed file directories.

    Args:
        repo_path: Root of the repository
        changed_files: Optional list of relative file paths being changed.
                       If provided, also loads rules from their parent directories.

    Returns:
        Combined rules text, separated by ---
    """
    rules: list[str] = []
    seen: set[str] = set()

    # Always load repo root rules
    _load_rules_at(repo_path, rules, seen)

    # Load rules for each changed file's directory chain
    if changed_files:
        for fp in changed_files:
            current = repo_path
            for part in Path(fp).parent.parts:
                current = current / part
                _load_rules_at(current, rules, seen)

    return "\n\n---\n\n".join(rules) if rules else ""


def _load_rules_at(directory: Path, rules: list[str], seen: set[str]):
    """Load rule files from a single directory."""
    for name in RULE_FILE_NAMES:
        path = directory / name
        key = str(path)
        if path.exists() and key not in seen:
            try:
                content = path.read_text(encoding="utf-8", errors="replace").strip()
                if content:
                    rules.append(f"[Rules from {path.name}]\n{content}")
                    seen.add(key)
            except Exception:
                pass
