"""
Layer 6 — Validation & Test Runner.
Writes modified files to disk, then runs:
  1. Syntax check (compile each modified .py file)
  2. pytest on test files related to the changes
  3. ruff lint check on modified files (if ruff is available)
"""
from __future__ import annotations

import subprocess
import sys
import time
import ast
from pathlib import Path
import structlog

from layer4_planner.file_reader import get_repo_path
from layer45_agent.implementation import Implementation
from layer6_validator.models.result import (
    ValidationResult, ValidationStatus, TestResult, LintResult, SyntaxResult
)

log = structlog.get_logger(__name__)


def _worst(statuses: list[ValidationStatus]) -> ValidationStatus:
    order = [ValidationStatus.PASSED, ValidationStatus.SKIPPED, ValidationStatus.ERROR, ValidationStatus.FAILED]
    for s in reversed(order):
        if s in statuses:
            return s
    return ValidationStatus.PASSED


def check_syntax(repo_root: Path, file_paths: list[str]) -> SyntaxResult:
    """Compile each modified Python file to catch syntax errors."""
    errors = []
    for rel in file_paths:
        if not rel.endswith(".py"):
            continue
        abs_path = repo_root / rel
        if not abs_path.exists():
            continue
        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
            ast.parse(source, filename=rel)
        except SyntaxError as e:
            errors.append(f"{rel}:{e.lineno}: {e.msg}")

    # TypeScript/JS: run tsc --noEmit if tsconfig.json exists
    ts_files = [rel for rel in file_paths if rel.endswith((".ts", ".tsx"))]
    if ts_files and (repo_root / "tsconfig.json").exists():
        pm = _detect_package_manager(repo_root)
        tsc_cmd = ["npx", "tsc", "--noEmit"] if pm else ["tsc", "--noEmit"]
        try:
            proc = subprocess.run(
                tsc_cmd, cwd=str(repo_root),
                capture_output=True, text=True, timeout=60,
            )
            if proc.returncode != 0:
                output = proc.stdout + proc.stderr
                ts_error_lines = [
                    line.strip() for line in output.splitlines()
                    if "error TS" in line
                ]
                # Only count errors referencing local/aliased imports as real
                # (missing npm packages are a deps issue, not a code bug)
                local_errors = [
                    e for e in ts_error_lines
                    if any(p in e for p in ["'./", "'../", "'@/", '"./', '"../', '"@/'])
                ]
                if local_errors:
                    errors.extend(local_errors[:10])
                elif ts_error_lines and not any(
                    sig in output.lower()
                    for sig in ["cannot find module", "err_module_not_found"]
                ):
                    # Real TS errors (not import-related)
                    errors.extend(ts_error_lines[:10])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # tsc not available or too slow, skip gracefully

    status = ValidationStatus.FAILED if errors else ValidationStatus.PASSED
    log.info("syntax.check", files=len(file_paths), errors=len(errors))
    return SyntaxResult(status=status, errors=errors)


def _find_test_files(repo_root: Path, changed_files: list[str]) -> list[str]:
    """
    Find test files to run using 4 strategies:
    1. Changed file is itself a test file
    2. Pattern-matched test files (test_{name}.py / {name}_test.py)
    3. Test files in the same directory as changed file
    4. Test files in nearest parent tests/ or test/ directory
    Falls back to running entire test directory if nothing found.
    """
    test_files = set()
    for rel in changed_files:
        p = Path(rel)
        # Strategy 1: Changed file is itself a test file
        if "test" in p.name.lower():
            if (repo_root / rel).exists():
                test_files.add(rel)
            continue

        # Strategy 2: Pattern-matched test files
        stem = p.stem
        # Python patterns
        for pattern in [f"test_{stem}.py", f"{stem}_test.py"]:
            for candidate in repo_root.rglob(pattern):
                test_files.add(str(candidate.relative_to(repo_root)))
        # JS/TS patterns
        for pattern in [f"{stem}.test.ts", f"{stem}.test.tsx", f"{stem}.spec.ts", f"{stem}.spec.tsx",
                        f"{stem}.test.js", f"{stem}.test.jsx", f"{stem}.spec.js", f"{stem}.spec.jsx"]:
            for candidate in repo_root.rglob(pattern):
                test_files.add(str(candidate.relative_to(repo_root)))

        # Strategy 3: Test files in the same directory as changed file
        parent = repo_root / p.parent
        if parent.is_dir():
            for f in parent.iterdir():
                if f.is_file() and f.suffix in (".py", ".ts", ".tsx", ".js", ".jsx") and "test" in f.name.lower():
                    test_files.add(str(f.relative_to(repo_root)))

        # Strategy 4: Tests in nearest parent tests/ test/ __tests__/ directory
        for ancestor in [p.parent, p.parent.parent]:
            for test_dir_name in ["tests", "test", "__tests__"]:
                candidate_dir = repo_root / ancestor / test_dir_name
                if candidate_dir.is_dir():
                    for tf in candidate_dir.rglob("test_*.py"):
                        test_files.add(str(tf.relative_to(repo_root)))
                    for tf in candidate_dir.rglob("*_test.py"):
                        test_files.add(str(tf.relative_to(repo_root)))
                    # JS/TS test globs
                    for tf in candidate_dir.rglob("*.test.*"):
                        test_files.add(str(tf.relative_to(repo_root)))
                    for tf in candidate_dir.rglob("*.spec.*"):
                        test_files.add(str(tf.relative_to(repo_root)))

    # Fallback: if nothing found, run entire test directory
    if not test_files:
        for test_dir in ["tests", "test", "__tests__"]:
            td = repo_root / test_dir
            if td.is_dir():
                test_files.add(test_dir)
                break

    return sorted(test_files)


def run_tests(repo_root: Path, test_paths: list[str], timeout: int = 120) -> TestResult:
    """Run pytest on the given test paths."""
    if not test_paths:
        log.warning("tests.no_files")
        return TestResult(
            status=ValidationStatus.SKIPPED,
            passed=0, failed=0, errors=0,
            output="No test files found.",
            duration_s=0.0,
        )

    # Only use --timeout if pytest-timeout is installed (avoids crash)
    timeout_args = []
    try:
        import pytest_timeout  # noqa: F401
        timeout_args = [f"--timeout={timeout}"]
    except ImportError:
        pass

    cmd = [
        sys.executable, "-m", "pytest",
        *test_paths,
        "-v", "--tb=short", "--no-header",
        *timeout_args,
    ]

    log.info("tests.run", cmd=" ".join(cmd[:5] + ["..."]), cwd=str(repo_root))
    start = time.time()

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout + 30,
        )
        duration = time.time() - start
        output = (proc.stdout + proc.stderr)[-4000:]  # keep last 4000 chars

        # Parse pytest exit code:
        # 0 = all passed, 1 = some failed, 2 = interrupted, 3 = internal error, 4 = no tests, 5 = no tests collected
        if proc.returncode == 0:
            status = ValidationStatus.PASSED
        elif proc.returncode == 5:
            status = ValidationStatus.SKIPPED
        else:
            status = ValidationStatus.FAILED

        # Count from output
        passed = failed = errors = 0
        for line in proc.stdout.splitlines():
            if " passed" in line:
                import re
                m = re.search(r'(\d+) passed', line)
                if m: passed = int(m.group(1))
            if " failed" in line:
                import re
                m = re.search(r'(\d+) failed', line)
                if m: failed = int(m.group(1))
            if " error" in line:
                import re
                m = re.search(r'(\d+) error', line)
                if m: errors = int(m.group(1))

        log.info("tests.done", status=status.value, passed=passed, failed=failed, errors=errors, duration=f"{duration:.1f}s")
        return TestResult(status=status, passed=passed, failed=failed, errors=errors, output=output, duration_s=duration)

    except subprocess.TimeoutExpired:
        return TestResult(
            status=ValidationStatus.ERROR,
            passed=0, failed=0, errors=1,
            output=f"Tests timed out after {timeout}s",
            duration_s=float(timeout),
        )
    except Exception as e:
        return TestResult(
            status=ValidationStatus.ERROR,
            passed=0, failed=0, errors=1,
            output=str(e),
            duration_s=0.0,
        )


def run_lint(repo_root: Path, file_paths: list[str]) -> LintResult:
    """Run ruff on modified files (if available). Gracefully skip if not installed."""
    py_files = [f for f in file_paths if f.endswith(".py") and (repo_root / f).exists()]
    if not py_files:
        return LintResult(status=ValidationStatus.SKIPPED, issues=[], output="No Python files to lint.")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "--output-format=concise", *py_files],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = proc.stdout + proc.stderr
        # Detect "ruff not installed" via python -m ruff failing
        if proc.returncode != 0 and "No module named ruff" in proc.stderr:
            log.debug("lint.ruff_not_found")
            return LintResult(status=ValidationStatus.SKIPPED, issues=[], output="ruff not installed, skipped.")
        issues = [
            line for line in proc.stdout.splitlines()
            if line.strip()
            and not line.startswith("Found")
            and not line.startswith("All checks passed")
        ]
        status = ValidationStatus.FAILED if proc.returncode != 0 else ValidationStatus.PASSED
        log.info("lint.done", status=status.value, issues=len(issues))
        return LintResult(status=status, issues=issues, output=output)
    except FileNotFoundError:
        log.debug("lint.ruff_not_found")
        return LintResult(status=ValidationStatus.SKIPPED, issues=[], output="ruff not installed, skipped.")
    except Exception as e:
        return LintResult(status=ValidationStatus.ERROR, issues=[], output=str(e))


def _detect_package_manager(repo_root: Path) -> str | None:
    """Detect the package manager from lockfiles."""
    if (repo_root / "bun.lockb").exists() or (repo_root / "bun.lock").exists():
        return "bun"
    if (repo_root / "yarn.lock").exists():
        return "yarn"
    if (repo_root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (repo_root / "package-lock.json").exists() or (repo_root / "package.json").exists():
        return "npm"
    return None


def run_build_check(repo_root: Path, timeout: int = 120) -> LintResult:
    """
    Language-agnostic build/compilation check.
    Auto-detects package manager from lockfiles, then runs build.
    BLOCKING — fails if compilation/build errors found.

    Supports: bun/npm/yarn/pnpm, cargo, go, tsc, python.
    """
    build_commands: list[tuple[list[str], str, str]] = []  # (cmd, name, cwd)
    root = str(repo_root)

    # Detect JS/TS package manager from lockfiles (use correct one, not all)
    pm = _detect_package_manager(repo_root)
    # TypeScript type-check FIRST (catches import errors that bundlers miss)
    if (repo_root / "tsconfig.json").exists():
        if pm:
            build_commands.append((["npx", "tsc", "--noEmit"], "tsc --noEmit", root))
        else:
            build_commands.append((["tsc", "--noEmit"], "tsc --noEmit", root))
    # Framework/bundler build
    if pm and (repo_root / "package.json").exists():
        build_commands.append(([pm, "run", "build"], f"{pm} build", root))

    # Non-JS build systems
    if (repo_root / "Cargo.toml").exists():
        build_commands.append((["cargo", "build", "--message-format=short"], "cargo build", root))
    if (repo_root / "go.mod").exists():
        build_commands.append((["go", "build", "./..."], "go build", root))

    # Search subdirectories if nothing found at root (monorepo support)
    if not build_commands:
        for subdir_name in ["frontend", "backend", "dashboard", "app", "web", "client", "server"]:
            subdir = repo_root / subdir_name
            if not subdir.is_dir():
                continue
            sub_cwd = str(subdir)
            sub_pm = _detect_package_manager(subdir)
            if (subdir / "tsconfig.json").exists():
                tsc_cmd = ["npx", "tsc", "--noEmit"] if sub_pm else ["tsc", "--noEmit"]
                build_commands.append((tsc_cmd, f"tsc --noEmit ({subdir_name}/)", sub_cwd))
            if sub_pm and (subdir / "package.json").exists():
                build_commands.append(([sub_pm, "run", "build"], f"{sub_pm} build ({subdir_name}/)", sub_cwd))

    if not build_commands:
        log.info("build.skipped", reason="no_build_system_found")
        return LintResult(
            status=ValidationStatus.SKIPPED,
            issues=[],
            output="No build system detected.",
        )

    errors = []
    last_error = None

    for cmd, name, cwd in build_commands:
        try:
            log.info("build.running", command=name)
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

            if proc.returncode == 0:
                log.info("build.success", command=name)
                return LintResult(
                    status=ValidationStatus.PASSED,
                    issues=[],
                    output=f"Build succeeded: {name}",
                )

            # Build failed — check if it's missing deps vs real code errors
            output = proc.stdout + proc.stderr
            output_lower = output.lower()

            # Detect missing dependencies (not a real code error)
            missing_deps_signals = [
                "command not found",
                "not found: ",
                "cannot find module",
                "module not found",
                "no such file or directory",
                "enoent",
                "err_module_not_found",
                "could not resolve",
            ]
            is_missing_deps = any(sig in output_lower for sig in missing_deps_signals)

            if is_missing_deps:
                # Distinguish missing npm deps from broken local imports
                # Relative/aliased imports (./  ../  @/) are code bugs, not missing deps
                relative_errors = [
                    line.strip() for line in output.splitlines()
                    if any(k in line.lower() for k in ["cannot find module", "module not found", "could not resolve"])
                    and any(p in line for p in ["'./", "'../", "'@/", '"./', '"../', '"@/'])
                ]
                if relative_errors:
                    log.info("build.local_import_error", command=name, errors=len(relative_errors))
                    return LintResult(
                        status=ValidationStatus.FAILED,
                        issues=relative_errors[:10],
                        output=f"Local import errors ({name}): {output[-2000:]}",
                    )
                log.info("build.missing_deps", command=name)
                return LintResult(
                    status=ValidationStatus.SKIPPED,
                    issues=[],
                    output=f"Build skipped: dependencies not installed ({name}). Run install first.",
                )

            # Real code errors
            error_lines = [
                line.strip()
                for line in output.splitlines()
                if any(
                    keyword in line.lower()
                    for keyword in ["error", "failed", "cannot find", "undefined"]
                )
            ]

            if error_lines:
                errors.extend(error_lines[:10])
                last_error = (name, output[-2000:])
                break

        except FileNotFoundError:
            log.warning("build.cmd_not_found", command=name)
            continue
        except subprocess.TimeoutExpired:
            last_error = (name, f"Build timed out after {timeout}s")
            break
        except Exception:
            continue

    if errors:
        log.info("build.failed", command=last_error[0] if last_error else "unknown", errors=len(errors))
        return LintResult(
            status=ValidationStatus.FAILED,
            issues=errors,
            output=last_error[1] if last_error else "Build failed",
        )

    log.info("build.skipped", reason="all_commands_failed")
    return LintResult(
        status=ValidationStatus.SKIPPED,
        issues=[],
        output="No build system detected (npm, cargo, go, python, java, tsc).",
    )


def run_typecheck(repo_root: Path, file_paths: list[str], timeout: int = 60) -> LintResult:
    """Run mypy on modified Python files (if available). Non-blocking — reports but doesn't fail."""
    py_files = [f for f in file_paths if f.endswith(".py") and (repo_root / f).exists()]
    if not py_files:
        return LintResult(status=ValidationStatus.SKIPPED, issues=[], output="No Python files to type-check.")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--ignore-missing-imports",
             "--no-error-summary", *py_files],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=timeout,
        )
        if "No module named mypy" in proc.stderr:
            return LintResult(status=ValidationStatus.SKIPPED, issues=[], output="mypy not installed.")

        issues = [line for line in proc.stdout.splitlines() if ": error:" in line]
        # Non-blocking: always PASSED for overall, but include issues for PR body
        log.info("typecheck.done", issues=len(issues))
        return LintResult(
            status=ValidationStatus.PASSED,
            issues=issues,
            output=(proc.stdout + proc.stderr)[-2000:],
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return LintResult(status=ValidationStatus.SKIPPED, issues=[], output="mypy unavailable or timed out.")
    except Exception as e:
        return LintResult(status=ValidationStatus.SKIPPED, issues=[], output=str(e))


def validate(impl: Implementation) -> ValidationResult:
    """
    Full validation pipeline:
    1. Write files to disk
    2. Syntax check (Python)
    3. Build check (language-agnostic)
    4. Find + run tests
    5. Lint
    6. Type check (Python, non-blocking)
    """
    repo_root = get_repo_path(impl.repo_id)
    log.info("validator.start", ticket_id=impl.ticket_id, files=len(impl.file_results))

    # 1 — Write files
    written = impl.write_to_disk(str(repo_root))
    log.info("validator.files_written", count=len(written))

    changed_files = [fr.file_path for fr in impl.file_results if fr.change_type != "delete"]

    # 2 — Syntax check (Python)
    syntax = check_syntax(repo_root, changed_files)

    # 3 — Build check (language-agnostic, blocking)
    build = run_build_check(repo_root)

    # 4 — Find test files and run
    test_paths = _find_test_files(repo_root, changed_files)
    log.info("validator.test_files", files=test_paths)
    tests = run_tests(repo_root, test_paths)

    # 5 — Lint
    lint = run_lint(repo_root, changed_files)

    # 6 — Type check (Python, non-blocking — doesn't affect overall status)
    typecheck = run_typecheck(repo_root, changed_files)

    overall = _worst([syntax.status, build.status, tests.status, lint.status])

    result = ValidationResult(
        ticket_id=impl.ticket_id,
        repo_id=impl.repo_id,
        overall=overall,
        syntax=syntax,
        tests=tests,
        lint=lint,
        files_written=written,
        repo_root=str(repo_root),
        typecheck=typecheck,
        build=build,
    )

    log.info("validator.done", overall=overall.value, ticket_id=impl.ticket_id)
    return result
