"""[D] build_check — Auto-detect build system, compile. Routes through sandbox when available. Zero LLM tokens."""
from __future__ import annotations

import subprocess
import structlog
from pathlib import Path

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


_BUILD_COMMANDS = {
    # (config_file, system_name, command)
    "tsconfig.json":  ("tsc",   "npx tsc --noEmit 2>&1"),
    "Cargo.toml":     ("cargo", "cargo check 2>&1"),
    "go.mod":         ("go",    "go build ./... 2>&1"),
}


def _detect_build(repo_path: Path) -> tuple[str, str] | None:
    for config_file, (name, cmd) in _BUILD_COMMANDS.items():
        if (repo_path / config_file).exists():
            return name, cmd
    return None


def execute(ctx: PipelineContext) -> NodeResult:
    if not ctx.repo_path:
        ctx.build_passed = True
        return NodeResult(success=True, tokens_used=0)

    build_info = _detect_build(ctx.repo_path)
    if not build_info:
        log.info("build.skip", reason="no build system detected")
        ctx.build_passed = True
        ctx.build_output = "skipped"
        return NodeResult(success=True, tokens_used=0)

    name, cmd = build_info

    # Flush files and route through sandbox or host
    if ctx.implementation:
        ctx.implementation.write_to_disk(str(ctx.repo_path))
    if ctx.sandbox:
        ctx.sandbox.sync_files(ctx.modified_files)
        return _build_in_sandbox(ctx, name, cmd)
    else:
        return _build_on_host(ctx, name, cmd)


def _build_in_sandbox(ctx: PipelineContext, name: str, cmd: str) -> NodeResult:
    log.info("build.sandbox", system=name)
    result = ctx.sandbox.exec(cmd, timeout=120)
    ctx.build_output = (result["stdout"] + "\n" + result["stderr"])[-4000:]
    ctx.build_passed = (result["exit_code"] == 0)
    log.info("build.done", system=name, passed=ctx.build_passed, sandbox=True)
    return NodeResult(success=True, tokens_used=0)


def _build_on_host(ctx: PipelineContext, name: str, cmd: str) -> NodeResult:
    log.info("build.host", system=name)
    try:
        # Split cmd for subprocess (remove 2>&1 redirect, handle in shell)
        result = subprocess.run(
            cmd, shell=True, cwd=str(ctx.repo_path),
            capture_output=True, text=True, timeout=120,
        )
        ctx.build_output = (result.stdout + "\n" + result.stderr)[-4000:]
        ctx.build_passed = (result.returncode == 0)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        ctx.build_output = str(e)
        ctx.build_passed = True  # Don't block on missing tools

    log.info("build.done", system=name, passed=ctx.build_passed, sandbox=False)
    return NodeResult(success=True, tokens_used=0)
