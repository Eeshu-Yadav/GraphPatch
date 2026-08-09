"""[D] reproduce_bug — Run failing test BEFORE the agent sees anything. Routes through sandbox. Zero tokens."""
from __future__ import annotations

import re
import subprocess
import structlog

from minions.engine.blueprint import NodeResult
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


def _extract_test_hints(title: str, body: str) -> list[str]:
    """Extract test file paths or test names from ticket text."""
    text = f"{title}\n{body}"
    hints = []

    for m in re.finditer(r'[\w/]*test[\w/]*\.py', text):
        hints.append(m.group())
    for m in re.finditer(r'test_\w+(?:::test_\w+)?', text):
        hints.append(m.group())

    return list(dict.fromkeys(hints))


def execute(ctx: PipelineContext) -> NodeResult:
    if not ctx.repo_path:
        return NodeResult(success=True, tokens_used=0)

    hints = _extract_test_hints(ctx.title, ctx.body)
    if not hints:
        log.info("reproduce.skip", reason="no test hints in ticket")
        return NodeResult(success=True, tokens_used=0)

    test_target = hints[0]

    # Use detected profile's test command if available
    profile = ctx.profile
    if profile.get("test_command"):
        cmd = profile["test_command"].replace("{module}", test_target).replace("{paths}", test_target)
    else:
        # Fallback by language
        lang = profile.get("language", "python")
        fallbacks = {
            "python": f"python -m pytest {test_target} -x --tb=short --no-header",
            "javascript": f"npx jest {test_target} --no-coverage --forceExit",
            "typescript": f"npx jest {test_target} --no-coverage --forceExit",
            "rust": f"cargo test {test_target} -- --nocapture",
            "go": f"go test ./{test_target}/... -v -count=1",
        }
        cmd = fallbacks.get(lang, f"python -m pytest {test_target} -x --tb=short --no-header")
    cmd += " 2>&1"

    # Build env vars from profile
    env_prefix = ""
    if profile.get("env_vars"):
        env_prefix = " ".join(f"{k}={v}" for k, v in profile["env_vars"].items()) + " "

    log.info("reproduce.running", target=test_target, cmd=cmd[:80], sandbox=bool(ctx.sandbox))

    if ctx.sandbox:
        result = ctx.sandbox.exec(env_prefix + cmd, timeout=60)
        ctx.reproduce_output = (result["stdout"] + "\n" + result["stderr"])[-4000:]
        log.info("reproduce.done", exit_code=result["exit_code"])
    else:
        import os
        env = {**os.environ, **profile.get("env_vars", {})} if profile.get("env_vars") else None
        try:
            result = subprocess.run(
                cmd, shell=True,
                cwd=str(ctx.repo_path),
                capture_output=True, text=True, timeout=60,
                env=env,
            )
            ctx.reproduce_output = (result.stdout + "\n" + result.stderr)[-4000:]
            log.info("reproduce.done", exit_code=result.returncode)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            ctx.reproduce_output = f"Could not reproduce: {e}"
            log.warning("reproduce.failed", error=str(e))

    return NodeResult(success=True, tokens_used=0)
