"""Tool implementations — 8 graph-powered + 7 standard tools."""
from __future__ import annotations

import difflib
import os
import re
import subprocess
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


# ── Graph Tools (thin wrappers around L2 queries) ────────────────────────────

def tool_get_callers(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_callers
    result = get_callers(repo_id, args["symbol_name"], depth=args.get("depth", 1))
    return {"callers": result, "total": len(result)}


def tool_get_impact(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_impact
    result = get_impact(repo_id, args["symbol_name"], depth=args.get("depth", 2))
    # Cap impact results to avoid token bloat
    if isinstance(result, dict):
        for key in ("affected_files", "affected_symbols", "impacts"):
            if key in result and isinstance(result[key], list) and len(result[key]) > 20:
                result[key] = result[key][:20]
                result[f"{key}_truncated"] = True
    return result


def tool_get_dependencies(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_file_dependencies
    result = get_file_dependencies(repo_id, args["file_path"])
    # Cap dependency lists to avoid token bloat
    if isinstance(result, dict):
        for key in ("imports", "imported_by", "dependencies", "dependents"):
            if key in result and isinstance(result[key], list) and len(result[key]) > 30:
                result[key] = result[key][:30]
                result[f"{key}_truncated"] = True
    return result


def tool_get_test_coverage(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_test_files
    return {"test_files": get_test_files(repo_id, args["file_path"])}



def tool_get_coupled_files(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_git_coupling
    return {"coupled": get_git_coupling(repo_id, args["file_path"], min_score=args.get("min_score", 0.1))}


def tool_search_symbols(args: dict, repo_id: str, **_) -> dict:
    from src.semantic.embeddings import embed_single
    from src.semantic.vector_store import search
    vector = embed_single(args["query"])
    if not vector:
        return {"symbols": [], "error": "Failed to generate embedding"}
    results = search(
        query_vector=vector,
        repo_id=repo_id,
        entity_types=args.get("entity_types"),
        limit=args.get("limit", 10),
        min_score=args.get("min_score", 0.35),
    )
    # Trim docstrings to save tokens
    for sym in results:
        if isinstance(sym, dict) and "docstring" in sym and sym["docstring"]:
            sym["docstring"] = sym["docstring"][:100]
    return {"symbols": results}


def tool_get_risk_score(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_risk_score
    return get_risk_score(repo_id, args["file_path"])


def tool_get_reviewers(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_pr_reviewers
    return get_pr_reviewers(repo_id, args.get("file_paths", []))


# ── Graph Tools (new — exposing existing queries + composite) ────────────────

def tool_get_top_files(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_top_files
    prefix = args.get("path_prefix", "")
    limit = min(args.get("limit", 10), 15)
    results = get_top_files(repo_id, path_prefix=prefix, limit=limit)
    return {"files": results, "total": len(results)}


def tool_get_file_info(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import get_file_summary
    summary = get_file_summary(repo_id, args["file_path"])
    if not summary:
        return {"error": f"File not found in graph index: {args['file_path']}"}
    return {
        "path": summary.path,
        "language": summary.language,
        "lines": summary.lines,
        "centrality": summary.centrality,
        "summary": summary.summary,
        "is_test": summary.is_test,
    }


def tool_get_symbol_details(args: dict, repo_id: str, **_) -> dict:
    from src.graph.queries import lookup_symbols_batch
    names = args["names"][:10]
    results = lookup_symbols_batch(repo_id, names)
    # Trim docstrings to save tokens
    for sym in results:
        if isinstance(sym, dict) and "docstring" in sym and sym["docstring"]:
            sym["docstring"] = sym["docstring"][:100]
    return {"symbols": results, "total": len(results)}


def tool_get_class_hierarchy(args: dict, repo_id: str, **_) -> dict:
    from src.graph import client as g
    class_name = args["class_name"]
    # Parents (what this class inherits from)
    parents = g.run(
        """
        MATCH (c:Class {name: $name, repo_id: $repo_id})-[:INHERITS*1..5]->(parent:Class)
        RETURN DISTINCT parent.name AS name, parent.file_path AS file,
               coalesce(parent.line_start, 0) AS line_start
        """,
        {"name": class_name, "repo_id": repo_id},
    )
    # Children (what inherits from this class)
    children = g.run(
        """
        MATCH (c:Class {name: $name, repo_id: $repo_id})<-[:INHERITS*1..3]-(child:Class)
        RETURN DISTINCT child.name AS name, child.file_path AS file,
               coalesce(child.line_start, 0) AS line_start
        """,
        {"name": class_name, "repo_id": repo_id},
    )
    # Methods of this class (via CONTAINS + qualified_name)
    methods = g.run(
        """
        MATCH (f:File)-[:CONTAINS]->(fn:Function)
        WHERE fn.repo_id = $repo_id
          AND fn.qualified_name STARTS WITH $prefix
        RETURN fn.name AS name, fn.qualified_name AS qualified_name,
               fn.file_path AS file, fn.line_start AS line_start,
               fn.is_async AS is_async
        ORDER BY fn.line_start
        LIMIT 30
        """,
        {"repo_id": repo_id, "prefix": class_name + "."},
    )
    return {
        "class": class_name,
        "parents": parents,
        "children": children,
        "methods": methods,
    }


def tool_get_change_context(args: dict, repo_id: str, **_) -> dict:
    """Composite pre-change analysis — the single most important graph tool."""
    from src.graph.queries import (
        get_risk_score, get_file_dependencies, get_test_files,
        get_git_coupling, get_impact, get_file_summary,
    )
    fp = args["file_path"]
    sym = args.get("symbol_name", "")

    result = {}

    # Risk score
    result["risk"] = get_risk_score(repo_id, fp)

    # Dependencies
    deps = get_file_dependencies(repo_id, fp)
    result["dependencies"] = deps.get("dependencies", [])[:10]
    result["dependents"] = deps.get("dependents", [])[:10]

    # Test coverage
    result["test_files"] = get_test_files(repo_id, fp)

    # Coupled files (co-change history)
    coupling = get_git_coupling(repo_id, fp, min_score=0.2)
    result["coupled_files"] = coupling[:10]

    # Symbol-specific: callers + impact
    if sym:
        impact = get_impact(repo_id, sym, depth=2)
        result["will_break"] = impact.get("will_break", [])[:10]
        result["may_break"] = impact.get("may_break", [])[:5]
        result["total_affected_files"] = impact.get("total_affected_files", 0)

    # File summary
    summary = get_file_summary(repo_id, fp)
    if summary:
        result["file_info"] = {
            "language": summary.language,
            "lines": summary.lines,
            "centrality": summary.centrality,
            "summary": summary.summary,
        }

    return result


# ── Standard Tools ───────────────────────────────────────────────────────────

def tool_read_file(
    args: dict,
    repo_path: Path,
    modified_files: dict[str, str],
    **_,
) -> dict:
    fp = args["file_path"]

    if ".." in fp or fp.startswith("/"):
        return {"error": f"Invalid path: {fp}. Must be relative, no '..'"}

    # Check virtual FS first (agent's own edits)
    if fp in modified_files:
        content = modified_files[fp]
    else:
        abs_path = repo_path / fp
        if not abs_path.exists():
            return {"error": f"File not found: {fp}"}
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": f"Failed to read {fp}: {e}"}

    lines = content.splitlines(keepends=True)
    total_lines = len(lines)

    start = max(1, args.get("start_line", 1)) - 1
    end = args.get("end_line", 0)
    if end > 0:
        lines = lines[start:end]
    else:
        lines = lines[start:]

    result_content = "".join(lines)
    truncated = len(result_content) > 20_000
    if truncated:
        result_content = result_content[:20_000] + "\n... [truncated — use start_line/end_line to read specific sections]"

    return {
        "content": result_content,
        "total_lines": total_lines,
        "truncated": truncated,
    }


def _find_best_match(search_lines: list[str], current_lines: list[str]) -> tuple[int, int, float]:
    """
    Find the best matching region in current_lines for search_lines.
    Returns (start_idx, end_idx, ratio). ratio >= 0.85 is a good match.
    """
    search_len = len(search_lines)
    if search_len == 0 or len(current_lines) == 0:
        return (0, 0, 0.0)

    best_start, best_ratio = 0, 0.0

    # Slide a window of search_len over current_lines
    for start in range(len(current_lines) - search_len + 1):
        candidate = current_lines[start:start + search_len]
        matcher = difflib.SequenceMatcher(None, candidate, search_lines, autojunk=False)
        ratio = matcher.ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_start = start

    return (best_start, best_start + search_len, best_ratio)


def tool_write_file(
    args: dict,
    repo_path: Path,
    modified_files: dict[str, str],
    original_files: dict[str, str],
    **_,
) -> dict:
    fp = args["file_path"]

    if ".." in fp or fp.startswith("/"):
        return {"success": False, "file_path": fp, "error": "Invalid path"}

    edits = args.get("edits", [])

    # Defensive: LLM sometimes passes edits as a JSON-encoded string instead of a list.
    # Iterating a string yields single chars, which causes silent corruption.
    if isinstance(edits, str):
        import json as _json
        # Clean: strip whitespace and any trailing XML-like tags from tool_use format
        cleaned = edits.strip()
        # Remove anything after the last ] (trailing tags, newlines, etc.)
        bracket_pos = cleaned.rfind(']')
        if bracket_pos >= 0:
            cleaned = cleaned[:bracket_pos + 1]

        parsed = None
        for attempt_str in [cleaned, cleaned.replace("\\'", "'")]:
            try:
                parsed = _json.loads(attempt_str)
                break
            except (ValueError, TypeError):
                continue
        # Fallback: Python literal (handles single-quoted dicts)
        if parsed is None:
            try:
                import ast
                parsed = ast.literal_eval(cleaned)
            except (ValueError, SyntaxError):
                pass
        if parsed is None or not isinstance(parsed, list):
            return {"success": False, "file_path": fp, "error": "edits is a string but could not parse as a list of edit objects"}
        edits = parsed

    if not edits:
        return {"success": False, "file_path": fp, "error": "No edits provided"}

    if not isinstance(edits, list):
        return {"success": False, "file_path": fp, "error": f"edits must be a list, got {type(edits).__name__}"}

    # Defensive: handle individual edits that are strings or malformed
    cleaned_edits = []
    for i, edit in enumerate(edits):
        if isinstance(edit, str):
            # Single edit passed as JSON string
            try:
                import json as _json
                edit = _json.loads(edit)
            except (ValueError, TypeError):
                # Treat as full file replacement
                cleaned_edits.append({"search": "", "replace": edit})
                continue
        if isinstance(edit, dict):
            cleaned_edits.append(edit)
        else:
            return {"success": False, "file_path": fp, "error": f"Edit {i+1}: expected dict with 'search' and 'replace', got {type(edit).__name__}"}
    edits = cleaned_edits

    # Get current content
    if fp in modified_files:
        current = modified_files[fp]
    else:
        abs_path = repo_path / fp
        if abs_path.exists():
            current = abs_path.read_text(encoding="utf-8", errors="replace")
        else:
            current = ""

    # Save original for diff
    if fp not in original_files:
        original_files[fp] = current

    # Snapshot before edits — rollback if result is catastrophically corrupted
    pre_edit_content = current

    # Push to edit history for undo_edit support
    if fp not in _edit_history:
        _edit_history[fp] = []
    _edit_history[fp].append(current)
    # Keep max 5 history entries per file
    if len(_edit_history[fp]) > 5:
        _edit_history[fp] = _edit_history[fp][-5:]

    edit_results = []

    # Apply edits sequentially
    for i, edit in enumerate(edits):
        search = edit.get("search", "")
        replace = edit.get("replace", "")

        # Line-range editing mode: {start_line, end_line, replace}
        # Avoids search-string matching entirely — precise line-based edits
        if "start_line" in edit and "end_line" in edit:
            start = max(1, int(edit["start_line"])) - 1  # 1-indexed to 0-indexed
            end = int(edit["end_line"])
            lines = current.splitlines(keepends=True)
            if start >= len(lines):
                edit_results.append({"edit": i + 1, "status": "failed", "error": f"start_line {start+1} beyond file ({len(lines)} lines)"})
                continue
            end = min(end, len(lines))
            replaced_lines = "".join(lines[start:end])
            new_lines = replace if replace.endswith("\n") or not replace else replace + "\n"
            current = "".join(lines[:start]) + new_lines + "".join(lines[end:])
            edit_results.append({"edit": i + 1, "status": "line_range", "lines": f"{start+1}-{end}", "replaced": len(replaced_lines), "new": len(new_lines)})
            continue

        # New file creation: empty search = full replacement
        if search == "":
            current = replace
            edit_results.append({"edit": i + 1, "status": "created"})
            continue

        # Strategy 1: Exact string match (fast path)
        if search in current:
            current = current.replace(search, replace, 1)
            edit_results.append({"edit": i + 1, "status": "exact_match"})
            continue

        # Strategy 2: Normalized match (strip trailing whitespace per line)
        search_norm = "\n".join(line.rstrip() for line in search.split("\n"))
        current_norm = "\n".join(line.rstrip() for line in current.split("\n"))
        if search_norm in current_norm:
            # Find the line-level position in the normalized version
            idx = current_norm.index(search_norm)
            pre_lines = current_norm[:idx].count("\n")
            search_line_count = search_norm.count("\n") + 1
            orig_lines = current.splitlines(keepends=True)
            before = "".join(orig_lines[:pre_lines])
            after = "".join(orig_lines[pre_lines + search_line_count:])
            # Ensure replace ends with newline if replacing whole lines
            if replace and not replace.endswith("\n") and after and after[0] != "\n":
                replace += "\n"
            current = before + replace + after
            edit_results.append({"edit": i + 1, "status": "normalized_match"})
            continue

        # Strategy 3: Fuzzy difflib match (handles indentation + minor differences)
        search_lines_list = search.splitlines(keepends=True)
        current_lines_list = current.splitlines(keepends=True)

        start, end, ratio = _find_best_match(search_lines_list, current_lines_list)

        if ratio >= 0.85:
            replace_lines = replace.splitlines(keepends=True)
            # Ensure trailing newline consistency
            if replace_lines and not replace_lines[-1].endswith("\n"):
                if end < len(current_lines_list):
                    replace_lines[-1] += "\n"
            current_lines_list = current_lines_list[:start] + replace_lines + current_lines_list[end:]
            current = "".join(current_lines_list)
            edit_results.append({"edit": i + 1, "status": "fuzzy_match", "ratio": round(ratio, 2)})
            continue

        # All strategies failed — return context around best partial match
        context_start = max(0, start - 3)
        context_end = min(len(current_lines_list), end + 3)
        nearby = "".join(current_lines_list[context_start:context_end])
        return {
            "success": False,
            "file_path": fp,
            "error": (
                f"Edit {i + 1}: search string not found (best match ratio={ratio:.2f} at lines {start + 1}-{end}).\n"
                f"Nearby content (lines {context_start + 1}-{context_end}):\n{nearby[:500]}"
            ),
            "edits_applied": edit_results,
        }

    if fp in original_files and current == original_files[fp]:
        return {"success": True, "file_path": fp, "warning": "No actual change — content identical after edits", "edits": edit_results}

    # Size guard: limit by CHANGE SIZE, not file size.
    # Large files (150KB) are fine to edit if the change is small.
    # Block only if the diff itself is unreasonably large (likely a full-file rewrite).
    original = original_files.get(fp, "")
    if original:
        orig_lines = original.splitlines(keepends=True)
        cur_lines = current.splitlines(keepends=True)
        # Count only added/removed lines in the unified diff (lines starting with +/-)
        diff_lines = list(difflib.unified_diff(orig_lines, cur_lines, n=0))
        changed_lines = sum(1 for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
        changed_chars = sum(len(l) for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
        if changed_lines > 500 or changed_chars > 30_000:
            return {
                "success": False, "file_path": fp,
                "error": f"Change too large ({changed_lines} lines, {changed_chars} chars in diff). "
                         f"Use targeted search/replace edits, not full-file rewrites. "
                         f"Read the specific section with start_line/end_line, then edit just that section.",
            }
    else:
        # New file: cap at 50K
        if len(current) > 50_000:
            return {"success": False, "file_path": fp, "error": "New file too large (>50k chars)"}

    # Quick syntax check for Python files
    if fp.endswith(".py"):
        try:
            compile(current, fp, "exec")
        except SyntaxError as e:
            log.warning("write_file.syntax_warning", file=fp, error=str(e))
            # Don't fail — agent can fix in next iteration, but warn
            edit_results.append({"warning": f"Python syntax error after edits: {e}"})

    # Post-write verification: detect new imports + deleted exports
    from layer45_agent.safety import extract_new_imports, check_deleted_exports

    original_content = original_files.get(fp, "")
    new_imports = extract_new_imports(original_content, current, fp)
    if new_imports:
        edit_results.append({
            "new_imports_detected": new_imports[:10],
            "warning": "New local imports added. VERIFY these exist with search_code before finishing. "
                       "If search returns 0 results, the import is hallucinated — revert it.",
        })

    deleted = check_deleted_exports(original_content, current)
    if deleted:
        edit_results.append({
            "deleted_exports": deleted,
            "warning": "Existing exports were DELETED. Unless the ticket explicitly asks for removal, "
                       "revert these deletions — you may be breaking consumers.",
        })

    # Corruption guard: if the edit reduced a large file to almost nothing, rollback
    if pre_edit_content and len(pre_edit_content) > 100 and len(current.strip()) < 10:
        log.warning("write_file.corruption_detected", file=fp,
                     before=len(pre_edit_content), after=len(current))
        modified_files[fp] = pre_edit_content  # restore pre-edit state
        return {
            "success": False, "file_path": fp,
            "error": f"Edit resulted in near-empty file ({len(current)} chars from {len(pre_edit_content)} chars). "
                     f"This looks like corruption — edits rolled back. "
                     f"Re-read the file and use more precise search strings.",
        }

    modified_files[fp] = current
    return {"success": True, "file_path": fp, "edits": edit_results}


def tool_search_code(args: dict, repo_path: Path, **_) -> dict:
    pattern = args.get("pattern", "")
    file_glob = args.get("file_glob", "")
    max_results = args.get("max_results", 20)

    # Use ripgrep (rg) if available, else fall back to grep.
    # ripgrep handles --glob natively (supports ** patterns).
    use_rg = subprocess.run(["which", "rg"], capture_output=True).returncode == 0

    if use_rg:
        cmd = ["rg", "-n", "--no-heading", "--max-count", str(max_results)]
        if file_glob:
            # rg --glob supports ** patterns natively
            cmd.extend(["--glob", file_glob])
        cmd.extend([pattern, str(repo_path)])
    else:
        cmd = ["grep", "-rn", "--binary-files=without-match"]
        if file_glob:
            # grep --include only supports simple patterns like *.py.
            # Convert path globs: "tests/**/*.py" → --include="*.py" + search in tests/ subdir
            if "**" in file_glob or "/" in file_glob:
                # Extract the extension pattern and search path
                import fnmatch
                parts = file_glob.rsplit("/", 1)
                if len(parts) == 2:
                    subdir = parts[0].replace("**", "")
                    ext_pattern = parts[1]  # e.g. "*.py"
                    cmd.extend(["--include", ext_pattern])
                    # Narrow search to subdir if it exists
                    search_path = repo_path / subdir.strip("/")
                    if search_path.is_dir():
                        cmd.extend([pattern, str(search_path)])
                    else:
                        cmd.extend([pattern, str(repo_path)])
                else:
                    cmd.extend(["--include", file_glob, pattern, str(repo_path)])
            else:
                cmd.extend(["--include", file_glob, pattern, str(repo_path)])
        else:
            cmd.extend([pattern, str(repo_path)])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        matches = []
        for line in proc.stdout.splitlines()[:max_results]:
            parts = line.split(":", 2)
            if len(parts) >= 3:
                file_path = parts[0].replace(str(repo_path) + "/", "")
                matches.append({
                    "file": file_path,
                    "line": int(parts[1]) if parts[1].isdigit() else 0,
                    "text": parts[2][:200],
                })

        # Auto-retry without file_glob if empty results (glob might be wrong)
        if not matches and file_glob:
            fallback_cmd = ["grep", "-rn", "--binary-files=without-match", pattern, str(repo_path)]
            fallback = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=10)
            for line in fallback.stdout.splitlines()[:max_results]:
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    file_path = parts[0].replace(str(repo_path) + "/", "")
                    matches.append({
                        "file": file_path,
                        "line": int(parts[1]) if parts[1].isdigit() else 0,
                        "text": parts[2][:200],
                    })
            if matches:
                return {"matches": matches, "total": len(matches), "note": f"file_glob '{file_glob}' matched nothing, showing unfiltered results"}

        return {"matches": matches, "total": len(matches)}
    except subprocess.TimeoutExpired:
        return {"matches": [], "error": "Search timed out"}
    except Exception as e:
        return {"matches": [], "error": str(e)}


def _parse_test_failures(output: str) -> list[dict]:
    """Parse pytest output into structured failure details."""
    failures = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        # Look for FAILED markers
        if "FAILED" in line and "::" in line:
            test_name = line.split("FAILED")[-1].strip().strip("-").strip()
            if not test_name:
                parts = line.split()
                test_name = next((p for p in parts if "::" in p), line.strip())
            failures.append({"test": test_name, "error": "", "detail": ""})
        # Look for assertion errors with expected/actual
        elif "AssertionError" in line or "assert " in line:
            detail = line.strip()
            # Grab a few surrounding lines for context
            context = "\n".join(lines[max(0, i-2):min(len(lines), i+3)])
            if failures:
                failures[-1]["error"] = "AssertionError"
                failures[-1]["detail"] = context[:500]
            else:
                failures.append({"test": "unknown", "error": "AssertionError", "detail": context[:500]})
        elif "Error" in line and ("expected" in line.lower() or "actual" in line.lower() or "got" in line.lower()):
            if failures:
                failures[-1]["detail"] += "\n" + line.strip()
        i += 1
    return failures[:10]  # max 10 failures


def tool_run_tests(
    args: dict,
    repo_path: Path,
    modified_files: dict[str, str],
    **_,
) -> dict:
    # Flush virtual FS to disk
    for fp, content in modified_files.items():
        abs_path = repo_path / fp
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    from layer6_validator.runner import run_tests, run_lint, _find_test_files

    test_paths = args.get("test_paths", [])
    include_lint = args.get("include_lint", True)

    if not test_paths:
        changed = list(modified_files.keys())
        test_paths = _find_test_files(repo_path, changed)

    test_result = run_tests(repo_path, test_paths)

    # Detect test infrastructure failures (missing deps, broken conftest, config errors)
    from layer45_agent.sandbox import classify_test_error
    error_info = classify_test_error(test_result.output)

    result = {
        "test_status": test_result.status.value,
        "passed": test_result.passed,
        "failed": test_result.failed,
        "errors": test_result.errors,
        "output": test_result.output[:8000],
        "failures": _parse_test_failures(test_result.output),
    }

    if error_info["is_infra"]:
        result["infrastructure_error"] = True
        result["error_type"] = error_info["error_type"]
        missing = error_info.get("missing_modules", [])
        pip_pkgs = error_info.get("pip_packages", [])
        if missing:
            result["missing_modules"] = missing
        result["warning"] = (
            f"Tests failed due to {error_info['error_type']}. "
            + (f"Missing modules: {', '.join(missing)}. Try: pip install {' '.join(pip_pkgs)}" if pip_pkgs
               else "Use run_command to install the project and its test dependencies.")
        )

    if include_lint:
        lint_paths = [fp for fp in modified_files.keys() if fp.endswith(".py")]
        if lint_paths:
            lint_result = run_lint(repo_path, lint_paths)
            result["lint_status"] = lint_result.status.value
            result["lint_issues"] = lint_result.issues[:20]
        else:
            result["lint_status"] = "skipped"
            result["lint_issues"] = []

    return result


def tool_run_command(args: dict, repo_path: Path, modified_files: dict[str, str], **_) -> dict:
    """Execute a shell command in the repo root."""
    command = args.get("command", "")
    if not command:
        return {"error": "No command provided"}

    # Block dangerous commands
    dangerous = ["rm -rf", "rm -r /", "mkfs", "dd if=", "> /dev/", ":(){ :|:& };:"]
    for d in dangerous:
        if d in command:
            return {"error": f"Blocked dangerous command: {d}"}

    # Flush virtual FS to disk first
    for fp, content in modified_files.items():
        abs_path = repo_path / fp
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = proc.stdout[-3000:] if len(proc.stdout) > 3000 else proc.stdout
        stderr = proc.stderr[-2000:] if len(proc.stderr) > 2000 else proc.stderr
        return {
            "exit_code": proc.returncode,
            "stdout": output,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out (30s limit)"}
    except Exception as e:
        return {"error": f"Command failed: {e}"}


def tool_build_check(args: dict, repo_path: Path, modified_files: dict[str, str], **_) -> dict:
    """Check if code compiles/builds successfully (language-agnostic)."""
    # Flush modified files to disk first
    for fp, content in modified_files.items():
        abs_path = repo_path / fp
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    # Detect package manager from lockfiles (don't try all blindly)
    pm = None
    if (repo_path / "bun.lockb").exists() or (repo_path / "bun.lock").exists():
        pm = "bun"
    elif (repo_path / "yarn.lock").exists():
        pm = "yarn"
    elif (repo_path / "pnpm-lock.yaml").exists():
        pm = "pnpm"
    elif (repo_path / "package-lock.json").exists() or (repo_path / "package.json").exists():
        pm = "npm"

    build_commands: list[tuple[list[str], str, str]] = []  # (cmd, name, cwd)
    root = str(repo_path)
    # TypeScript type-check FIRST (catches import errors that bundlers miss)
    if (repo_path / "tsconfig.json").exists():
        if pm:
            build_commands.append((["npx", "tsc", "--noEmit"], "tsc --noEmit", root))
        else:
            build_commands.append((["tsc", "--noEmit"], "tsc --noEmit", root))
    # Framework/bundler build
    if pm and (repo_path / "package.json").exists():
        build_commands.append(([pm, "run", "build"], f"{pm} build", root))
    if (repo_path / "Cargo.toml").exists():
        build_commands.append((["cargo", "build", "--message-format=short"], "cargo build", root))
    if (repo_path / "go.mod").exists():
        build_commands.append((["go", "build", "./..."], "go build", root))

    # Search subdirectories if nothing found at root (monorepo support)
    if not build_commands:
        for subdir_name in ["frontend", "backend", "dashboard", "app", "web", "client", "server"]:
            subdir = repo_path / subdir_name
            if not subdir.is_dir():
                continue
            sub_cwd = str(subdir)
            sub_pm = None
            if (subdir / "package.json").exists():
                if (subdir / "bun.lockb").exists() or (subdir / "bun.lock").exists():
                    sub_pm = "bun"
                elif (subdir / "yarn.lock").exists():
                    sub_pm = "yarn"
                elif (subdir / "pnpm-lock.yaml").exists():
                    sub_pm = "pnpm"
                else:
                    sub_pm = "npm"
            if (subdir / "tsconfig.json").exists():
                tsc_cmd = ["npx", "tsc", "--noEmit"] if sub_pm else ["tsc", "--noEmit"]
                build_commands.append((tsc_cmd, f"tsc --noEmit ({subdir_name}/)", sub_cwd))
            if sub_pm and (subdir / "package.json").exists():
                build_commands.append(([sub_pm, "run", "build"], f"{sub_pm} build ({subdir_name}/)", sub_cwd))

    if not build_commands:
        return {"status": "skipped", "message": "No build system detected"}

    for cmd, name, cwd in build_commands:
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if proc.returncode == 0:
                log.info("build.success", command=name)
                return {"status": "success", "command": name}

            # Build failed — detect missing deps vs real code errors
            output = proc.stdout + proc.stderr
            output_lower = output.lower()

            missing_deps_signals = [
                "command not found", "not found: ",
                "cannot find module", "module not found",
                "enoent", "err_module_not_found", "could not resolve",
            ]
            if any(sig in output_lower for sig in missing_deps_signals):
                # Distinguish missing npm deps from broken local imports
                # Relative/aliased imports (./  ../  @/) are code bugs, not missing deps
                relative_errors = [
                    line.strip() for line in output.splitlines()
                    if any(k in line.lower() for k in ["cannot find module", "module not found", "could not resolve"])
                    and any(p in line for p in ["'./", "'../", "'@/", '"./', '"../', '"@/'])
                ]
                if relative_errors:
                    log.info("build.local_import_error", command=name, errors=len(relative_errors))
                    return {
                        "status": "failed",
                        "command": name,
                        "errors": relative_errors[:5],
                        "output": output[-1500:],
                        "message": "Local import errors detected — these are code bugs, not missing dependencies.",
                    }
                log.info("build.missing_deps", command=name)
                return {
                    "status": "skipped",
                    "command": name,
                    "message": "Dependencies not installed. Build check not possible — proceed with code review instead.",
                }

            errors = [
                line.strip()
                for line in output.splitlines()
                if any(k in line.lower() for k in ["error", "failed", "cannot find"])
            ]

            log.info("build.failed", command=name, errors=len(errors))
            return {
                "status": "failed",
                "command": name,
                "errors": errors[:5],
                "output": output[-1500:],
            }

        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "command": name, "error": "Build timed out (60s)"}
        except Exception as e:
            continue

    return {"status": "skipped", "reason": "No build system detected"}


def tool_get_diff(
    args: dict,
    modified_files: dict[str, str],
    original_files: dict[str, str],
    **_,
) -> dict:
    """Generate unified diff of all changes made so far."""
    diffs = []
    for fp in sorted(modified_files.keys()):
        original = original_files.get(fp, "")
        modified = modified_files[fp]
        if original == modified:
            continue

        diff_lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile=f"a/{fp}",
            tofile=f"b/{fp}",
        )
        diffs.append("".join(diff_lines))

    if not diffs:
        return {"diff": "", "files_changed": 0, "message": "No changes made yet"}

    full_diff = "\n".join(diffs)
    if len(full_diff) > 4000:
        full_diff = full_diff[:4000] + "\n... [truncated]"

    return {"diff": full_diff, "files_changed": len(diffs)}


def tool_finish(args: dict, **_) -> dict:
    # Soft gate: warn if agent didn't verify blast radius for modified files
    warnings = []
    for fp in args.get("files_changed", []):
        cache_key = _make_cache_key("get_change_context", {"file_path": fp})
        if cache_key not in _result_cache:
            warnings.append(
                f"You did not call get_change_context for '{fp}'. "
                f"Consider verifying blast radius before finalizing."
            )
    result = {
        "acknowledged": True,
        "summary": args.get("summary", ""),
        "files_changed": args.get("files_changed", []),
    }
    if warnings:
        result["warnings"] = warnings[:3]  # Cap at 3 warnings
    return result


# ── Environment Setup ────────────────────────────────────────────────────────

_env_installed: set[str] = set()


def tool_setup_environment(args: dict, repo_path: Path, **_) -> dict:
    """Auto-detect project type and install dependencies so tests can run."""
    key = str(repo_path)
    if key in _env_installed:
        return {"status": "already_installed", "message": "Dependencies were already installed."}

    install_attempts = []

    # Detect project type and try install commands in order
    # Python
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        for cmd, name in [
            ([sys.executable, "-m", "pip", "install", "-e", ".[test]", "-q"], "pip install -e .[test]"),
            ([sys.executable, "-m", "pip", "install", "-e", ".[dev]", "-q"], "pip install -e .[dev]"),
            ([sys.executable, "-m", "pip", "install", "-e", ".", "-q"], "pip install -e ."),
        ]:
            try:
                proc = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=300)
                install_attempts.append({"cmd": name, "exit_code": proc.returncode})
                if proc.returncode == 0:
                    _env_installed.add(key)
                    log.info("setup_env.installed", method=name)
                    return {"status": "installed", "method": name}
            except (subprocess.TimeoutExpired, FileNotFoundError):
                install_attempts.append({"cmd": name, "error": "timeout or not found"})
                continue

    if (repo_path / "requirements.txt").exists():
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
                cwd=str(repo_path), capture_output=True, text=True, timeout=300,
            )
            install_attempts.append({"cmd": "pip install -r requirements.txt", "exit_code": proc.returncode})
            if proc.returncode == 0:
                _env_installed.add(key)
                return {"status": "installed", "method": "pip install -r requirements.txt"}
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    # JS/TS
    for pm_file, cmd, name in [
        ("bun.lockb", ["bun", "install"], "bun install"),
        ("yarn.lock", ["yarn", "install"], "yarn install"),
        ("pnpm-lock.yaml", ["pnpm", "install"], "pnpm install"),
        ("package.json", ["npm", "install"], "npm install"),
    ]:
        if (repo_path / pm_file).exists():
            try:
                proc = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=300)
                install_attempts.append({"cmd": name, "exit_code": proc.returncode})
                if proc.returncode == 0:
                    _env_installed.add(key)
                    return {"status": "installed", "method": name}
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue

    # Rust / Go
    if (repo_path / "Cargo.toml").exists():
        try:
            proc = subprocess.run(["cargo", "fetch"], cwd=str(repo_path), capture_output=True, timeout=120)
            if proc.returncode == 0:
                _env_installed.add(key)
                return {"status": "installed", "method": "cargo fetch"}
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if (repo_path / "go.mod").exists():
        try:
            proc = subprocess.run(["go", "mod", "download"], cwd=str(repo_path), capture_output=True, timeout=120)
            if proc.returncode == 0:
                _env_installed.add(key)
                return {"status": "installed", "method": "go mod download"}
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return {
        "status": "failed",
        "message": "Could not install dependencies automatically.",
        "attempts": install_attempts[:5],
    }


# ── Discovery & Navigation Tools ─────────────────────────────────────────────

_IGNORED_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
                 ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
                 ".eggs", "*.egg-info"}


def tool_list_directory(args: dict, repo_path: Path, **_) -> dict:
    """List files and subdirectories at a path in compact tree format."""
    rel_path = args.get("path", "")
    depth = min(args.get("depth", 1), 3)
    pattern = args.get("pattern", "")

    target = repo_path / rel_path if rel_path else repo_path
    if not target.is_dir():
        return {"error": f"Not a directory: {rel_path or '.'}"}

    entries = []
    count = 0
    max_entries = 200

    def _walk(current: Path, current_depth: int, prefix: str = ""):
        nonlocal count
        if current_depth > depth or count >= max_entries:
            return

        try:
            items = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            return

        dirs = []
        files = []
        for item in items:
            if item.name in _IGNORED_DIRS or item.name.startswith("."):
                continue
            if any(item.match(pat) for pat in _IGNORED_DIRS if "*" in pat):
                continue

            if item.is_dir():
                dirs.append(item)
            elif item.is_file():
                if pattern:
                    if not item.match(pattern):
                        continue
                size = item.stat().st_size
                size_str = f"{size}" if size < 1024 else f"{size // 1024}K" if size < 1048576 else f"{size // 1048576}M"
                files.append(f"{prefix}{item.name} ({size_str})")
                count += 1

        for d in dirs:
            entries.append(f"{prefix}{d.name}/")
            count += 1
            if current_depth < depth:
                _walk(d, current_depth + 1, prefix + "  ")

        entries.extend(files)

    _walk(target, 1)

    truncated = count >= max_entries
    return {
        "path": rel_path or ".",
        "entries": entries,
        "total": count,
        "truncated": truncated,
    }


def tool_find_files(args: dict, repo_path: Path, **_) -> dict:
    """Find files by name pattern using glob."""
    pattern = args["pattern"]
    rel_path = args.get("path", "")
    max_results = args.get("max_results", 20)

    target = repo_path / rel_path if rel_path else repo_path
    if not target.is_dir():
        return {"error": f"Not a directory: {rel_path or '.'}"}

    # Use rglob for recursive patterns, glob for simple ones
    if "**" in pattern:
        matches = target.glob(pattern)
    else:
        matches = target.rglob(pattern)

    results = []
    for p in matches:
        # Skip ignored directories
        parts = p.relative_to(repo_path).parts
        if any(part in _IGNORED_DIRS for part in parts):
            continue
        if any(part.startswith(".") for part in parts):
            continue

        results.append(str(p.relative_to(repo_path)))
        if len(results) >= max_results:
            break

    return {"files": sorted(results), "total": len(results)}


# ── Reasoning Tool ───────────────────────────────────────────────────────────

def tool_think(args: dict, **_) -> dict:
    """Scratchpad for reasoning. No-op — value is in conversation history."""
    return {"acknowledged": True}


# ── Edit Safety Tools ────────────────────────────────────────────────────────

# Per-file edit history stack for undo. Max 5 entries per file.
_edit_history: dict[str, list[str]] = {}
# Workspace checkpoints: label → snapshot of modified_files
_checkpoints: dict[str, dict[str, str]] = {}


def tool_undo_edit(
    args: dict,
    modified_files: dict[str, str],
    original_files: dict[str, str],
    **_,
) -> dict:
    """Revert the last write_file edit to a specific file."""
    fp = args["file_path"]

    if fp not in _edit_history or not _edit_history[fp]:
        return {"error": f"No edit history for {fp}. Nothing to undo."}

    previous = _edit_history[fp].pop()
    modified_files[fp] = previous

    remaining = len(_edit_history.get(fp, []))
    return {
        "success": True,
        "file_path": fp,
        "message": f"Reverted to previous version. {remaining} more undo(s) available.",
    }


def tool_checkpoint(args: dict, modified_files: dict[str, str], **_) -> dict:
    """Save a snapshot of all current file changes."""
    label = args["label"]

    if len(_checkpoints) >= 3 and label not in _checkpoints:
        oldest = next(iter(_checkpoints))
        del _checkpoints[oldest]

    _checkpoints[label] = dict(modified_files)
    return {
        "success": True,
        "label": label,
        "files_snapshot": len(modified_files),
        "checkpoints_available": list(_checkpoints.keys()),
    }


def tool_restore(args: dict, modified_files: dict[str, str], **_) -> dict:
    """Restore all files to a previous checkpoint."""
    label = args["label"]

    if label not in _checkpoints:
        return {"error": f"Checkpoint '{label}' not found. Available: {list(_checkpoints.keys())}"}

    snapshot = _checkpoints[label]
    modified_files.clear()
    modified_files.update(snapshot)

    # Clear edit history since we're reverting
    _edit_history.clear()

    return {
        "success": True,
        "label": label,
        "files_restored": len(snapshot),
        "message": f"Restored to checkpoint '{label}'. Edit history cleared.",
    }


# ── File Analysis Tools ──────────────────────────────────────────────────────

def tool_file_outline(args: dict, repo_path: Path, modified_files: dict[str, str], **_) -> dict:
    """Get the structure of a file without function bodies."""
    import ast as _ast

    fp = args["file_path"]

    if ".." in fp or fp.startswith("/"):
        return {"error": f"Invalid path: {fp}"}

    # Read from virtual FS or disk
    if fp in modified_files:
        content = modified_files[fp]
    else:
        abs_path = repo_path / fp
        if not abs_path.exists():
            return {"error": f"File not found: {fp}"}
        content = abs_path.read_text(encoding="utf-8", errors="replace")

    if not fp.endswith(".py"):
        # Non-Python: regex-based outline
        return _regex_outline(fp, content)

    try:
        tree = _ast.parse(content)
    except SyntaxError as e:
        return {"error": f"Syntax error in {fp}: {e}", "outline": _regex_outline(fp, content).get("outline", "")}

    lines = content.splitlines()
    outline_parts = []

    # Imports
    imports = []
    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, _ast.Import):
            for alias in node.names:
                imports.append(f"  import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
        elif isinstance(node, _ast.ImportFrom):
            names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in node.names)
            imports.append(f"  from {node.module or '.'} import {names}")
    if imports:
        outline_parts.append("IMPORTS:")
        outline_parts.extend(imports[:20])
        if len(imports) > 20:
            outline_parts.append(f"  ... and {len(imports) - 20} more")

    # Top-level constants/assignments
    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, _ast.Assign):
            for target in node.targets:
                if isinstance(target, _ast.Name) and target.id.isupper():
                    outline_parts.append(f"\nL{node.lineno}: {target.id} = ...")

    # Classes and functions
    for node in _ast.iter_child_nodes(tree):
        if isinstance(node, (_ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
            _format_node(node, lines, outline_parts, indent=0)

    outline = "\n".join(outline_parts)
    return {"outline": outline, "total_lines": len(lines), "file_path": fp}


def _format_node(node, lines, parts, indent):
    """Format a class or function node for the outline."""
    import ast as _ast
    prefix = "  " * indent

    # Decorators
    for dec in node.decorator_list:
        parts.append(f"{prefix}L{dec.lineno}: @{_ast.unparse(dec)}")

    if isinstance(node, _ast.ClassDef):
        bases = ", ".join(_ast.unparse(b) for b in node.bases) if node.bases else ""
        parts.append(f"{prefix}L{node.lineno}: class {node.name}({bases}):")

        # Class docstring
        body_first = node.body[0] if node.body else None
        if isinstance(body_first, _ast.Expr) and isinstance(body_first.value, (_ast.Constant, _ast.Str)):
            doc = getattr(body_first.value, "value", getattr(body_first.value, "s", ""))
            if isinstance(doc, str):
                first_line = doc.strip().split("\n")[0][:80]
                parts.append(f"{prefix}  \"\"\"{first_line}\"\"\"")

        # Methods
        for child in node.body:
            if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                _format_node(child, lines, parts, indent + 1)

    elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
        async_prefix = "async " if isinstance(node, _ast.AsyncFunctionDef) else ""
        args_str = _ast.unparse(node.args) if node.args.args else ""
        # Truncate long signatures
        if len(args_str) > 80:
            args_str = args_str[:77] + "..."
        ret = f" -> {_ast.unparse(node.returns)}" if node.returns else ""
        end_line = node.end_lineno or node.lineno
        body_lines = end_line - node.lineno
        parts.append(f"{prefix}L{node.lineno}-{end_line}: {async_prefix}def {node.name}({args_str}){ret}: ({body_lines} lines)")

        # Function docstring
        body_first = node.body[0] if node.body else None
        if isinstance(body_first, _ast.Expr) and isinstance(body_first.value, (_ast.Constant, _ast.Str)):
            doc = getattr(body_first.value, "value", getattr(body_first.value, "s", ""))
            if isinstance(doc, str):
                first_line = doc.strip().split("\n")[0][:80]
                parts.append(f"{prefix}  \"\"\"{first_line}\"\"\"")


def _regex_outline(fp: str, content: str) -> dict:
    """Regex-based outline for non-Python files. Language-agnostic patterns."""
    lines = content.splitlines()
    outline_parts = []

    # Language-agnostic patterns — covers JS/TS, Rust, Go, Java, C#, Ruby, PHP, C/C++, Kotlin, Swift
    patterns = [
        # JS/TS: function, class, interface, type, enum, const/let/var declarations
        r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:function\s*\*?|class|interface|type|enum)\s+\w+",
        r"^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?\(",  # arrow fns
        # Rust: fn, struct, enum, trait, impl, mod, type
        r"^\s*(?:pub(?:\([\w:]+\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl|mod|type|const)\s+\w+",
        # Go: func, type, var, const, interface
        r"^\s*(?:func|type|var|const)\s+(?:\(.*\)\s+)?\w+",
        # Java/Kotlin/C#: class, interface, enum, record, abstract, public/private methods
        r"^\s*(?:public|private|protected|internal|static|abstract|final|override|open|sealed|data)?\s*(?:public|private|protected|internal|static|abstract|final)?\s*(?:class|interface|enum|record|struct|object)\s+\w+",
        r"^\s*(?:public|private|protected|internal|static|abstract|override|virtual|async)?\s*(?:public|private|protected|internal|static|abstract|override|virtual|async)?\s*[\w<>\[\]]+\s+\w+\s*\(",  # method sigs
        # Ruby: class, module, def
        r"^\s*(?:class|module|def)\s+\w+",
        # PHP: class, function, interface, trait
        r"^\s*(?:abstract\s+|final\s+)?(?:class|function|interface|trait|enum)\s+\w+",
        # C/C++: function definitions, class, struct, enum, namespace, typedef
        r"^\s*(?:static\s+|inline\s+|virtual\s+|extern\s+)?(?:class|struct|enum|namespace|typedef|union)\s+\w+",
        r"^(?:static\s+|inline\s+)?[\w:*&<>]+\s+[\w:]+\s*\([^;]*\)\s*\{?\s*$",  # C function def
        # Swift: class, struct, enum, protocol, func, extension
        r"^\s*(?:public\s+|private\s+|internal\s+|open\s+|fileprivate\s+)?(?:final\s+)?(?:class|struct|enum|protocol|func|extension)\s+\w+",
        # Import/include statements (all languages)
        r"^\s*(?:import|from|require|include|use|using|#include)\s+",
    ]

    compiled = [re.compile(p) for p in patterns]

    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if not stripped or stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
            continue
        for pat in compiled:
            if pat.match(line):
                outline_parts.append(f"L{i + 1}: {stripped}")
                break

    return {"outline": "\n".join(outline_parts[:100]), "total_lines": len(lines), "file_path": fp}


# ── Lint & Validation Tools ──────────────────────────────────────────────────

def tool_lint_check(
    args: dict,
    repo_path: Path,
    modified_files: dict[str, str],
    **_,
) -> dict:
    """Run linter on a single file and return structured errors."""
    import json as _json

    fp = args["file_path"]

    # Flush this file to disk first
    if fp in modified_files:
        abs_path = repo_path / fp
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(modified_files[fp], encoding="utf-8")

    abs_path = repo_path / fp

    # Detect linter by file extension
    ext = Path(fp).suffix.lower()
    linter_configs = {
        # Python
        ".py": [
            (["ruff", "check", str(abs_path), "--output-format=json"], "ruff", True),
            (["flake8", "--format=json", str(abs_path)], "flake8", True),
        ],
        # JavaScript / TypeScript
        ".js": [(["npx", "eslint", "--format=json", str(abs_path)], "eslint", True)],
        ".jsx": [(["npx", "eslint", "--format=json", str(abs_path)], "eslint", True)],
        ".ts": [(["npx", "eslint", "--format=json", str(abs_path)], "eslint", True)],
        ".tsx": [(["npx", "eslint", "--format=json", str(abs_path)], "eslint", True)],
        # Rust
        ".rs": [(["cargo", "clippy", "--message-format=json", "--", "-W", "clippy::all"], "clippy", False)],
        # Go
        ".go": [(["go", "vet", str(abs_path)], "go-vet", False)],
    }

    candidates = linter_configs.get(ext, [])

    for cmd, linter_name, parse_json in candidates:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=str(repo_path))
            if proc.returncode == 0:
                return {"status": "clean", "issues": [], "file_path": fp, "linter": linter_name}

            issues = []
            if parse_json:
                try:
                    raw = _json.loads(proc.stdout)
                    # Ruff format
                    if isinstance(raw, list) and raw and "location" in raw[0]:
                        issues = [{"line": i.get("location", {}).get("row", 0), "code": i.get("code", ""),
                                   "message": i.get("message", ""), "fixable": i.get("fix") is not None} for i in raw[:20]]
                    # ESLint format (array of file objects)
                    elif isinstance(raw, list) and raw and "messages" in raw[0]:
                        for msg in raw[0].get("messages", [])[:20]:
                            issues.append({"line": msg.get("line", 0), "code": msg.get("ruleId", ""),
                                          "message": msg.get("message", ""), "fixable": msg.get("fix") is not None})
                except _json.JSONDecodeError:
                    pass

            if not issues:
                issues = [{"message": line.strip()} for line in (proc.stdout + proc.stderr).splitlines()
                         if line.strip() and ("error" in line.lower() or "warning" in line.lower())][:15]

            return {"status": "issues_found", "issues": issues, "file_path": fp, "linter": linter_name}
        except FileNotFoundError:
            continue  # Try next linter
        except subprocess.TimeoutExpired:
            return {"status": "timeout", "file_path": fp, "linter": linter_name}

    # No linter available — fall back to syntax check for Python
    if ext == ".py":
        try:
            compile(modified_files.get(fp, abs_path.read_text(encoding="utf-8", errors="replace")), fp, "exec")
            return {"status": "clean", "issues": [], "file_path": fp, "note": "No linter installed, syntax check only"}
        except SyntaxError as e:
            return {"status": "issues_found", "issues": [{"line": e.lineno, "message": str(e)}], "file_path": fp}

    return {"status": "skipped", "file_path": fp, "note": f"No linter found for {ext} files"}


def tool_classify_test_result(args: dict, **_) -> dict:
    """Classify test output into structured categories with suggested actions."""
    output = args.get("test_output", "")
    exit_code = args.get("exit_code", 1)

    if exit_code == 0:
        return {"category": "all_pass", "suggested_action": "Tests passed. Proceed to finish."}

    output_lower = output.lower()

    # ── No tests found (all frameworks) ──────────────────────────────────
    no_test_signals = [
        "no tests ran", "collected 0 items",        # pytest
        "no test suites found", "no tests found",   # Jest/Mocha/JUnit
        "no test files matched", "0 passing",        # Jest/Mocha
        "ok  \t(cached)", "no test files",           # Go
        "test result: ok. 0 passed",                 # Rust
        "no tests to run",                           # general
    ]
    if exit_code == 5 or any(sig in output_lower for sig in no_test_signals):
        return {
            "category": "no_tests",
            "suggested_action": "No tests found. Check test file paths, naming conventions, and test discovery settings for your framework.",
            "details": _extract_error_context(output, ["no tests", "collected 0", "no test suites", "0 passing"]),
        }

    # ── Import / module resolution errors (all languages) ────────────────
    import_signals = [
        "modulenotfounderror", "importerror", "no module named",   # Python
        "cannot find module", "module not found", "err_module_not_found",  # Node/JS/TS
        "unresolved import", "could not resolve",                  # ESM/bundler
        "package .* is not in goroot",                             # Go
        "unresolved reference",                                     # Kotlin/Java
        "use of undeclared crate",                                  # Rust
    ]
    if any(sig in output_lower for sig in import_signals):
        missing = _extract_missing_modules(output)
        return {
            "category": "import_error",
            "missing_modules": missing,
            "suggested_action": f"Import/module resolution error. Missing: {', '.join(missing) if missing else 'unknown'}. Install dependencies or fix import paths.",
            "details": _extract_error_context(output, ["ModuleNotFoundError", "ImportError", "cannot find module", "module not found"]),
        }

    # ── Test infrastructure errors ───────────────────────────────────────
    infra_signals = [
        "conftest", "fixture",                                      # pytest
        "beforeall", "beforeeach", "setup failed",                  # Jest/Mocha/JUnit
        "test setup error", "initialization error",                 # general
        "configuration error", "config error",                      # general
        "cannot connect", "connection refused",                     # DB/service deps
    ]
    if any(sig in output_lower for sig in infra_signals) and "error" in output_lower:
        return {
            "category": "infra_error",
            "suggested_action": "Test infrastructure/setup error. Check test setup, fixtures, configuration, and external dependencies (databases, services).",
            "details": _extract_error_context(output, infra_signals[:6]),
        }

    # ── Timeout ──────────────────────────────────────────────────────────
    timeout_signals = ["timeout", "timedout", "timed out", "exceeded timeout", "deadline exceeded"]
    if any(sig in output_lower for sig in timeout_signals):
        return {
            "category": "timeout",
            "suggested_action": "Test timed out. Check for infinite loops, blocking I/O, deadlocks, or excessive computation in your changes.",
        }

    # ── Assertion failures (all frameworks) ──────────────────────────────
    assertion_signals = [
        "assertionerror", "assert ",                                # Python
        "expect(", "tobequal", "tobe(", "tohavebeencalled",        # Jest
        "expected", "to equal", "to be", "assert.equal",           # Mocha/Chai
        "assertion failed", "expected:.*but was:",                  # JUnit
        "assert_eq!", "assert_ne!", "panic", "thread.*panicked",   # Rust
        "got:.*want:", "expected.*got",                             # Go
    ]
    if any(sig in output_lower for sig in assertion_signals):
        failures = _extract_assertion_details(output)
        return {
            "category": "assertion_failure",
            "failures": failures[:5],
            "suggested_action": "Assertion failures — your code logic produces wrong output. Read expected vs actual values and fix the logic.",
        }

    # ── Runtime errors (all languages) ───────────────────────────────────
    runtime_signals = [
        "typeerror", "valueerror", "attributeerror", "nameerror", "keyerror", "indexerror",  # Python
        "referenceerror", "rangeerror", "syntaxerror",             # JS
        "nullpointerexception", "classcastexception", "illegalargumentexception",  # Java
        "nil pointer dereference", "index out of range",           # Go
        "thread.*panicked.*index out of bounds",                   # Rust
        "segmentation fault", "stack overflow",                    # C/C++
    ]
    if any(sig in output_lower for sig in runtime_signals):
        return {
            "category": "runtime_error",
            "suggested_action": "Runtime error — your code crashes. Read the stack trace to find which line and fix it.",
            "details": _extract_error_context(output, ["Error", "Exception", "panic", "fault"]),
        }

    # ── Compilation / type errors ────────────────────────────────────────
    compile_signals = [
        "compilation failed", "build failed", "compile error",
        "type error", "ts\\(\\d+\\)", "tsc",                      # TypeScript
        "cannot find symbol", "incompatible types",                # Java
        "does not implement", "type mismatch",                     # Go/Rust
    ]
    if any(re.search(sig, output_lower) for sig in compile_signals):
        return {
            "category": "compile_error",
            "suggested_action": "Compilation or type error. Fix the type mismatch or missing symbol in your code.",
            "details": _extract_error_context(output, ["error", "cannot find", "incompatible", "mismatch"]),
        }

    return {
        "category": "unknown",
        "suggested_action": "Could not classify the failure. Read the test output carefully and fix based on the error messages.",
        "output_tail": output[-1000:],
    }


def _extract_missing_modules(output: str) -> list[str]:
    """Extract module/package names from import errors across all languages."""
    modules = []
    patterns = [
        r"No module named ['\"]?([a-zA-Z0-9_./-]+)",                # Python
        r"(?:cannot import|ImportError:.*) ['\"]?([a-zA-Z0-9_.]+)",  # Python
        r"Cannot find module ['\"]([^'\"]+)['\"]",                    # Node/JS
        r"Module not found:.*['\"]([^'\"]+)['\"]",                    # Webpack/bundler
        r"ERR_MODULE_NOT_FOUND.*['\"]([^'\"]+)['\"]",                 # Node ESM
        r"package ([a-zA-Z0-9_./-]+) is not in",                      # Go
        r"use of undeclared crate or module `([^`]+)`",               # Rust
        r"Unresolved reference: (\w+)",                                # Kotlin/Java
    ]
    for line in output.splitlines():
        for pat in patterns:
            match = re.search(pat, line)
            if match:
                modules.append(match.group(1))
                break
    return list(dict.fromkeys(modules))[:10]  # dedupe, max 10


def _extract_error_context(output: str, keywords: list[str]) -> str:
    """Extract lines around the first matching keyword."""
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if any(kw.lower() in line.lower() for kw in keywords):
            start = max(0, i - 2)
            end = min(len(lines), i + 5)
            return "\n".join(lines[start:end])
    return ""


def _extract_assertion_details(output: str) -> list[dict]:
    """Extract assertion failure details from test output."""
    failures = []
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if "FAILED" in line and "::" in line:
            test_name = line.split("FAILED")[-1].strip().strip("-").strip()
            # Look for assertion context nearby
            context = "\n".join(lines[max(0, i - 5):min(len(lines), i + 3)])
            failures.append({"test": test_name, "context": context[:300]})
    return failures


# ── Git History Tools ────────────────────────────────────────────────────────

def tool_git_log(args: dict, repo_path: Path, **_) -> dict:
    """Get recent git commits for a file or the whole repo."""
    fp = args.get("file_path", "")
    max_commits = min(args.get("max_commits", 10), 25)

    cmd = ["git", "log", f"--max-count={max_commits}",
           "--format=%h %ad %an: %s", "--date=short"]
    if fp:
        cmd.extend(["--follow", "--", fp])

    try:
        proc = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return {"error": f"git log failed: {proc.stderr[:200]}"}

        commits = []
        for line in proc.stdout.strip().splitlines():
            parts = line.split(" ", 3)
            if len(parts) >= 4:
                commits.append({"hash": parts[0], "date": parts[1], "author": parts[2].rstrip(":"), "message": parts[3]})
            else:
                commits.append({"raw": line})

        return {"commits": commits, "total": len(commits)}
    except subprocess.TimeoutExpired:
        return {"error": "git log timed out"}
    except Exception as e:
        return {"error": str(e)}


def tool_git_blame(args: dict, repo_path: Path, **_) -> dict:
    """Show who last modified each line in a file range."""
    fp = args["file_path"]
    start = args.get("start_line", 1)
    end = args.get("end_line", start + 20)
    end = min(end, start + 50)  # Cap at 50 lines

    cmd = ["git", "blame", f"-L{start},{end}", "--porcelain", fp]
    try:
        proc = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=10)
        if proc.returncode != 0:
            return {"error": f"git blame failed: {proc.stderr[:200]}"}

        # Parse porcelain output
        blame_entries = []
        current = {}
        for line in proc.stdout.splitlines():
            if line.startswith("\t"):
                current["code"] = line[1:][:120]
                blame_entries.append(current)
                current = {}
            elif line.startswith("author "):
                current["author"] = line[7:]
            elif line.startswith("summary "):
                current["message"] = line[8:][:100]
            elif re.match(r"^[0-9a-f]{40} ", line):
                parts = line.split()
                current["commit"] = parts[0][:8]
                if len(parts) >= 3:
                    current["line"] = int(parts[2])

        return {"blame": blame_entries[:50], "file_path": fp}
    except subprocess.TimeoutExpired:
        return {"error": "git blame timed out"}
    except Exception as e:
        return {"error": str(e)}


# ── Batch & Review Tools ─────────────────────────────────────────────────────

def tool_batch_read(
    args: dict,
    repo_path: Path,
    modified_files: dict[str, str],
    **_,
) -> dict:
    """Read multiple files in a single call."""
    files = args.get("files", [])
    if not files:
        return {"error": "No files specified"}
    if len(files) > 5:
        files = files[:5]

    results = {}
    total_chars = 0
    max_total = 30_000

    for entry in files:
        fp = entry["file_path"]
        read_args = {"file_path": fp}
        if "start_line" in entry:
            read_args["start_line"] = entry["start_line"]
        if "end_line" in entry:
            read_args["end_line"] = entry["end_line"]

        result = tool_read_file(read_args, repo_path, modified_files)

        content = result.get("content", "")
        if total_chars + len(content) > max_total:
            remaining = max_total - total_chars
            result["content"] = content[:remaining] + "\n... [truncated — batch_read 30K limit]"
            result["truncated"] = True
            results[fp] = result
            break

        total_chars += len(content)
        results[fp] = result

    return {"files": results, "total_files": len(results), "total_chars": total_chars}


def tool_self_review(
    args: dict,
    modified_files: dict[str, str],
    original_files: dict[str, str],
    **_,
) -> dict:
    """Review own changes before finishing. Returns annotated diff with checklist."""
    diff_result = tool_get_diff(args, modified_files=modified_files, original_files=original_files)
    diff = diff_result.get("diff", "")
    files_changed = diff_result.get("files_changed", 0)

    if not diff:
        return {"review": "No changes to review.", "files_changed": 0}

    # Detect potential issues — language-agnostic debug code patterns
    debug_patterns = {
        "print(": [".py"],
        "console.log": [".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"],
        "console.debug": [".js", ".jsx", ".ts", ".tsx"],
        "debugger": [".js", ".jsx", ".ts", ".tsx"],
        "fmt.Println": [".go"],
        "println!": [".rs"],
        "System.out.println": [".java", ".kt"],
        "var_dump(": [".php"],
        "dd(": [".php"],
        "puts ": [".rb"],
        "p ": [".rb"],
        "NSLog(": [".swift", ".m"],
        "printf(": [".c", ".cpp", ".h"],
        "std::cout": [".cpp", ".cc", ".cxx"],
    }
    incomplete_markers = ["TODO", "FIXME", "HACK", "XXX", "TEMP", "DELETEME"]

    warnings = []
    for fp in modified_files:
        content = modified_files[fp]
        original = original_files.get(fp, "")
        ext = Path(fp).suffix.lower()

        # Check debug code
        for pattern, extensions in debug_patterns.items():
            if ext in extensions and pattern in content and pattern not in original:
                warnings.append(f"DEBUG: {fp} contains new '{pattern}' — remove before submitting")

        # Check incomplete markers
        for marker in incomplete_markers:
            if marker in content and marker not in original:
                warnings.append(f"INCOMPLETE: {fp} contains new {marker} marker")

    checklist = (
        "SELF-REVIEW CHECKLIST:\n"
        "[ ] Every edit addresses the ticket requirement\n"
        "[ ] No unintended side effects or deleted functionality\n"
        "[ ] No debug/logging code left behind\n"
        "[ ] All new imports/includes exist and are correct\n"
        "[ ] Test files were NOT modified (unless the ticket asks for it)\n"
        "[ ] Variable/function names follow existing conventions\n"
    )

    return {
        "diff": diff,
        "files_changed": files_changed,
        "warnings": warnings,
        "checklist": checklist,
    }


# ── Memoization Cache ────────────────────────────────────────────────────────

# Keyed by (tool_name, frozen_args). Cleared for a file when write_file modifies it.
_result_cache: dict[tuple, dict] = {}

# Tools whose results are safe to cache (idempotent, read-only)
_CACHEABLE_TOOLS = {
    "read_file", "search_symbols", "get_dependencies", "get_test_coverage",
    "get_coupled_files", "get_callers", "get_impact", "get_risk_score",
    "get_reviewers", "search_code",
    # New graph tools (read-only)
    "get_top_files", "get_file_info", "get_symbol_details", "get_class_hierarchy",
    "get_change_context",
    # New standard read-only tools
    "list_directory", "find_files", "file_outline", "git_log", "git_blame",
}


def _make_cache_key(name: str, args: dict) -> tuple:
    """Create a hashable cache key from tool name and args."""
    frozen = tuple(sorted((k, str(v)) for k, v in args.items()))
    return (name, frozen)


def _invalidate_cache_for_file(file_path: str) -> None:
    """Remove cached results that reference a modified file."""
    to_remove = []
    for key in _result_cache:
        tool_name, frozen_args = key
        args_dict = dict(frozen_args)
        if args_dict.get("file_path") == file_path:
            to_remove.append(key)
    for key in to_remove:
        del _result_cache[key]


def clear_tool_cache() -> None:
    """Clear entire memoization cache + edit state (call between pipeline runs)."""
    _result_cache.clear()
    _edit_history.clear()
    _checkpoints.clear()


# ── Dispatcher ───────────────────────────────────────────────────────────────

_TOOL_MAP = {
    # Graph tools
    "get_callers": tool_get_callers,
    "get_impact": tool_get_impact,
    "get_dependencies": tool_get_dependencies,
    "get_test_coverage": tool_get_test_coverage,
    "get_coupled_files": tool_get_coupled_files,
    "search_symbols": tool_search_symbols,
    "get_risk_score": tool_get_risk_score,
    "get_reviewers": tool_get_reviewers,
    # Graph tools (new)
    "get_top_files": tool_get_top_files,
    "get_file_info": tool_get_file_info,
    "get_symbol_details": tool_get_symbol_details,
    "get_class_hierarchy": tool_get_class_hierarchy,
    "get_change_context": tool_get_change_context,
    # Standard tools
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "search_code": tool_search_code,
    "run_tests": tool_run_tests,
    "run_command": tool_run_command,
    "build_check": tool_build_check,
    "get_diff": tool_get_diff,
    "finish": tool_finish,
    "setup_environment": tool_setup_environment,
    # Discovery & navigation
    "list_directory": tool_list_directory,
    "find_files": tool_find_files,
    # Reasoning
    "think": tool_think,
    # Edit safety
    "undo_edit": tool_undo_edit,
    "checkpoint": tool_checkpoint,
    "restore": tool_restore,
    # File analysis
    "file_outline": tool_file_outline,
    # Lint & validation
    "lint_check": tool_lint_check,
    "classify_test_result": tool_classify_test_result,
    # Git history
    "git_log": tool_git_log,
    "git_blame": tool_git_blame,
    # Batch & review
    "batch_read": tool_batch_read,
    "self_review": tool_self_review,
}


# Tools that should execute inside the Docker sandbox when available
_SANDBOX_TOOLS = {"run_tests", "run_command", "build_check", "setup_environment"}


def _sandbox_run_tests(sandbox, args: dict, repo_path: Path, modified_files: dict[str, str]) -> dict:
    """Run tests inside the Docker sandbox with auto-fix for missing deps."""
    from layer45_agent.sandbox import classify_test_error

    sandbox.sync_files(modified_files)

    test_paths = args.get("test_paths", [])
    include_lint = args.get("include_lint", True)

    if not test_paths:
        changed = list(modified_files.keys())
        from layer6_validator.runner import _find_test_files
        test_paths = _find_test_files(repo_path, changed)

    paths_str = " ".join(test_paths) if test_paths else "."

    # Use the detected project profile's test command (if available).
    # This is the KEY fix: Django uses "python tests/runtests.py {module}",
    # not "python -m pytest". The profile was detected + verified at sandbox start.
    profile = sandbox.profile if hasattr(sandbox, "profile") and sandbox.profile else {}
    profile_cmd = profile.get("test_command", "")
    env_prefix = " ".join(f"{k}={v}" for k, v in profile.get("env_vars", {}).items())

    if profile_cmd and "{module}" in profile_cmd:
        # Use profile's test command with the test paths
        base_cmd = profile_cmd.replace("{module}", paths_str)
        if env_prefix:
            base_cmd = f"{env_prefix} {base_cmd}"
    elif profile_cmd and "{paths}" in profile_cmd:
        base_cmd = profile_cmd.replace("{paths}", paths_str)
        if env_prefix:
            base_cmd = f"{env_prefix} {base_cmd}"
    else:
        # Fallback: plain pytest (no --timeout to avoid crashes)
        base_cmd = f"python -m pytest {paths_str} -v --tb=short --no-header"

    # Run tests — up to 2 auto-fix attempts if infra error detected
    max_fix_attempts = 2
    for attempt in range(max_fix_attempts + 1):
        # On retry after pytest config error, add -W ignore to bypass warning filters
        cmd = base_cmd
        if attempt > 0:
            cmd = base_cmd + " -W ignore::DeprecationWarning -W ignore::UserWarning -p no:warnings"
        cmd += " 2>&1"

        result = sandbox.exec(cmd, timeout=150)
        output = result["stdout"] + result["stderr"]

        # Classify the error
        error_info = classify_test_error(output)

        if not error_info["is_infra"] or result["exit_code"] == 0:
            break  # Real test result (pass or fail), not infra problem

        if attempt < max_fix_attempts:
            # Try to fix the environment
            log.info("sandbox.auto_fix_env", attempt=attempt + 1, error_type=error_info["error_type"])
            fix_result = sandbox.fix_test_environment(output)
            if not fix_result.get("fixed"):
                # If targeted fix didn't work and it's a config error,
                # next iteration will try with -W ignore flags
                if error_info["error_type"] != "pytest_config_error":
                    break  # Can't fix, return what we have
            else:
                log.info("sandbox.auto_fix_env.installed", packages=fix_result.get("installed", []))

    # Count pass/fail from final output
    passed = output.count(" PASSED")
    failed = output.count(" FAILED")
    errors = output.count(" ERROR")

    test_result = {
        "test_status": "passed" if result["exit_code"] == 0 else "failed",
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "output": output[-8000:],
        "failures": _parse_test_failures(output),
    }

    if error_info["is_infra"] and result["exit_code"] != 0:
        test_result["infrastructure_error"] = True
        test_result["error_type"] = error_info["error_type"]
        missing = error_info.get("missing_modules", [])
        if missing:
            test_result["missing_modules"] = missing
        test_result["warning"] = (
            f"Tests failed due to {error_info['error_type']}. "
            f"Missing: {', '.join(missing) if missing else 'unknown'}. "
            "Call setup_environment or use run_command to install missing deps."
        )

    if include_lint:
        lint_paths = [fp for fp in modified_files.keys() if fp.endswith(".py")]
        if lint_paths:
            lint_cmd = f"python -m ruff check {' '.join(lint_paths)} --output-format=json 2>/dev/null || true"
            lint_result = sandbox.exec(lint_cmd, timeout=30)
            test_result["lint_status"] = "passed" if lint_result["exit_code"] == 0 else "failed"
            test_result["lint_issues"] = []
        else:
            test_result["lint_status"] = "skipped"
            test_result["lint_issues"] = []

    return test_result


def _sandbox_run_command(sandbox, args: dict, modified_files: dict[str, str]) -> dict:
    """Execute a shell command inside the Docker sandbox."""
    command = args.get("command", "")
    if not command:
        return {"error": "No command provided"}

    dangerous = ["rm -rf /", "mkfs", "dd if=", "> /dev/", ":(){ :|:& };:"]
    for d in dangerous:
        if d in command:
            return {"error": f"Blocked dangerous command: {d}"}

    sandbox.sync_files(modified_files)
    result = sandbox.exec(command, timeout=30)

    stdout = result["stdout"][-3000:] if len(result["stdout"]) > 3000 else result["stdout"]
    stderr = result["stderr"][-2000:] if len(result["stderr"]) > 2000 else result["stderr"]
    return {"exit_code": result["exit_code"], "stdout": stdout, "stderr": stderr}


def _sandbox_build_check(sandbox, args: dict, repo_path: Path, modified_files: dict[str, str]) -> dict:
    """Run build check inside the Docker sandbox."""
    sandbox.sync_files(modified_files)

    # TypeScript
    if (repo_path / "tsconfig.json").exists():
        result = sandbox.exec("npx tsc --noEmit 2>&1", timeout=60)
        if result["exit_code"] == 0:
            return {"status": "success", "command": "tsc --noEmit"}
        output = result["stdout"] + result["stderr"]
        errors = [l.strip() for l in output.splitlines() if "error" in l.lower()]
        return {"status": "failed", "command": "tsc --noEmit", "errors": errors[:5], "output": output[-1500:]}

    # Node.js build
    if (repo_path / "package.json").exists():
        pm = "npm"
        for lockfile, mgr in [("bun.lockb", "bun"), ("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm")]:
            if (repo_path / lockfile).exists():
                pm = mgr
                break
        result = sandbox.exec(f"{pm} run build 2>&1", timeout=60)
        if result["exit_code"] == 0:
            return {"status": "success", "command": f"{pm} run build"}
        output = result["stdout"] + result["stderr"]
        errors = [l.strip() for l in output.splitlines() if "error" in l.lower()]
        return {"status": "failed", "command": f"{pm} run build", "errors": errors[:5], "output": output[-1500:]}

    # Rust
    if (repo_path / "Cargo.toml").exists():
        result = sandbox.exec("cargo build --message-format=short 2>&1", timeout=120)
        if result["exit_code"] == 0:
            return {"status": "success", "command": "cargo build"}
        return {"status": "failed", "command": "cargo build", "output": (result["stdout"] + result["stderr"])[-1500:]}

    # Go
    if (repo_path / "go.mod").exists():
        result = sandbox.exec("go build ./... 2>&1", timeout=60)
        if result["exit_code"] == 0:
            return {"status": "success", "command": "go build"}
        return {"status": "failed", "command": "go build", "output": (result["stdout"] + result["stderr"])[-1500:]}

    return {"status": "skipped", "message": "No build system detected"}


def _sandbox_setup_environment(sandbox) -> dict:
    """Install project deps inside the Docker sandbox."""
    return sandbox.setup_environment()


def execute_tool(
    name: str,
    args: dict,
    repo_path: Path,
    repo_id: str,
    modified_files: dict[str, str],
    original_files: dict[str, str],
    sandbox=None,
) -> dict:
    """Execute a tool by name. Routes execution tools through sandbox when available."""
    fn = _TOOL_MAP.get(name)
    if not fn:
        return {"error": f"Unknown tool: {name}"}

    # Memoization: return cached result for idempotent tools
    if name in _CACHEABLE_TOOLS:
        file_arg = args.get("file_path", "")
        skip_cache = (name == "read_file" and file_arg in modified_files)
        if not skip_cache:
            cache_key = _make_cache_key(name, args)
            if cache_key in _result_cache:
                log.debug("tool.cache_hit", tool=name)
                return _result_cache[cache_key]

    try:
        # Route execution tools through Docker sandbox when available
        if sandbox and name in _SANDBOX_TOOLS:
            if name == "run_tests":
                result = _sandbox_run_tests(sandbox, args, repo_path, modified_files)
            elif name == "run_command":
                result = _sandbox_run_command(sandbox, args, modified_files)
            elif name == "build_check":
                result = _sandbox_build_check(sandbox, args, repo_path, modified_files)
            elif name == "setup_environment":
                result = _sandbox_setup_environment(sandbox)
            else:
                result = fn(args=args, repo_path=repo_path, repo_id=repo_id,
                            modified_files=modified_files, original_files=original_files)
        else:
            result = fn(
                args=args,
                repo_path=repo_path,
                repo_id=repo_id,
                modified_files=modified_files,
                original_files=original_files,
            )

        # Cache the result if cacheable
        if name in _CACHEABLE_TOOLS:
            file_arg = args.get("file_path", "")
            skip_cache = (name == "read_file" and file_arg in modified_files)
            if not skip_cache:
                cache_key = _make_cache_key(name, args)
                _result_cache[cache_key] = result

        # Invalidate cache when a file is written
        if name == "write_file" and result.get("success"):
            _invalidate_cache_for_file(args.get("file_path", ""))

        return result
    except Exception as e:
        log.error("tool.error", tool=name, error=str(e))
        return {"error": f"Tool '{name}' failed: {e}"}
