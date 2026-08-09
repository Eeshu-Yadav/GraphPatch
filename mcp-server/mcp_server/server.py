"""
Ticket-to-PR MCP Server.

Exposes the full pipeline as MCP tools so Claude Code (or any MCP client)
can trigger code changes directly from natural language instructions.

Tools:
  - run_pipeline       — Full L3→L4→L5→L6 (no PR, returns diff + validation)
  - run_pipeline_pr    — Full L3→L4→L5→L6→L7 (opens GitHub PR)
  - index_repo         — Trigger Layer 2 indexing on a repo
  - get_pipeline_status — Describe what layers/services are running

Usage (stdio transport, default for Claude Code):
    python -m mcp_server.server
"""
from __future__ import annotations

import os
import sys
import logging
import traceback

# Load .env from monorepo root
from pathlib import Path
_env_path = Path(__file__).parent.parent.parent / ".env"
if _env_path.exists():
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=str(_env_path))

# CRITICAL: MCP uses stdout for JSON-RPC — redirect ALL logging to stderr
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

import structlog
structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(colors=False),
    ],
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

log = structlog.get_logger(__name__)


def _reset_repo_to_origin(repo_id: str) -> None:
    """Checkout the base branch and hard-reset to origin before each pipeline run."""
    import subprocess
    repo_cache = os.environ.get("REPO_CACHE_DIR", str(Path.home() / "Desktop" / "context" / "repos"))
    slug = repo_id.replace("/", "_")
    repo_path = f"{repo_cache}/{slug}"
    if not os.path.exists(repo_path):
        return

    # Strategy: find the base branch (what was indexed/cloned)
    # 1. Check origin/HEAD
    # 2. List all local branches that track a remote
    # 3. Fallback chain: main → master
    candidates = []

    # Try origin/HEAD first — auto-set if missing
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            candidates.append(result.stdout.strip().split("/")[-1])
        else:
            # origin/HEAD not set — fix it automatically
            subprocess.run(
                ["git", "remote", "set-head", "origin", "--auto"],
                cwd=repo_path, capture_output=True, timeout=10,
            )
            result = subprocess.run(
                ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
                cwd=repo_path, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                candidates.append(result.stdout.strip().split("/")[-1])
    except Exception:
        pass

    # Prioritize common defaults BEFORE random local branches
    candidates.extend(["main", "master"])

    # Then add other local branches as fallback (excluding fix/ branches)
    try:
        result = subprocess.run(
            ["git", "branch", "--format=%(refname:short)"],
            cwd=repo_path, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            local_branches = [b.strip() for b in result.stdout.splitlines()
                              if b.strip() and not b.strip().startswith("fix/")
                              and b.strip() not in ("main", "master")]  # already added above
            candidates.extend(local_branches)
    except Exception:
        pass

    # Try each candidate — first one that exists and has a remote wins
    for branch in candidates:
        result = subprocess.run(
            ["git", "checkout", branch],
            cwd=repo_path, capture_output=True, timeout=10,
        )
        if result.returncode == 0:
            subprocess.run(["git", "fetch", "origin"], cwd=repo_path, capture_output=True)
            subprocess.run(
                ["git", "reset", "--hard", f"origin/{branch}"],
                cwd=repo_path, capture_output=True,
            )
            # Clean up any leftover fix branches and untracked files
            subprocess.run(["git", "clean", "-fd"], cwd=repo_path, capture_output=True)
            return

def _install_deps(repo_id: str) -> None:
    """Auto-detect package manager and install dependencies if needed."""
    import subprocess
    repo_cache = os.environ.get("REPO_CACHE_DIR", str(Path.home() / "Desktop" / "context" / "repos"))
    slug = repo_id.replace("/", "_")
    repo_path = Path(f"{repo_cache}/{slug}")
    if not repo_path.exists():
        return

    log = structlog.get_logger("deps")

    # Skip if node_modules already exists and is recent
    node_modules = repo_path / "node_modules"
    if node_modules.exists() and any(node_modules.iterdir()):
        log.info("deps.already_installed", path=str(repo_path))
        return

    # Detect package manager
    if (repo_path / "bun.lockb").exists() or (repo_path / "bun.lock").exists():
        pm, cmd = "bun", ["bun", "install", "--frozen-lockfile"]
    elif (repo_path / "yarn.lock").exists():
        pm, cmd = "yarn", ["yarn", "install", "--frozen-lockfile"]
    elif (repo_path / "pnpm-lock.yaml").exists():
        pm, cmd = "pnpm", ["pnpm", "install", "--frozen-lockfile"]
    elif (repo_path / "package.json").exists():
        pm, cmd = "npm", ["npm", "ci"]
    else:
        # Not a JS/TS project — check for other ecosystems
        if (repo_path / "Cargo.toml").exists():
            pm, cmd = "cargo", ["cargo", "fetch"]
        elif (repo_path / "go.mod").exists():
            pm, cmd = "go", ["go", "mod", "download"]
        elif (repo_path / "requirements.txt").exists():
            log.info("deps.python_skip", reason="venv required")
            return
        else:
            return

    log.info("deps.installing", pm=pm, path=str(repo_path))
    try:
        result = subprocess.run(
            cmd, cwd=str(repo_path),
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            log.info("deps.installed", pm=pm)
        else:
            # Try without --frozen-lockfile as fallback
            fallback_cmd = [cmd[0], "install"]
            result = subprocess.run(
                fallback_cmd, cwd=str(repo_path),
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                log.info("deps.installed_fallback", pm=pm)
            else:
                log.warning("deps.install_failed", pm=pm, stderr=result.stderr[:300])
    except subprocess.TimeoutExpired:
        log.warning("deps.timeout", pm=pm)
    except FileNotFoundError:
        log.warning("deps.pm_not_found", pm=pm)


from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="ticket-to-pr",
    instructions=(
        "This server runs a ticket-to-PR agentic pipeline. "
        "Use run_pipeline to implement a GitHub issue as code changes "
        "and optionally open a pull request. "
        "Use index_repo to index a new repository before running the pipeline."
    ),
)


def _extract_pr_summary(pr_body: str, fallback_title: str) -> str:
    """
    Extract only the meaningful description from a PR body,
    stripping auto-generated sections (Files Changed, Validation, Model, etc.).
    """
    if not pr_body:
        return fallback_title

    # Try to extract just the Summary section
    import re
    summary_match = re.search(
        r'##\s*Summary\s*\n(.*?)(?=\n##|\n---|\Z)',
        pr_body,
        re.DOTALL,
    )
    if summary_match:
        summary = summary_match.group(1).strip()
        # Remove markdown bullets/formatting but keep the text
        summary = re.sub(r'^[\s*•-]+', '', summary, flags=re.MULTILINE).strip()
        if len(summary) > 20:
            return summary

    # Fallback: take everything before the first ## section
    first_section = pr_body.split("##")[0].strip()
    if len(first_section) > 20:
        return first_section

    return fallback_title


def _build_agent_config():
    """Build AgentConfig with per-phase model selection from env vars."""
    from layer45_agent.models import AgentConfig
    return AgentConfig(
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        explore_model=os.environ.get("CLAUDE_EXPLORE_MODEL", "claude-haiku-4-5-20251001"),
        plan_model=os.environ.get("CLAUDE_PLAN_MODEL", "claude-opus-4-6"),
        write_model=os.environ.get("CLAUDE_WRITE_MODEL", "claude-sonnet-4-20250514"),
    )


# ---------------------------------------------------------------------------
# Tool: run_pipeline
# ---------------------------------------------------------------------------

@mcp.tool()
def run_pipeline(
    repo_id: str,
    ticket_id: str,
    title: str,
    body: str,
) -> str:
    """
    Run the ticket-to-PR pipeline WITHOUT opening a PR.

    Runs the graph-powered agentic loop (L3→L4.5→L6) where Claude uses tools
    iteratively (read files, query graph, write edits, run tests).

    Args:
        repo_id:   GitHub repo in owner/repo format (e.g. "realpython/codetiming")
        ticket_id: Unique ticket identifier (e.g. "TICKET-1" or "GH-42")
        title:     Short description of the issue/feature
        body:      Full description of the ticket (acceptance criteria, steps to reproduce, etc.)
    """
    try:
        from layer3_context.models.ticket import Ticket
        from layer3_context.assembly.assembler import assemble
        from layer6_validator.runner import validate
        from layer45_agent.agent import run_agent

        _reset_repo_to_origin(repo_id)
        ticket = Ticket(ticket_id=ticket_id, title=title, body=body, repo_id=repo_id)
        bundle = assemble(ticket)

        config = _build_agent_config()
        agent_result = run_agent(ticket, bundle, config)

        # Gate: check if agent actually produced changes
        if not agent_result.success:
            return (
                f"## Pipeline Failed — {ticket_id}\n\n"
                f"**Agent error:** {agent_result.error}\n"
                f"**Iterations:** {agent_result.iterations}\n"
                f"**Tool calls:** {len(agent_result.tool_calls)}"
            )
        if not agent_result.implementation.file_results:
            return (
                f"## Pipeline Produced No Changes — {ticket_id}\n\n"
                f"The agent completed {agent_result.iterations} iterations "
                f"but did not modify any files."
            )

        impl = agent_result.implementation
        agent_info = (
            f"**Agent:** {agent_result.iterations} iterations, "
            f"{len(agent_result.tool_calls)} tool calls, "
            f"{agent_result.total_prompt_tokens} prompt tokens\n"
        )
        plan_summary = agent_result.implementation.plan_summary

        validation = validate(impl)
        diff = impl.to_diff_text()

        output = [
            f"## Pipeline Result — {ticket_id}",
            "",
            agent_info,
            f"**Plan:** {plan_summary}",
            "",
            f"**Files changed:** {', '.join(fr.file_path for fr in impl.file_results)}",
            "",
            f"**Validation:**",
            f"```",
            validation.summary(),
            f"```",
            "",
            f"**Diff:**",
            f"```diff",
            diff[:6000],
            "```",
        ]
        return "\n".join(output)

    except Exception:
        return f"Pipeline failed:\n```\n{traceback.format_exc()}\n```"


# ---------------------------------------------------------------------------
# Tool: run_pipeline_pr
# ---------------------------------------------------------------------------

@mcp.tool()
def run_pipeline_pr(
    repo_id: str,
    ticket_id: str,
    title: str,
    body: str,
    github_token: str = "",
    draft: bool = False,
) -> str:
    """
    Run the pipeline AND open a GitHub Pull Request.

    Runs the graph-powered agentic loop, validates, and opens a PR.

    Args:
        repo_id:       GitHub repo in owner/repo format (e.g. "realpython/codetiming")
        ticket_id:     Unique ticket identifier (e.g. "TICKET-1" or "GH-42")
        title:         Short description of the issue/feature
        body:          Full description of the ticket
        github_token:  GitHub Personal Access Token (or set GITHUB_TOKEN env var)
        draft:         Open as a draft PR (default: False)
    """
    try:
        from layer3_context.models.ticket import Ticket
        from layer3_context.assembly.assembler import assemble
        from layer4_planner.file_reader import get_repo_path
        from layer6_validator.runner import validate
        from layer6_validator.reviewer import review_changes, revert_unnecessary_files
        from layer7_publisher.publisher import publish
        from layer45_agent.agent import run_agent

        _reset_repo_to_origin(repo_id)
        _install_deps(repo_id)
        token = github_token or os.environ.get("GITHUB_TOKEN", "")
        ticket = Ticket(ticket_id=ticket_id, title=title, body=body, repo_id=repo_id)
        bundle = assemble(ticket)

        config = _build_agent_config()
        validation = None

        # Single agent run — no wasteful restarts
        # Feedback from previous failures gets injected into system prompt
        agent_result = run_agent(ticket, bundle, config)

        # If agent produced 0 files, fail fast — don't retry
        if not agent_result.implementation.file_results:
            return (
                f"## Pipeline Failed — {ticket_id}\n\n"
                f"**Error:** Agent explored but produced no changes.\n"
                f"**Reason:** {agent_result.error or 'Could not determine what to modify.'}\n"
                f"No token-wasting retry — agent had full context and tools."
            )

        impl = agent_result.implementation
        validation = validate(impl)

        # Accept if: no syntax/lint errors + has changes + build didn't FAIL
        # Build SKIPPED (missing deps) is OK; build FAILED (code errors) is not
        from layer6_validator.models.result import ValidationStatus
        no_syntax_errors = not validation.syntax.errors
        no_lint_issues = not validation.lint.issues
        has_changes = bool(impl.file_results)
        build_failed = (
            validation.build is not None
            and validation.build.status == ValidationStatus.FAILED
        )

        if not (validation.passed() or (no_syntax_errors and no_lint_issues and has_changes and not build_failed)):
            # Validation has fixable errors — retry ONCE with feedback
            # Reuse the SAME bundle (ticket hasn't changed, no need to re-assemble)
            cached_bundle = bundle
            err_parts = [f"## Validation FAILED — fix these errors:\n"]
            if validation.syntax.errors:
                err_parts.append("**Syntax Errors:**")
                for e in validation.syntax.errors:
                    err_parts.append(f"  - {e}")
            if validation.tests.failed > 0 or validation.tests.errors > 0:
                err_parts.append(
                    f"**Tests:** {validation.tests.passed} passed, "
                    f"{validation.tests.failed} failed, {validation.tests.errors} errors"
                )
                test_tail = "\n".join(validation.tests.output.splitlines()[-40:])
                err_parts.append(f"```\n{test_tail}\n```")
            if validation.lint.issues:
                err_parts.append(f"**Lint Issues ({len(validation.lint.issues)}):**")
                for issue in validation.lint.issues[:15]:
                    err_parts.append(f"  - {issue}")

            _reset_repo_to_origin(repo_id)
            _install_deps(repo_id)
            # Reuse cached bundle — no re-assembly needed
            retry_result = run_agent(
                ticket, cached_bundle, config,
                feedback="\n".join(err_parts),
                prev_cache=agent_result.exploration_cache,
            )
            if retry_result.implementation.file_results:
                impl = retry_result.implementation
                validation = validate(impl)

        if validation and not validation.passed():
            draft = True

        # --- Code Review Gate (Opus) ---
        repo_root = Path(get_repo_path(repo_id))

        review = review_changes(
            repo_root=repo_root,
            repo_id=repo_id,
            ticket_title=title,
            ticket_body=body,
            file_results=impl.file_results,
        )

        if not review.approved:
            log.info("review.rejected", feedback=review.feedback[:200])

            # Revert unnecessary files
            if review.files_to_drop:
                revert_unnecessary_files(repo_root, review.files_to_drop)
                impl.file_results = [
                    fr for fr in impl.file_results
                    if fr.file_path not in review.files_to_drop
                ]

            if not impl.file_results:
                # ALL files dropped — nothing to publish
                return (
                    f"## Pipeline Review Rejected — {ticket_id}\n\n"
                    f"**Review Feedback:** {review.feedback}\n"
                    f"All changes were deemed unnecessary by Opus reviewer."
                )

            # Files survived review — publish them as draft (reviewer had concerns)
            draft = True

        review_summary = f"Review: {'APPROVED' if review.approved else 'PARTIAL'} — {review.summary}"

        pr_result = publish(
            impl=impl,
            ticket_title=title,
            github_token=token,
            validation_summary=f"{validation.summary()}\n\n{review_summary}",
            draft=draft,
        )

        if pr_result.success:
            status = "DRAFT (validation failed)" if not validation.passed() else "Ready"
            return (
                f"## PR Opened — {ticket_id}\n\n"
                f"**URL:** {pr_result.pr_url}\n"
                f"**Branch:** `{pr_result.branch_name}`\n"
                f"**Status:** {status}\n"
                f"**Files:** {', '.join(pr_result.files_changed)}\n\n"
                f"**Validation:**\n```\n{validation.summary()}\n```"
            )
        else:
            return (
                f"## Pipeline ran, but PR failed — {ticket_id}\n\n"
                f"**Error:** {pr_result.error}\n\n"
                f"**Validation:**\n```\n{validation.summary()}\n```"
            )

    except Exception:
        return f"Pipeline failed:\n```\n{traceback.format_exc()}\n```"


# ---------------------------------------------------------------------------
# Tool: retry_pipeline_pr
# ---------------------------------------------------------------------------

@mcp.tool()
def retry_pipeline_pr(
    repo_id: str,
    pr_number: int,
    ticket_id: str = "",
    title: str = "",
    body: str = "",
    github_token: str = "",
) -> str:
    """
    Re-run the pipeline with feedback from a failed/rejected PR.

    Fetches the PR's diff and review comments (including screenshots),
    then runs a new agent that learns from the previous attempt.
    Pushes the fix as a new commit on the SAME PR branch — no new PR created.

    Args:
        repo_id:       GitHub repo in owner/repo format
        pr_number:     The PR number to retry from (e.g. 18)
        ticket_id:     Override ticket ID (defaults to RETRY-{pr_number})
        title:         Override title (defaults to PR title)
        body:          Override body (defaults to PR body)
        github_token:  GitHub PAT (or set GITHUB_TOKEN env var)
    """
    try:
        from layer3_context.models.ticket import Ticket
        from layer3_context.assembly.assembler import assemble
        from layer6_validator.runner import validate
        from layer7_publisher import github_api
        from layer45_agent.agent import run_agent

        token = github_token or os.environ.get("GITHUB_TOKEN", "")

        # 1. Fetch previous PR data
        pr_info = github_api.get_pr_info(repo_id, pr_number, token)
        pr_diff = github_api.get_pr_diff(repo_id, pr_number, token)
        review_comments = github_api.get_pr_review_comments(repo_id, pr_number, token)
        issue_comments = github_api.get_pr_issue_comments(repo_id, pr_number, token)

        # Use PR info as defaults if not provided
        title = title or pr_info["title"]
        # Strip PR template noise — only keep the Summary section as ticket body
        raw_body = body or pr_info["body"] or title
        clean_body = _extract_pr_summary(raw_body, title)
        body = clean_body
        ticket_id = ticket_id or f"RETRY-{pr_number}"

        # 2. Build feedback string + log what we found
        import structlog as _log
        _retry_log = _log.get_logger("retry")

        _retry_log.info("retry.pr_fetched", pr=pr_number, state=pr_info["state"],
                        diff_lines=pr_diff.count("\n"), review_comments=len(review_comments),
                        issue_comments=len(issue_comments))

        for c in review_comments:
            _retry_log.info("retry.review_comment", file=c["file"], line=c["line"],
                            author=c["author"], body=c["body"][:200])
        for c in issue_comments:
            _retry_log.info("retry.issue_comment", author=c["author"], body=c["body"][:200])

        feedback_parts = []
        feedback_parts.append(f"### Previous PR: #{pr_number}")
        feedback_parts.append(f"**Diff from previous attempt:**\n```diff\n{pr_diff[:4000]}\n```")

        if review_comments:
            feedback_parts.append("**Reviewer feedback (inline comments):**")
            for c in review_comments:
                feedback_parts.append(f"- **{c['file']}:{c['line']}** ({c['author']}): {c['body']}")

        if issue_comments:
            feedback_parts.append("**Reviewer feedback (general comments):**")
            for c in issue_comments:
                feedback_parts.append(f"- ({c['author']}): {c['body']}")

        if not review_comments and not issue_comments:
            feedback_parts.append("**No reviewer comments found.** The PR was likely rejected because the fix was incorrect or incomplete. Analyze the diff above and find a better approach.")

        feedback = "\n\n".join(feedback_parts)

        # 3. Extract images from reviewer comments (screenshots of bugs)
        all_comments = review_comments + issue_comments
        feedback_images = github_api.extract_images_from_comments(all_comments, token)

        # 4. Reset repo to main, run agent with feedback + images (with validation gate)
        _reset_repo_to_origin(repo_id)
        ticket = Ticket(ticket_id=ticket_id, title=title, body=body, repo_id=repo_id)
        bundle = assemble(ticket)

        config = _build_agent_config()
        max_attempts = 2  # fewer retries for retry flow (already a retry itself)
        current_feedback = feedback
        current_images = feedback_images
        validation = None

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                _reset_repo_to_origin(repo_id)
                bundle = assemble(ticket)
                current_images = None  # images only relevant on first pass

            agent_result = run_agent(
                ticket, bundle, config,
                feedback=current_feedback,
                feedback_images=current_images,
            )
            impl = agent_result.implementation
            validation = validate(impl)

            if validation.passed():
                break

            # Append validation errors to feedback for next attempt
            err_parts = [current_feedback, f"\n\n## Auto-retry (attempt {attempt} FAILED):\n"]
            if validation.syntax.errors:
                err_parts.append("**Syntax Errors:** " + "; ".join(validation.syntax.errors))
            if validation.tests.failed > 0 or validation.tests.errors > 0:
                test_tail = "\n".join(validation.tests.output.splitlines()[-30:])
                err_parts.append(f"**Test failures:**\n```\n{test_tail}\n```")
            if validation.lint.issues:
                err_parts.append("**Lint:** " + "; ".join(validation.lint.issues[:10]))
            err_parts.append("\nFix these errors. Do NOT repeat the same edits.")
            current_feedback = "\n".join(err_parts)

        # 5. Push new commit onto the EXISTING PR branch (not a new PR)
        from layer7_publisher import git_ops
        from layer4_planner.file_reader import get_repo_path

        repo_root = get_repo_path(repo_id)
        head_branch = pr_info["head_branch"]

        try:
            # Checkout the existing PR branch
            git_ops.checkout_branch(repo_root, head_branch, token, repo_id)

            # Write modified files to disk
            for fr in impl.file_results:
                fp = repo_root / fr.file_path
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(fr.modified_content, encoding="utf-8")

            # Commit & push to the same branch
            modified = [fr.file_path for fr in impl.file_results if fr.change_type != "delete"]
            deleted = [fr.file_path for fr in impl.file_results if fr.change_type == "delete"]

            commit_msg = (
                f"fix({ticket_id.lower()}): retry — address review feedback from PR #{pr_number}\n\n"
                f"{impl.plan_summary[:300]}"
            )
            sha = git_ops.commit_changes(repo_root, modified, deleted, commit_msg)
            git_ops.push_branch(repo_root, head_branch, token, repo_id)

            pr_url = f"https://github.com/{repo_id}/pull/{pr_number}"
            return (
                f"## Retry pushed to PR #{pr_number}\n\n"
                f"**PR:** {pr_url}\n"
                f"**Branch:** `{head_branch}`\n"
                f"**Commit:** `{sha[:8]}`\n"
                f"**Files:** {', '.join(modified)}\n\n"
                f"**Agent:** {agent_result.iterations} iterations, "
                f"{len(agent_result.tool_calls)} tool calls\n\n"
                f"**Validation:**\n```\n{validation.summary()}\n```"
            )

        except Exception as e:
            return (
                f"## Retry agent ran, but push failed — {ticket_id}\n\n"
                f"**PR:** #{pr_number}\n"
                f"**Error:** {e}\n\n"
                f"**Validation:**\n```\n{validation.summary()}\n```"
            )

    except Exception:
        return f"Retry pipeline failed:\n```\n{traceback.format_exc()}\n```"


# ---------------------------------------------------------------------------
# Tool: index_repo
# ---------------------------------------------------------------------------

@mcp.tool()
def index_repo(
    repo_url: str,
    repo_id: str,
) -> str:
    """
    Clone and index a GitHub repository (Layer 2).

    This must be run before run_pipeline for any new repo.
    Takes ~2-5 minutes depending on repo size.

    Args:
        repo_url:  Full GitHub clone URL (e.g. "https://github.com/realpython/codetiming")
        repo_id:   Short identifier in owner/repo format (e.g. "realpython/codetiming")
    """
    try:
        import subprocess
        indexer_dir = str(Path(__file__).parent.parent.parent / "layer2-indexer")
        venv_python = str(Path(__file__).parent.parent.parent / ".venv" / "bin" / "python")

        result = subprocess.run(
            [venv_python, "-m", "src.cli", "index",
             "--repo", repo_url,
             "--id", repo_id,
             "--sync", "--skip-descriptions"],
            cwd=indexer_dir,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode == 0:
            return f"Indexed {repo_id} successfully.\n\n```\n{result.stdout[-2000:]}\n```"
        else:
            return f"Indexing failed (exit {result.returncode}):\n```\n{result.stderr[-2000:]}\n```"

    except Exception:
        return f"index_repo failed:\n```\n{traceback.format_exc()}\n```"


# ---------------------------------------------------------------------------
# Tool: get_pipeline_status
# ---------------------------------------------------------------------------

@mcp.tool()
def get_pipeline_status() -> str:
    """
    Check that all pipeline services (Qdrant, Memgraph, Ollama, Redis) are reachable
    and report which environment variables are set.
    """
    lines = ["## Pipeline Status\n"]

    checks = {
        "Qdrant (vector DB)": ("http", os.environ.get("QDRANT_URL", "http://localhost:6333")),
        "Ollama (embeddings)": ("http", os.environ.get("OLLAMA_URL", "http://localhost:11434")),
    }

    import socket

    def tcp_check(host: str, port: int) -> bool:
        try:
            s = socket.create_connection((host, port), timeout=2)
            s.close()
            return True
        except Exception:
            return False

    # Memgraph
    mg_ok = tcp_check(
        os.environ.get("MEMGRAPH_HOST", "localhost"),
        int(os.environ.get("MEMGRAPH_PORT", "7687")),
    )
    lines.append(f"- Memgraph (graph DB): {'✅' if mg_ok else '❌'}")

    # Redis
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    redis_host = redis_url.split("//")[-1].split(":")[0]
    redis_port = int(redis_url.split(":")[-1]) if ":" in redis_url.split("//")[-1] else 6379
    redis_ok = tcp_check(redis_host, redis_port)
    lines.append(f"- Redis:               {'✅' if redis_ok else '❌'}")

    # HTTP checks
    import urllib.request
    for name, (_, url) in checks.items():
        try:
            urllib.request.urlopen(url, timeout=2)
            ok = True
        except Exception:
            ok = False
        lines.append(f"- {name}: {'✅' if ok else '❌'}")

    # Env vars
    lines.append("\n**Environment:**")
    env_vars = ["ANTHROPIC_API_KEY", "GITHUB_TOKEN", "CLAUDE_MODEL"]
    for var in env_vars:
        val = os.environ.get(var, "")
        masked = f"{val[:6]}..." if len(val) > 6 else ("(not set)" if not val else val)
        lines.append(f"- `{var}`: {masked}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Minion Blueprint Tools (NEW — uses minions/ engine alongside existing tools)
# ---------------------------------------------------------------------------

@mcp.tool()
def run_minion(
    repo_id: str,
    ticket_id: str,
    title: str,
    body: str,
    task_type: str = "auto",
) -> str:
    """
    Run the blueprint-based minion pipeline WITHOUT opening a PR.

    Uses deterministic + agentic nodes for structured execution.
    Deterministic nodes (lint, test, build) run in a Docker sandbox when available.
    Falls back to the legacy agent pipeline if the blueprint engine fails.

    This is the minion equivalent of run_pipeline — same inputs, structured execution.

    Args:
        repo_id:    GitHub repo in owner/repo format (e.g. "realpython/codetiming")
        ticket_id:  Unique ticket identifier (e.g. "TICKET-1" or "GH-42")
        title:      Short description of the issue/feature
        body:       Full description of the ticket
        task_type:  auto | bug_fix | feature | migration | test_fix (default: auto-detect)
    """
    try:
        from minions.mcp_tools import run_minion as _run_minion
        return _run_minion(
            repo_id=repo_id,
            ticket_id=ticket_id,
            title=title,
            body=body,
            task_type=task_type,
            fallback=True,
        )
    except Exception:
        return f"Minion pipeline failed:\n```\n{traceback.format_exc()}\n```"


@mcp.tool()
def run_minion_pr(
    repo_id: str,
    ticket_id: str,
    title: str,
    body: str,
    task_type: str = "auto",
    github_token: str = "",
    draft: bool = False,
) -> str:
    """
    Run the blueprint-based minion pipeline AND open a GitHub Pull Request.

    Uses deterministic + agentic nodes for structured execution:
    - [D] nodes: setup, lint, test, build, PR creation (zero LLM tokens)
    - [A] nodes: explore, write code, fix errors (focused LLM calls)
    - [G] gates: lint check, test check (2-round hard cap), code review

    Deterministic nodes run in a Docker sandbox when available.
    Falls back to the legacy agent pipeline if the blueprint engine fails.

    This is the minion equivalent of run_pipeline_pr — same inputs, structured execution.

    Args:
        repo_id:       GitHub repo in owner/repo format (e.g. "realpython/codetiming")
        ticket_id:     Unique ticket identifier (e.g. "TICKET-1" or "GH-42")
        title:         Short description of the issue/feature
        body:          Full description of the ticket
        task_type:     auto | bug_fix | feature | migration | test_fix (default: auto-detect)
        github_token:  GitHub Personal Access Token (or set GITHUB_TOKEN env var)
        draft:         Open as a draft PR (default: False)
    """
    try:
        from minions.mcp_tools import run_minion_pr as _run_minion_pr
        return _run_minion_pr(
            repo_id=repo_id,
            ticket_id=ticket_id,
            title=title,
            body=body,
            task_type=task_type,
            github_token=github_token,
            draft=draft,
            fallback=True,
        )
    except Exception:
        return f"Minion PR pipeline failed:\n```\n{traceback.format_exc()}\n```"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
