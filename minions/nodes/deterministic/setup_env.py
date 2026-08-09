"""[D] setup_env — Reset repo, install deps, create branch, start sandbox, load rules. Zero tokens."""
from __future__ import annotations

import subprocess
import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def execute(ctx: PipelineContext) -> NodeResult:
    from mcp_server.server import _reset_repo_to_origin, _install_deps
    from layer4_planner.file_reader import get_repo_path
    from minions.rules.loader import load_rules

    # Reset repo to clean state
    _reset_repo_to_origin(ctx.repo_id)

    ctx.repo_path = get_repo_path(ctx.repo_id)

    # Create feature branch
    slug = ctx.ticket_id.lower()
    for ch in " /\\:*?\"<>|":
        slug = slug.replace(ch, "-")
    slug = slug[:30].strip("-")
    ctx.branch_name = f"minion/{slug}"

    subprocess.run(
        ["git", "checkout", "-b", ctx.branch_name],
        cwd=str(ctx.repo_path),
        capture_output=True,
    )

    # Start Docker sandbox (if available)
    ctx.sandbox = _start_sandbox(ctx)

    # Install deps — inside sandbox if running, else on host
    if ctx.sandbox:
        env_result = ctx.sandbox.setup_environment()
        log.info("setup_env.sandbox_deps", status=env_result.get("status"),
                 method=env_result.get("method", ""))
    else:
        _install_deps(ctx.repo_id)

    # Detect project profile (test runner, build system, etc.)
    try:
        from layer45_agent.sandbox import detect_project_profile
        ctx.profile = detect_project_profile(
            ctx.repo_path, ctx.repo_id, sandbox=ctx.sandbox, use_cache=True,
        )
    except Exception as e:
        log.warning("setup_env.profile_failed", error=str(e))
        ctx.profile = {}

    # Load directory-scoped rules from repo root
    ctx.directory_rules = load_rules(ctx.repo_path)

    log.info("setup_env.done", repo=ctx.repo_id, branch=ctx.branch_name,
             sandbox=bool(ctx.sandbox), rules_loaded=bool(ctx.directory_rules),
             profile_language=ctx.profile.get("language", "unknown"),
             profile_test_cmd=ctx.profile.get("test_command", "")[:50])

    return NodeResult(success=True, tokens_used=0)


def _start_sandbox(ctx: PipelineContext):
    """Try to start a Docker sandbox. Returns Sandbox or None."""
    try:
        from layer45_agent.sandbox import Sandbox, should_use_sandbox

        if not should_use_sandbox():
            log.info("setup_env.sandbox_skip", reason="Docker not available or disabled")
            return None

        sandbox = Sandbox(ctx.repo_path, run_id=ctx.ticket_id[:8])
        result = sandbox.start()

        if result.get("status") in ("started", "already_running"):
            log.info("setup_env.sandbox_ready",
                     container=result.get("container"),
                     image=result.get("image"))
            return sandbox
        else:
            log.warning("setup_env.sandbox_failed", error=result.get("error", ""))
            return None

    except ImportError:
        log.debug("setup_env.sandbox_skip", reason="sandbox module not available")
        return None
    except Exception as e:
        log.warning("setup_env.sandbox_error", error=str(e))
        return None
