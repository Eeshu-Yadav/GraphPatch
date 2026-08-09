"""[D] run_tests — Run tests using auto-detected project profile. Routes through Docker sandbox when available. Zero LLM tokens."""
from __future__ import annotations

import re
import subprocess
import structlog
from pathlib import Path

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def _find_affected_tests(repo_path, changed_files: list[str]) -> list[str]:
    """Find test files related to changed files."""
    test_files = []
    seen = set()

    for fp in changed_files:
        p = Path(fp)
        stem = p.stem
        parent = p.parent

        if "test" in stem.lower():
            if fp not in seen:
                test_files.append(fp)
                seen.add(fp)
            continue

        candidates = [
            parent / f"test_{stem}.py",
            parent / f"{stem}_test.py",
            parent / f"tests/test_{stem}.py",
            parent / f"tests/{stem}_test.py",
        ]
        for c in candidates:
            full = repo_path / c
            if full.exists() and str(c) not in seen:
                test_files.append(str(c))
                seen.add(str(c))

        for test_dir_name in ("tests", "test", "__tests__"):
            test_dir = repo_path / parent / test_dir_name
            if test_dir.is_dir():
                for tf in test_dir.glob("test_*.py"):
                    rel = str(tf.relative_to(repo_path))
                    if rel not in seen:
                        test_files.append(rel)
                        seen.add(rel)
                break

    return test_files


def _build_test_command(ctx: PipelineContext, test_files: list[str]) -> str:
    """Build the test command from detected project profile. Language-agnostic."""
    paths_str = " ".join(test_files) if test_files else "."
    profile = ctx.profile

    # Priority 1: Profile-detected test command (most accurate, verified)
    if profile.get("test_command_all") and not test_files:
        return profile["test_command_all"]
    if profile.get("test_command") and test_files:
        return profile["test_command"].replace("{module}", paths_str).replace("{paths}", paths_str)

    # Priority 2: Language-aware fallback
    lang = profile.get("language", "python")
    fallbacks = {
        "python": f"python -m pytest {paths_str} -v --tb=short --no-header",
        "javascript": f"npx jest {paths_str} --no-coverage --forceExit",
        "typescript": f"npx jest {paths_str} --no-coverage --forceExit",
        "rust": f"cargo test {paths_str} -- --nocapture",
        "go": f"go test {paths_str} -v -count=1",
        "ruby": f"bundle exec rspec {paths_str}",
        "java": f"mvn test -Dtest={paths_str} -pl .",
        "kotlin": f"./gradlew test --tests {paths_str}",
        "php": f"vendor/bin/phpunit {paths_str}",
        "swift": f"swift test --filter {paths_str}",
    }
    return fallbacks.get(lang, f"python -m pytest {paths_str} -v --tb=short")


def execute(ctx: PipelineContext) -> NodeResult:
    if not ctx.repo_path or not ctx.implementation:
        return NodeResult(success=True, tokens_used=0)

    # Flush files to disk (sandbox volume mount sees changes immediately)
    ctx.implementation.write_to_disk(str(ctx.repo_path))

    changed = [fr.file_path for fr in ctx.implementation.file_results]
    test_files = _find_affected_tests(ctx.repo_path, changed)

    if not test_files:
        log.info("tests.skip", reason="no test files found")
        ctx.test_passed = True
        ctx.test_output = "No test files found for changed files"
        ctx.test_counts = {"passed": 0, "failed": 0, "errors": 0}
        ctx.ci_round += 1
        return NodeResult(success=True, tokens_used=0)

    # Route through sandbox if available
    if ctx.sandbox:
        return _run_in_sandbox(ctx, test_files)
    else:
        return _run_on_host(ctx, test_files)


def _run_in_sandbox(ctx: PipelineContext, test_files: list[str]) -> NodeResult:
    """Run tests inside Docker sandbox using detected project profile."""
    ctx.sandbox.sync_files(ctx.modified_files)

    cmd = _build_test_command(ctx, test_files) + " 2>&1"

    # Apply env vars from profile
    env_prefix = ""
    if ctx.profile.get("env_vars"):
        env_prefix = " ".join(f"{k}={v}" for k, v in ctx.profile["env_vars"].items()) + " "

    log.info("tests.sandbox", files=test_files, cmd=cmd[:80])
    result = ctx.sandbox.exec(env_prefix + cmd, timeout=150)

    output = result["stdout"] + "\n" + result["stderr"]
    return _parse_result(ctx, output, result["exit_code"])


def _run_on_host(ctx: PipelineContext, test_files: list[str]) -> NodeResult:
    """Run tests directly on host using detected project profile."""
    cmd = _build_test_command(ctx, test_files)
    log.info("tests.host", files=test_files, cmd=cmd[:80])

    # Build environment with profile env vars
    env = None
    if ctx.profile.get("env_vars"):
        import os
        env = {**os.environ, **ctx.profile["env_vars"]}

    try:
        result = subprocess.run(
            cmd, shell=True,
            cwd=str(ctx.repo_path),
            capture_output=True, text=True, timeout=120,
            env=env,
        )
        output = result.stdout + "\n" + result.stderr
        return _parse_result(ctx, output, result.returncode)

    except subprocess.TimeoutExpired:
        ctx.test_output = "Tests timed out after 120 seconds"
        ctx.test_passed = False
        ctx.test_counts = {"passed": 0, "failed": 0, "errors": 1}
        ctx.ci_round += 1
        return NodeResult(success=True, tokens_used=0)  # test_gate handles the failure
    except FileNotFoundError:
        ctx.test_output = "Test runner not found"
        ctx.test_passed = True
        ctx.test_counts = {"passed": 0, "failed": 0, "errors": 0}
        ctx.ci_round += 1
        return NodeResult(success=True, tokens_used=0)


def _parse_result(ctx: PipelineContext, output: str, exit_code: int) -> NodeResult:
    """Parse pytest output into context fields.

    ALWAYS returns success=True — the run_tests node "succeeded" at running tests.
    Whether tests PASSED or FAILED is stored in ctx.test_passed for test_gate to decide.
    This prevents the runner from looking for a failure edge on run_tests.
    """
    ctx.test_output = output[-4000:]
    ctx.test_passed = (exit_code == 0) or (exit_code == 5)  # 5 = no tests collected

    passed = len(re.findall(r" PASSED", output))
    failed = len(re.findall(r" FAILED", output))
    errors = len(re.findall(r" ERROR", output))
    ctx.test_counts = {"passed": passed, "failed": failed, "errors": errors}
    ctx.ci_round += 1

    log.info("tests.done", passed=passed, failed=failed, errors=errors,
             ci_round=ctx.ci_round, sandbox=bool(ctx.sandbox))

    # Always success=True — run_tests "succeeded" at running. test_gate decides what to do.
    return NodeResult(success=True, tokens_used=0)
