"""Conversation history compression for the agent loop (Claude format)."""
from __future__ import annotations

import json


def _extract_tool_summary(block) -> str:
    """Extract a structured summary from a tool_use block."""
    name = block.name
    args = block.input or {}
    # Keep key identifiers, not full content
    if name == "read_file":
        return f"read_file({args.get('file_path', '?')})"
    elif name == "write_file":
        fp = args.get("file_path", "?")
        n_edits = len(args.get("edits", []))
        return f"write_file({fp}, {n_edits} edits)"
    elif name == "search_code":
        return f"search_code(pattern={args.get('pattern', '?')!r})"
    elif name == "search_symbols":
        return f"search_symbols(query={args.get('query', '?')!r})"
    elif name in ("get_callers", "get_impact", "get_risk_score"):
        return f"{name}({args.get('symbol_name', args.get('file_path', '?'))})"
    elif name in ("get_dependencies", "get_test_coverage", "get_coupled_files"):
        return f"{name}({args.get('file_path', '?')})"
    elif name == "run_tests":
        paths = args.get("test_paths", [])
        return f"run_tests({', '.join(paths[:3])}{'...' if len(paths) > 3 else ''})"
    elif name == "run_command":
        cmd = args.get("command", "?")
        return f"run_command({cmd[:80]})"
    elif name == "get_diff":
        return "get_diff()"
    elif name == "finish":
        return f"finish({args.get('summary', '')[:100]})"
    else:
        args_short = json.dumps(args, default=str)[:150]
        return f"{name}({args_short})"


def _extract_result_summary(result_content: str, max_len: int = 300) -> str:
    """Extract key fields from a tool result string."""
    # Try to parse as JSON for structured extraction
    try:
        data = json.loads(result_content)
    except (json.JSONDecodeError, TypeError):
        # Plain text result — take head
        if isinstance(result_content, str):
            return result_content[:max_len]
        return str(result_content)[:max_len]

    if isinstance(data, dict):
        # Extract the most useful fields
        parts = []
        if "error" in data:
            parts.append(f"ERROR: {data['error'][:200]}")
        if "success" in data:
            parts.append(f"success={data['success']}")
        if "file_path" in data:
            parts.append(f"file={data['file_path']}")
        if "total_lines" in data:
            parts.append(f"lines={data['total_lines']}")
        if "truncated" in data and data["truncated"]:
            parts.append("(truncated)")
        if "matches" in data:
            parts.append(f"matches={len(data['matches'])}")
            for m in data["matches"][:3]:
                if isinstance(m, dict) and "file" in m:
                    parts.append(f"  {m['file']}:{m.get('line', '?')}")
        if "symbols" in data:
            parts.append(f"symbols={len(data['symbols'])}")
            for s in data["symbols"][:5]:
                if isinstance(s, dict) and "name" in s:
                    parts.append(f"  {s['name']}")
                elif isinstance(s, str):
                    parts.append(f"  {s}")
        if "callers" in data:
            parts.append(f"callers={data.get('total', len(data['callers']))}")
        if "test_files" in data:
            parts.append(f"test_files={data['test_files']}")
        if "edits" in data:
            parts.append(f"edits={data['edits']}")
        if "diff" in data:
            parts.append(f"diff_lines={data['diff'].count(chr(10))}, files_changed={data.get('files_changed', '?')}")
        if "exit_code" in data:
            parts.append(f"exit_code={data['exit_code']}")
            if data.get("stderr"):
                parts.append(f"stderr: {data['stderr'][:100]}")
        if "test_status" in data:
            parts.append(f"tests={data['test_status']} (pass={data.get('passed', 0)} fail={data.get('failed', 0)})")
        if "lint_status" in data:
            parts.append(f"lint={data['lint_status']}")

        summary = ", ".join(parts) if parts else json.dumps(data, default=str)[:max_len]
        return summary[:max_len]

    return json.dumps(data, default=str)[:max_len]


def _build_inventory(middle: list[dict]) -> dict:
    """Build an inventory of files read, files modified, and tools used from middle messages."""
    files_read: set[str] = set()
    files_modified: set[str] = set()
    symbols_found: set[str] = set()
    tools_used: dict[str, int] = {}
    # Preserve key file contents so agent doesn't have to re-read after compression
    file_contents: dict[str, str] = {}  # path → first 60 lines
    _pending_read_path: str | None = None

    for msg in middle:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant" and isinstance(content, list):
            for block in content:
                if hasattr(block, "type") and block.type == "tool_use":
                    name = block.name
                    tools_used[name] = tools_used.get(name, 0) + 1
                    args = block.input or {}
                    if name == "read_file":
                        fp = args.get("file_path", "")
                        files_read.add(fp)
                        _pending_read_path = fp
                    elif name == "write_file":
                        files_modified.add(args.get("file_path", ""))

        elif role == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    rc = item.get("content", "")
                    try:
                        data = json.loads(rc) if isinstance(rc, str) else rc
                        if isinstance(data, dict):
                            if "symbols" in data:
                                for s in data["symbols"][:10]:
                                    if isinstance(s, dict) and "name" in s:
                                        symbols_found.add(s["name"])
                                    elif isinstance(s, str):
                                        symbols_found.add(s)
                            # Preserve file content from read_file results
                            if "content" in data and "total_lines" in data and _pending_read_path:
                                lines = data["content"].splitlines()[:60]
                                file_contents[_pending_read_path] = "\n".join(lines)
                                _pending_read_path = None
                    except (json.JSONDecodeError, TypeError):
                        pass

    return {
        "files_read": sorted(files_read - {""}),
        "files_modified": sorted(files_modified - {""}),
        "symbols_found": sorted(symbols_found),
        "tools_used": tools_used,
        "file_contents": file_contents,
    }


def compress(
    messages: list[dict],
    keep_last_n: int = 6,
    compression_count: int = 0,
) -> tuple[list[dict], int]:
    """
    Compress conversation history to reduce token usage.

    Strategy:
    - Keep first user message (ticket context)
    - Summarize middle turns with structured extraction (not blind truncation)
    - Keep last N messages (most recent context)
    - After 3rd compression, switch to aggressive mode (last 4 turns + inventory only)

    Returns (compressed_messages, new_compression_count).
    """
    compression_count += 1

    # Aggressive mode after 3 compressions
    if compression_count >= 3:
        keep_last_n = 4

    if len(messages) <= keep_last_n + 1:
        return messages, compression_count

    first = messages[0]
    middle = messages[1:-keep_last_n]
    tail = messages[-keep_last_n:]

    # Build file/symbol inventory from middle messages
    inventory = _build_inventory(middle)

    # Build structured summary
    summary_lines = ["## Previous Actions Summary (compressed)"]

    if inventory["files_read"]:
        summary_lines.append(f"\n**Files read:** {', '.join(inventory['files_read'][:20])}")
    if inventory["files_modified"]:
        summary_lines.append(f"**Files modified:** {', '.join(inventory['files_modified'][:10])}")
    if inventory["symbols_found"]:
        summary_lines.append(f"**Symbols found:** {', '.join(inventory['symbols_found'][:15])}")
    if inventory["tools_used"]:
        tool_counts = ", ".join(f"{k}={v}" for k, v in sorted(inventory["tools_used"].items()))
        summary_lines.append(f"**Tool usage:** {tool_counts}")

    # Preserve key file contents (max 3 files, 60 lines each)
    # Prioritize modified files, then most recently read files
    preserved = inventory.get("file_contents", {})
    if preserved:
        # Prioritize files that were modified (agent needs original content for search strings)
        modified_set = set(inventory["files_modified"])
        priority_files = [p for p in preserved if p in modified_set]
        other_files = [p for p in preserved if p not in modified_set]
        ordered = (priority_files + other_files)[:3]
        if ordered:
            summary_lines.append("\n### Key File Contents (preserved across compression)")
            for path in ordered:
                summary_lines.append(f"\n**{path}** (first 60 lines):")
                summary_lines.append(f"```\n{preserved[path]}\n```")

    summary_lines.append("\n### Action log:")

    for msg in middle:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant" and isinstance(content, list):
            for block in content:
                if hasattr(block, "type"):
                    if block.type == "tool_use":
                        summary_lines.append(f"- Called {_extract_tool_summary(block)}")
                    elif block.type == "text" and block.text:
                        # Keep planning/reasoning text (more than 80 chars)
                        text = block.text.strip()
                        if len(text) > 20:
                            summary_lines.append(f"- Thought: {text[:200]}")
        elif role == "user" and isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    rc = item.get("content", "")
                    summary = _extract_result_summary(rc)
                    summary_lines.append(f"  → {summary}")
        elif role == "user" and isinstance(content, str):
            summary_lines.append(f"- User: {content[:200]}")

    summary_text = "\n".join(summary_lines)

    # Cap total summary size
    if len(summary_text) > 6000:
        summary_text = summary_text[:6000] + "\n... [summary truncated]"

    summary_msg = {"role": "user", "content": summary_text}

    # Collect all tool_use IDs that exist in kept messages (first + summary_msg + tail)
    valid_tool_ids: set[str] = set()
    for msg in [first, summary_msg] + tail:
        content = msg.get("content", [])
        if isinstance(content, list):
            for block in content:
                if hasattr(block, "type") and block.type == "tool_use":
                    if hasattr(block, "id"):
                        valid_tool_ids.add(block.id)

    # Filter out orphaned tool_result blocks from tail
    cleaned_tail = []
    for msg in tail:
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                cleaned_content = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        # Keep only if tool_use_id matches a valid tool
                        if item.get("tool_use_id") in valid_tool_ids:
                            cleaned_content.append(item)
                    else:
                        cleaned_content.append(item)
                msg = {**msg, "content": cleaned_content}
        cleaned_tail.append(msg)

    return [first, summary_msg] + cleaned_tail, compression_count
