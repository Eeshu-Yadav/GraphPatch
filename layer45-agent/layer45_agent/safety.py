"""Safety mechanisms: oscillation detection, path guards, limits."""
from __future__ import annotations

from collections import Counter

from layer45_agent.models import ToolCallRecord


def detect_oscillation(tool_log: list[ToolCallRecord], window: int = 6) -> bool:
    """
    Detect if the agent is stuck in a loop. Relaxed thresholds — nudges, not hard stops.
    1. Identical (tool_name, args) repeated `window` times (6, was 4)
    2. A-B-A-B ping-pong pattern (last 6 tool names, was 4)
    3. Same file read 6+ times in last 10 calls (was 4+ in last 6)
    4. write_file failing on same file 5+ times in last 10 calls (was 3+ in last 6)
    """
    if len(tool_log) < window:
        return False

    # Strategy 1: Exact identical calls repeated 6 times (was 4)
    recent = tool_log[-window:]
    first = (recent[0].tool_name, str(recent[0].args))
    if all((r.tool_name, str(r.args)) == first for r in recent):
        return True

    # Strategy 2: A-B-A-B ping-pong — require 3 full cycles (6 calls), not 2
    if len(tool_log) >= 6:
        names = [tc.tool_name for tc in tool_log[-6:]]
        if (names[0] == names[2] == names[4] and
            names[1] == names[3] == names[5] and
            names[0] != names[1]):
            return True

    # Strategy 3: Same file read 6+ times in last 10 calls (more lenient)
    recent_reads = [
        tc.args.get("file_path") for tc in tool_log[-10:]
        if tc.tool_name == "read_file" and tc.args.get("file_path")
    ]
    if recent_reads:
        file_counts = Counter(recent_reads)
        if any(count >= 6 for count in file_counts.values()):
            return True

    # Strategy 4: write_file failing on same file 5+ times in last 10 calls
    recent_writes = [
        tc for tc in tool_log[-10:]
        if tc.tool_name == "write_file"
    ]
    if len(recent_writes) >= 5:
        failed_files = [
            tc.args.get("file_path") for tc in recent_writes
            if isinstance(tc.result, dict) and not tc.result.get("success", True)
        ]
        if failed_files:
            file_counts = Counter(failed_files)
            if any(count >= 5 for count in file_counts.values()):
                return True

    return False


def validate_path(file_path: str) -> str | None:
    """
    Validate a file path is safe. Returns error message or None if OK.
    """
    if ".." in file_path:
        return "Path traversal not allowed (contains '..')"
    if file_path.startswith("/"):
        return "Absolute paths not allowed — use relative paths"
    if file_path.startswith("~"):
        return "Home directory paths not allowed"
    return None


def check_token_budget(used: int, limit: int) -> bool:
    """Returns True if we've exceeded the token budget."""
    return used > limit


# ── Post-write verification helpers ─────────────────────────────────────────

import re

# TS/JS: import { X, Y } from './local' or import X from '@/lib/foo'
_TS_NAMED_RE = re.compile(
    r"""import\s*\{([^}]+)\}\s*from\s*['"]([.@][^'"]+)['"]"""
)
_TS_DEFAULT_RE = re.compile(
    r"""import\s+(\w+)\s+from\s*['"]([.@][^'"]+)['"]"""
)
# Python: from .foo import bar, baz
_PY_RELATIVE_RE = re.compile(
    r"""from\s+(\.[\w.]*)\s+import\s+(.+)"""
)


def extract_new_imports(original: str, modified: str, file_path: str) -> list[str]:
    """
    Detect newly added relative/local imports (not present in original).
    Returns list of 'SymbolName from module/path' strings.
    Only checks relative imports (./  ../  @/) — not npm packages or stdlib.
    """
    def _ts_imports(content: str) -> set[str]:
        found = set()
        for m in _TS_NAMED_RE.finditer(content):
            names = [n.strip().split(" as ")[0].strip() for n in m.group(1).split(",")]
            mod = m.group(2)
            for n in names:
                if n:
                    found.add(f"{n} from '{mod}'")
        for m in _TS_DEFAULT_RE.finditer(content):
            found.add(f"{m.group(1)} from '{m.group(2)}'")
        return found

    def _py_imports(content: str) -> set[str]:
        found = set()
        for m in _PY_RELATIVE_RE.finditer(content):
            mod = m.group(1)
            names = [n.strip().split(" as ")[0].strip() for n in m.group(2).split(",")]
            for n in names:
                if n and n != "(":
                    found.add(f"{n} from '{mod}'")
        return found

    ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""

    if ext in ("ts", "tsx", "js", "jsx"):
        old_imports = _ts_imports(original)
        new_imports = _ts_imports(modified)
    elif ext == "py":
        old_imports = _py_imports(original)
        new_imports = _py_imports(modified)
    else:
        return []

    added = sorted(new_imports - old_imports)
    return added


_EXPORT_RE = re.compile(
    r"""export\s+(?:default\s+)?(?:function|const|class|let|var|interface|type|enum)\s+(\w+)"""
)


def check_deleted_exports(original: str, modified: str) -> list[str]:
    """
    Detect if exported symbols were removed from a file.
    Returns list of deleted export names.
    """
    if not original:
        return []
    orig_exports = set(_EXPORT_RE.findall(original))
    new_exports = set(_EXPORT_RE.findall(modified))
    deleted = orig_exports - new_exports
    return sorted(deleted)
