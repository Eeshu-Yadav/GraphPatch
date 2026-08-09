from __future__ import annotations
import os
from pathlib import Path
import structlog

log = structlog.get_logger(__name__)


def get_repo_path(repo_id: str) -> Path:
    slug = repo_id.replace("/", "_")
    cache = os.environ.get("REPO_CACHE_DIR", "/home/eeshu/Desktop/context/repos")
    return Path(cache) / slug


def read_files(repo_id: str, file_paths: list[str], max_chars_per_file: int = 40000) -> dict[str, str]:
    """
    Read file contents from the cloned repo.
    For large files, returns the full content up to max_chars_per_file.
    Returns {relative_path: content}
    """
    repo_path = get_repo_path(repo_id)
    contents = {}

    for rel_path in file_paths:
        abs_path = repo_path / rel_path
        if not abs_path.exists():
            log.warning("file_reader.missing", path=rel_path)
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
            if len(text) > max_chars_per_file:
                # Keep whole file but warn — caller should use read_file_focused for targeted edits
                log.warning("file_reader.large", path=rel_path, chars=len(text))
                text = text[:max_chars_per_file]
            contents[rel_path] = text
        except Exception as e:
            log.warning("file_reader.error", path=rel_path, error=str(e))

    return contents


def read_file_focused(
    repo_id: str,
    file_path: str,
    symbols: list[str],
    context_lines: int = 60,
) -> str:
    """
    For large files: return only the sections containing the given symbols,
    plus context_lines of surrounding code. Includes file header (imports).
    Prevents LLM from hallucinating truncated regions.
    """
    repo_path = get_repo_path(repo_id)
    abs_path = repo_path / file_path
    if not abs_path.exists():
        return ""

    text = abs_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)

    # Always include header (imports, module docstring) — first 30 lines
    header_end = min(30, total)
    included: set[int] = set(range(header_end))

    # Find lines containing each symbol name
    for sym in symbols:
        for i, line in enumerate(lines):
            if sym in line and ("def " in line or "class " in line):
                start = max(0, i - 5)
                end = min(total, i + context_lines)
                included.update(range(start, end))

    # If nothing found, return full file (small enough or no matches)
    if len(included) >= total * 0.8:
        return text

    # Build focused view with gap markers
    result_lines = []
    prev = -1
    for i in sorted(included):
        if prev != -1 and i > prev + 1:
            result_lines.append(f"\n# ... [{i - prev - 1} lines omitted] ...\n")
        result_lines.append(lines[i])
        prev = i

    focused = "\n".join(result_lines)
    log.debug("file_reader.focused", path=file_path, orig_lines=total, focused_lines=len(result_lines))
    return focused
