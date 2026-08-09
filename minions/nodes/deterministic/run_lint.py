"""[D] run_lint — Run linters with auto-fix. Routes through sandbox when available. Zero LLM tokens."""
from __future__ import annotations

import subprocess
import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def _split_files_by_change_type(ctx: PipelineContext) -> tuple[list[str], list[str]]:
    """Split changed files into (created, modified).

    Prettier/formatters should only run on NEW files the agent created.
    For MODIFIED files (search-replace edits), only run linters (error check)
    not formatters — to avoid reformatting the entire existing file.
    """
    created = []
    modified = []
    for fr in ctx.implementation.file_results:
        if fr.change_type == "create":
            created.append(fr.file_path)
        else:
            modified.append(fr.file_path)
    return created, modified


def execute(ctx: PipelineContext) -> NodeResult:
    if not ctx.repo_path or not ctx.implementation:
        return NodeResult(success=True, tokens_used=0)

    ctx.implementation.write_to_disk(str(ctx.repo_path))

    created_files, modified_files = _split_files_by_change_type(ctx)
    all_files = created_files + modified_files

    py_files = [f for f in all_files if f.endswith(".py")]
    # Only format NEW ts/js files — don't reformat existing files with prettier
    ts_files_to_format = [f for f in created_files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]
    # Lint-check ALL ts/js files (created + modified)
    ts_files_to_lint = [f for f in all_files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]

    errors = []
    autofixed = []

    if ctx.sandbox:
        ctx.sandbox.sync_files(ctx.modified_files)
        errors, autofixed = _lint_in_sandbox(ctx, py_files, ts_files_to_format, ts_files_to_lint)
    else:
        errors, autofixed = _lint_on_host(ctx, py_files, ts_files_to_format, ts_files_to_lint)

    ctx.lint_output = "\n".join(errors) if errors else "clean"
    ctx.lint_errors = errors
    ctx.lint_autofixed = autofixed

    # Re-read files from disk after auto-fix (only for files that were actually formatted)
    if autofixed and ctx.implementation:
        for fr in ctx.implementation.file_results:
            full_path = ctx.repo_path / fr.file_path
            if full_path.exists():
                new_content = full_path.read_text(encoding="utf-8", errors="replace")
                if new_content != fr.modified_content:
                    fr.modified_content = new_content
                    ctx.modified_files[fr.file_path] = new_content

    log.info("lint.done", errors=len(errors), autofixed=autofixed,
             created=len(created_files), modified=len(modified_files),
             sandbox=bool(ctx.sandbox))
    # Always success=True — run_lint "succeeded" at linting. lint_gate decides what to do.
    return NodeResult(success=True, tokens_used=0)


def _lint_in_sandbox(ctx, py_files, ts_format, ts_lint) -> tuple[list[str], list[str]]:
    """Run linters inside Docker sandbox."""
    errors = []
    autofixed = []

    if py_files:
        files_str = " ".join(py_files)
        ctx.sandbox.exec(f"python -m ruff check --fix --quiet {files_str} 2>/dev/null", timeout=30)
        autofixed.append("ruff --fix applied (sandbox)")
        result = ctx.sandbox.exec(
            f"python -m ruff check --output-format=concise {files_str} 2>/dev/null",
            timeout=30,
        )
        if result["exit_code"] != 0 and result["stdout"].strip():
            for line in result["stdout"].strip().splitlines():
                errors.append(line.strip())

    # Format ONLY new files
    if ts_format:
        files_str = " ".join(ts_format)
        ctx.sandbox.exec(f"npx prettier --write {files_str} 2>/dev/null", timeout=30)
        autofixed.append(f"prettier applied to {len(ts_format)} new file(s) (sandbox)")

    # Lint-check ALL ts/js files (but don't --write/--fix format existing ones)
    if ts_lint:
        files_str = " ".join(ts_lint)
        result = ctx.sandbox.exec(f"npx eslint {files_str} 2>/dev/null", timeout=30)
        if result["exit_code"] != 0 and result["stdout"].strip():
            for line in result["stdout"].strip().splitlines()[:20]:
                errors.append(line.strip())

    return errors, autofixed


def _lint_on_host(ctx, py_files, ts_format, ts_lint) -> tuple[list[str], list[str]]:
    """Run linters directly on host."""
    errors = []
    autofixed = []

    if py_files:
        try:
            subprocess.run(
                ["ruff", "check", "--fix", "--quiet"] + py_files,
                cwd=str(ctx.repo_path), capture_output=True, text=True, timeout=30,
            )
            autofixed.append("ruff --fix applied")

            check_result = subprocess.run(
                ["ruff", "check", "--output-format=concise"] + py_files,
                cwd=str(ctx.repo_path), capture_output=True, text=True, timeout=30,
            )
            if check_result.returncode != 0 and check_result.stdout.strip():
                for line in check_result.stdout.strip().splitlines():
                    errors.append(line.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Format ONLY new files — don't reformat existing files
    if ts_format:
        try:
            subprocess.run(
                ["npx", "prettier", "--write"] + ts_format,
                cwd=str(ctx.repo_path), capture_output=True, timeout=30,
            )
            autofixed.append(f"prettier applied to {len(ts_format)} new file(s)")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Lint-check ALL ts/js files (read-only, no --fix to avoid reformatting)
    if ts_lint:
        try:
            result = subprocess.run(
                ["npx", "eslint"] + ts_lint,
                cwd=str(ctx.repo_path), capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0 and result.stdout.strip():
                for line in result.stdout.strip().splitlines()[:20]:
                    errors.append(line.strip())
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return errors, autofixed
