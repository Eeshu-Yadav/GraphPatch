"""Core ReAct agent loop — graph-powered coding agent using Claude API."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import structlog
import anthropic

from layer3_context.models.ticket import Ticket
from layer3_context.models.context import ContextBundle
from layer45_agent.implementation import Implementation, FileResult

from layer45_agent.models import AgentConfig, AgentResult, ToolCallRecord, ExplorationCache, EditHistory
from layer45_agent.tool_defs import ALL_TOOL_DEFS
from layer45_agent.tools import execute_tool, clear_tool_cache
from layer45_agent.prompt import build_system_prompt
from layer45_agent.safety import detect_oscillation, check_token_budget
from layer45_agent.test_triage import (
    TriageState, classify_test_result, extract_error_signature,
    PHASE_EXPLORING, PHASE_WRITING, PHASE_VERIFYING, PHASE_FINISHING,
)
from layer45_agent.history import compress
from layer45_agent import trace

log = structlog.get_logger(__name__)

# Tools that indicate which phase the agent is in
_EXPLORE_TOOLS = {"search_symbols", "read_file", "get_dependencies", "get_impact",
                  "get_callers", "get_risk_score", "get_test_coverage",
                  "get_coupled_files", "get_reviewers", "search_code"}
_WRITE_TOOLS = {"write_file", "run_tests", "run_command", "get_diff"}


def _has_useful_exploration(tool_log: list[ToolCallRecord]) -> bool:
    """Check if exploration produced actionable results (not just empty/error calls)."""
    useful_reads = 0
    useful_graphs = 0
    for tc in tool_log:
        if tc.tool_name not in _EXPLORE_TOOLS:
            continue
        result = tc.result
        if not isinstance(result, dict):
            continue
        if tc.tool_name == "read_file":
            if "error" not in result and result.get("total_lines", 0) > 0:
                useful_reads += 1
        elif tc.tool_name == "search_symbols":
            if len(result.get("symbols", [])) > 0:
                useful_reads += 1
        elif tc.tool_name == "search_code":
            if len(result.get("matches", [])) > 0:
                useful_reads += 1
        elif tc.tool_name in {"get_dependencies", "get_impact", "get_callers"}:
            # Check if any list fields are non-empty
            has_data = any(
                isinstance(v, list) and len(v) > 0
                for v in result.values()
            ) or any(
                isinstance(v, int) and v > 0
                for k, v in result.items() if k == "total"
            )
            if has_data:
                useful_graphs += 1
    # Require useful reads. Graph results are a bonus, not a gate.
    # Many issues (config changes, Sentry filtering) have no graph matches.
    return useful_reads >= 2 or (useful_reads >= 1 and useful_graphs >= 1)


def _pick_model(
    config: AgentConfig,
    tool_log: list[ToolCallRecord],
    modified_files: dict,
    repair_count: int = 0,
) -> str:
    """Pick model based on COGNITIVE DEMAND of the current task, not pipeline phase.

    Signal-based routing:
    - Haiku ($0.001/turn):  Exploration, tool routing, output parsing
    - Sonnet ($0.01/turn):  Code reading, writing, test analysis, planning
    - Opus ($0.05/turn):    ONLY when stuck — capped at 3 total turns per run
    """
    has_written = any(tc.tool_name == "write_file" for tc in tool_log)
    n_tools = len(tool_log)

    # ── SIGNAL 1: Stuck detection → escalate to Opus ──────────────────────
    if n_tools >= 6:
        recent = [tc.tool_name for tc in tool_log[-6:]]
        unique_recent = set(recent)

        # Same 1-2 tools repeated 6 times without writing = stuck exploring
        is_stuck_exploring = (
            len(unique_recent) <= 2
            and not has_written
            and not modified_files
        )

        # 3+ empty searches in a row = can't find code
        recent_searches = [
            tc for tc in tool_log[-8:]
            if tc.tool_name == "search_code"
            and isinstance(tc.result, dict)
            and len(tc.result.get("matches", [])) == 0
        ]
        is_stuck_searching = len(recent_searches) >= 3

        # Only escalate if actually stuck AND haven't used Opus too much
        # Count Opus turns by checking model_switch log entries
        # (tool_log doesn't track model, so cap by simple counter)
        if is_stuck_exploring or is_stuck_searching:
            log.info("model.escalate_opus", reason="stuck",
                     exploring=is_stuck_exploring,
                     searching=is_stuck_searching)
            return config.plan_model  # Opus — for breakthrough reasoning

    # ── SIGNAL 2: Writing code → Sonnet ───────────────────────────────────
    if has_written or modified_files:
        return config.write_model  # Sonnet for code gen + test analysis

    # ── SIGNAL 3: Enough exploration done, ready to plan → Sonnet ─────────
    explore_calls = [tc for tc in tool_log if tc.tool_name in _EXPLORE_TOOLS]
    if len(explore_calls) >= 3 and _has_useful_exploration(tool_log):
        return config.write_model  # Sonnet for planning + writing

    # ── SIGNAL 4: Early exploration → Sonnet (not Haiku) ──────────────────
    # Use Sonnet even for early exploration — stronger reasoning for vague tickets
    if n_tools < 6:
        return config.explore_model  # Sonnet (explore_model = Sonnet now)

    # ── Default: Sonnet ───────────────────────────────────────────────────
    return config.write_model


def _get_repo_path(repo_id: str) -> Path:
    slug = repo_id.replace("/", "_")
    cache = os.environ.get("REPO_CACHE_DIR", "/home/eeshu/Desktop/context/repos")
    return Path(cache) / slug


def _build_implementation(
    ticket_id: str,
    repo_id: str,
    modified_files: dict[str, str],
    original_files: dict[str, str],
    repo_path: Path,
    model: str,
    plan_summary: str = "Agent loop implementation",
) -> Implementation:
    """Convert virtual filesystem changes into an Implementation object for L7."""
    import difflib

    file_results = []
    total_diff_chars = 0

    for fp, new_content in modified_files.items():
        original = original_files.get(fp, "")
        if new_content == original:
            continue

        change_type = "create" if not original else "modify"

        # Measure actual diff size (not file size)
        if original:
            diff_lines = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                n=0,
            ))
            diff_size = sum(len(l) for l in diff_lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---")))
        else:
            diff_size = len(new_content)  # new file: entire content is the diff

        total_diff_chars += diff_size

        file_results.append(FileResult(
            file_path=fp,
            change_type=change_type,
            original_content=original,
            modified_content=new_content,
            explanation="Modified by agent loop",
        ))

    if total_diff_chars > 50_000:
        log.warning("implementation.large_diff", total_diff_chars=total_diff_chars, files=len(file_results))

    return Implementation(
        ticket_id=ticket_id,
        repo_id=repo_id,
        plan_summary=plan_summary,
        file_results=file_results,
        model_used=model,
    )


def _build_exploration_cache(tool_log: list[ToolCallRecord], modified_files: dict[str, str]) -> ExplorationCache:
    """Extract reusable exploration data from agent's tool log."""
    cache = ExplorationCache()
    cache.files_modified = dict(modified_files)

    for tc in tool_log:
        result = tc.result if isinstance(tc.result, dict) else {}

        if tc.tool_name == "read_file":
            path = tc.args.get("file_path", "")
            lines = result.get("total_lines", 0)
            if path and "error" not in result:
                cache.files_read[path] = f"{lines} lines"

        elif tc.tool_name == "search_symbols":
            for s in result.get("symbols", []):
                name = s.get("name", s) if isinstance(s, dict) else str(s)
                if name not in cache.symbols_found:
                    cache.symbols_found.append(name)

        elif tc.tool_name == "get_diff":
            diff = result.get("diff", "")
            if diff:
                cache.diff_summary = diff[:2000]

        # Build tool summary
        args_short = ", ".join(f"{k}={v}" for k, v in list(tc.args.items())[:2])
        cache.tool_summaries.append(f"{tc.tool_name}({args_short})")

    return cache


def run_agent(
    ticket: Ticket,
    bundle: ContextBundle,
    config: AgentConfig,
    feedback: str | None = None,
    feedback_images: list[dict] | None = None,
    prev_cache: ExplorationCache | None = None,
) -> AgentResult:
    """
    Run the graph-powered ReAct agent loop using Claude API.

    1. Builds system prompt with ticket + context map
    2. Loops: Claude decides tool → execute → result back → repeat
    3. Returns Implementation compatible with L7 publisher
    """
    repo_path = _get_repo_path(ticket.repo_id)
    clear_tool_cache()  # Fresh cache for each pipeline run
    api_key = config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    if not api_key:
        return AgentResult(
            ticket_id=ticket.ticket_id,
            repo_id=ticket.repo_id,
            implementation=Implementation(
                ticket_id=ticket.ticket_id,
                repo_id=ticket.repo_id,
                plan_summary="",
                file_results=[],
                model_used=config.model,
            ),
            success=False,
            error="ANTHROPIC_API_KEY not set",
        )

    client = anthropic.Anthropic(api_key=api_key)
    system_prompt = build_system_prompt(ticket, bundle, feedback=feedback, test_cmd_hint=config.test_cmd_hint)

    # Inject exploration cache into system prompt (avoids re-exploration on retry)
    if prev_cache:
        cache_prompt = prev_cache.to_prompt()
        if cache_prompt:
            system_prompt += f"\n\n{cache_prompt}"
            log.info("agent.cache_injected", files=len(prev_cache.files_read), symbols=len(prev_cache.symbols_found))

    # Conversation history (Claude format)
    if feedback_images:
        # Multimodal first message: text + reviewer screenshots
        first_msg_content: list[dict] = [
            {"type": "text", "text": "Implement the ticket above. This is a RETRY — study the previous attempt and reviewer screenshots carefully before starting."},
        ]
        for img in feedback_images:
            first_msg_content.append(img)
            first_msg_content.append({"type": "text", "text": "Above: screenshot from reviewer showing the issue."})
        messages: list[dict] = [{"role": "user", "content": first_msg_content}]
    elif feedback:
        messages: list[dict] = [
            {"role": "user", "content": "Implement the ticket above. This is a RETRY — the previous attempt was rejected. Study the feedback in the system prompt, then explore and fix it properly."}
        ]
    else:
        messages: list[dict] = [
            {"role": "user", "content": "Implement the ticket above. Start by exploring the codebase using the graph tools."}
        ]

    # Agent state
    modified_files: dict[str, str] = {}
    original_files: dict[str, str] = {}
    tool_log: list[ToolCallRecord] = []
    iteration = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    compression_count = 0
    plan_summary = "Agent loop implementation"
    repair_count = 0  # Tracks test-fail-fix cycles (max config.max_repair_iterations)
    edit_history = EditHistory()  # C6: checkpoint/rollback for hypothesis-based fixing
    _new_file_warned: set[str] = set()  # Track which new files got coupled-files warning
    triage = TriageState()  # Persistent test triage state (survives compression)

    log.info("agent.start", ticket_id=ticket.ticket_id, repo_id=ticket.repo_id, model=config.model)

    # ── Docker Sandbox: disposable per-run execution environment ────────────
    sandbox = None
    if config.use_sandbox:
        from layer45_agent.sandbox import Sandbox, should_use_sandbox
        if should_use_sandbox():
            sandbox = Sandbox(repo_path, run_id=ticket.ticket_id[:8], repo_id=ticket.repo_id)
            start_result = sandbox.start()
            if start_result.get("status") in ("started", "already_running"):
                log.info("agent.sandbox.ready", container=start_result.get("container"),
                         image=start_result.get("image"))
                # Pre-install deps in sandbox so first run_tests doesn't fail
                env_result = sandbox.setup_environment()
                log.info("agent.sandbox.env", status=env_result.get("status"),
                         method=env_result.get("method", ""))
            else:
                log.warning("agent.sandbox.failed", error=start_result.get("error", ""))
                sandbox = None  # Fall back to host execution

    # ── Trace: capture full pipeline for debugging ──────────────────────────
    trace.start_trace(ticket.ticket_id, ticket.repo_id, config.model)
    trace.log_system_prompt(system_prompt)
    trace.log_initial_message(messages)

    # ── ReAct Loop ──────────────────────────────────────────────────────────
    while iteration < config.max_iterations:
        iteration += 1
        turn_start = time.time()
        log.info("agent.iteration", n=iteration, prompt_tokens=total_prompt_tokens)

        # Pick model based on current phase
        current_model = _pick_model(config, tool_log, modified_files, repair_count)
        if iteration == 1 or (iteration > 1 and current_model != _pick_model(config, tool_log[:-1] if tool_log else [], modified_files, repair_count)):
            log.info("agent.model_switch", model=current_model, iteration=iteration)

        # Problem 2+3: Restrict tools when finish gate active or Tier 3 nudge
        allowed_tools = triage.get_allowed_tools()
        if allowed_tools is not None:
            current_tools = [t for t in ALL_TOOL_DEFS if t.get("name", t.get("function", {}).get("name", "")) in allowed_tools]
            log.info("agent.tools_restricted", allowed=list(allowed_tools), iteration=iteration)
        else:
            current_tools = ALL_TOOL_DEFS

        # Call Claude with retry on rate limit (429) and overload (529)
        response = None
        for attempt in range(4):
            try:
                response = client.messages.create(
                    model=current_model,
                    max_tokens=config.max_output_tokens_per_turn,
                    system=[{
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }],
                    messages=messages,
                    tools=current_tools,
                    temperature=config.temperature,
                )
                break
            except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
                wait = 30.0 * (attempt + 1)
                log.warning("agent.rate_limited", attempt=attempt + 1, wait=f"{wait:.0f}s", error=str(e)[:100])
                time.sleep(wait)
                continue
            except anthropic.APIError as e:
                if e.status_code in (429, 529, 503):
                    wait = 30.0 * (attempt + 1)
                    log.warning("agent.rate_limited", attempt=attempt + 1, wait=f"{wait:.0f}s")
                    time.sleep(wait)
                    continue
                log.error("agent.api_error", error=str(e), iteration=iteration)
                return AgentResult(
                    ticket_id=ticket.ticket_id,
                    repo_id=ticket.repo_id,
                    implementation=_build_implementation(
                        ticket.ticket_id, ticket.repo_id,
                        modified_files, original_files, repo_path, config.model, plan_summary,
                    ),
                    tool_calls=tool_log,
                    iterations=iteration,
                    total_prompt_tokens=total_prompt_tokens,
                    total_completion_tokens=total_completion_tokens,
                    success=False,
                    error=f"Claude API error: {e}",
                )

        if response is None:
            log.error("agent.rate_limit_exhausted", iteration=iteration)
            return AgentResult(
                ticket_id=ticket.ticket_id,
                repo_id=ticket.repo_id,
                implementation=_build_implementation(
                    ticket.ticket_id, ticket.repo_id,
                    modified_files, original_files, repo_path, config.model, plan_summary,
                ),
                tool_calls=tool_log,
                iterations=iteration,
                total_prompt_tokens=total_prompt_tokens,
                total_completion_tokens=total_completion_tokens,
                success=False,
                error="Rate limit exhausted after 4 attempts",
            )

        # Track tokens
        if response.usage:
            total_prompt_tokens += response.usage.input_tokens or 0
            total_completion_tokens += response.usage.output_tokens or 0

        # Append assistant response to history
        messages.append({"role": "assistant", "content": response.content})

        # Check for tool use blocks
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            # No tools called — check if model wants to stop
            log.info("agent.no_tools", iteration=iteration, stop_reason=response.stop_reason)
            if response.stop_reason == "end_turn":
                break
            continue

        # Execute each tool call
        tool_results = []
        finished = False

        for block in tool_use_blocks:
            tool_name = block.name
            tool_args = block.input if isinstance(block.input, dict) else {}
            log.info("agent.tool_call", tool=tool_name, args_keys=list(tool_args.keys()))

            # C6: Checkpoint before first write_file (for rollback if fix fails)
            if tool_name == "write_file" and not edit_history.has_checkpoint:
                edit_history.checkpoint(modified_files, original_files)
                log.info("agent.checkpoint", attempt=edit_history.attempt_count)

            result = execute_tool(
                name=tool_name,
                args=tool_args,
                repo_path=repo_path,
                repo_id=ticket.repo_id,
                modified_files=modified_files,
                original_files=original_files,
                sandbox=sandbox,
            )

            # Auto-fix: if run_tests hits infrastructure error, install deps and retry
            if (tool_name == "run_tests"
                and isinstance(result, dict)
                and result.get("infrastructure_error")):
                log.info("agent.auto_setup_env", reason="infrastructure_error")
                setup_result = execute_tool(
                    name="setup_environment",
                    args={},
                    repo_path=repo_path,
                    repo_id=ticket.repo_id,
                    modified_files=modified_files,
                    original_files=original_files,
                    sandbox=sandbox,
                )
                if isinstance(setup_result, dict) and setup_result.get("status") in ("installed", "partial"):
                    # Re-run the failed test — agent gets working results, never sees infra error
                    result = execute_tool(
                        name=tool_name,
                        args=tool_args,
                        repo_path=repo_path,
                        repo_id=ticket.repo_id,
                        modified_files=modified_files,
                        original_files=original_files,
                        sandbox=sandbox,
                    )
                    log.info("agent.auto_setup_env.retry", test_status=result.get("test_status"))

            tool_log.append(ToolCallRecord(
                iteration=iteration,
                tool_name=tool_name,
                args=tool_args,
                result=result,
                timestamp=time.time(),
            ))

            # ── Exploration tracking: record tool usage for efficiency metrics ──
            triage.record_tool_usage(tool_name, result if isinstance(result, dict) else None)

            # ── Phase tracking: update triage state on key tool calls ──
            if tool_name == "write_file" and isinstance(result, dict) and result.get("success"):
                triage.on_write_file(tool_args.get("file_path", ""), iteration)
            elif tool_name in ("run_tests", "run_command") and isinstance(result, dict):
                exit_code = result.get("exit_code", 1) if tool_name == "run_command" else (
                    0 if result.get("test_status") == "passed" else 1)
                if triage.phase in (PHASE_WRITING, PHASE_VERIFYING):
                    triage.on_test_or_command_after_write(exit_code, iteration)

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

            if tool_name == "finish":
                # Gate: require build_check for TS/JS repos before finishing
                has_ts_files = any(
                    fp.endswith((".ts", ".tsx", ".js", ".jsx"))
                    for fp in modified_files.keys()
                )
                ran_build = any(tc.tool_name == "build_check" for tc in tool_log[:-1])
                build_ok = any(
                    tc.tool_name == "build_check"
                    and isinstance(tc.result, dict)
                    and tc.result.get("status") in ("success", "skipped")
                    for tc in tool_log[:-1]
                )
                if has_ts_files and not ran_build:
                    # Never ran build_check → block
                    override = {
                        "acknowledged": False,
                        "error": "BLOCKED: You modified TypeScript/JS files but never called build_check. "
                                 "Run build_check to verify your imports compile, then call finish again.",
                    }
                    tool_results[-1]["content"] = json.dumps(override, default=str)
                    log.warning("agent.finish_blocked", reason="no_build_check_for_ts")
                elif has_ts_files and ran_build and not build_ok:
                    # build_check ran but FAILED → block until fixed
                    override = {
                        "acknowledged": False,
                        "error": "BLOCKED: build_check FAILED. Fix the compilation errors first, then call finish again.",
                    }
                    tool_results[-1]["content"] = json.dumps(override, default=str)
                    log.warning("agent.finish_blocked", reason="build_check_failed")
                else:
                    finished = True
                    plan_summary = result.get("summary", plan_summary)

        # Send tool results back
        messages.append({"role": "user", "content": tool_results})

        # ── Trace: log this complete turn ───────────────────────────────────
        if trace.is_enabled():
            assistant_text = ""
            trace_tool_calls = []
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    assistant_text += block.text
                elif block.type == "tool_use":
                    trace_tool_calls.append({
                        "tool": block.name,
                        "args": block.input if isinstance(block.input, dict) else {},
                    })
            trace_tool_results = []
            for tc in tool_log[-len(tool_use_blocks):]:
                trace_tool_results.append({
                    "tool": tc.tool_name,
                    "result_preview": str(tc.result)[:1000],
                })
            usage = response.usage if response.usage else None
            trace.log_turn(
                iteration=iteration,
                model=current_model,
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                assistant_text=assistant_text,
                tool_calls=trace_tool_calls,
                tool_results=trace_tool_results,
                stop_reason=response.stop_reason or "",
                duration_ms=int((time.time() - turn_start) * 1000),
            )

        # Per-phase iteration limits (hard enforcement)
        if not finished:
            has_written_any = any(tc.tool_name == "write_file" for tc in tool_log)
            has_tested = any(tc.tool_name == "run_tests" for tc in tool_log)

            # ── Baseline recording: if run_tests was called before write_file ──
            # This captures the "before" state so we can compare after changes.
            if has_tested and not has_written_any and triage.baseline_signature is None:
                baseline_tests = [
                    tc for tc in tool_log
                    if tc.tool_name == "run_tests" and isinstance(tc.result, dict)
                ]
                if baseline_tests:
                    bt = baseline_tests[-1]
                    baseline_output = str(bt.result.get("output", ""))
                    baseline_exit = 0 if bt.result.get("test_status") == "passed" else 1
                    triage.record_baseline(baseline_output, baseline_exit)
                    if triage.baseline_status == "failed":
                        messages.append({
                            "role": "user",
                            "content": f"BASELINE RECORDED: Tests were ALREADY failing before your changes. "
                                       f"If you see the same failure after your code change, it's pre-existing — "
                                       f"not caused by your fix. Verify your fix with a standalone test if needed.",
                        })

            # Count explore-only iterations (iterations where ALL tools were explore tools)
            explore_iters = 0
            iter_tools: dict[int, set[str]] = {}
            for tc in tool_log:
                iter_tools.setdefault(tc.iteration, set()).add(tc.tool_name)
            for it, tools in iter_tools.items():
                if tools.issubset(_EXPLORE_TOOLS):
                    explore_iters += 1

            # ── Escalating nudges (replaces old soft nudges) ──────────────
            # Problem 2: Nudges escalate from soft → firm → directive
            # Problem 3: At finish gate, tools are restricted
            if triage.finish_gate_active:
                # Finish gate: verification passed, agent should finish NOW
                log.info("agent.finish_gate", iteration=iteration, phase=triage.phase)
                messages.append({
                    "role": "user",
                    "content": "VERIFICATION PASSED: Your standalone test passed. Your fix is correct.\n"
                               "Execute NOW:\n"
                               "  1. get_diff()\n"
                               "  2. finish(summary='...')\n"
                               "Do not explore further.",
                })
            elif has_written_any:
                # Escalating nudge based on triage state
                nudge_msg = triage.get_nudge_message(iteration)
                if nudge_msg:
                    tier = "soft" if triage.nudge_count <= 4 else "firm" if triage.nudge_count <= 9 else "directive"
                    log.info("agent.nudge", iteration=iteration, tier=tier, count=triage.nudge_count)
                    messages.append({"role": "user", "content": nudge_msg})
            elif not has_written_any and explore_iters >= config.max_explore_iterations:
                # Pre-write: gentle exploration nudge (unchanged)
                log.info("agent.nudge.explore", iteration=iteration, explore_iters=explore_iters)
                messages.append({
                    "role": "user",
                    "content": f"You've explored for {explore_iters} iterations. If you have enough context, consider writing your fix.",
                })

            # ── Exploration efficiency guidance (fires once) ──────────────
            exploration_tip = triage.get_exploration_guidance()
            if exploration_tip:
                log.info("agent.exploration_guidance",
                         semantic=triage.semantic_search_count,
                         structural=triage.structural_tool_count,
                         iteration=iteration)
                messages.append({"role": "user", "content": exploration_tip})

            # Check if last write had unverified import warnings
            if has_written_any:
                last_writes = [tc for tc in tool_log if tc.tool_name == "write_file" and isinstance(tc.result, dict)]
                if last_writes:
                    last_write = last_writes[-1]
                    edits = last_write.result.get("edits", [])
                    has_import_warning = any(
                        isinstance(e, dict) and "new_imports_detected" in e
                        for e in edits
                    )
                    verified_since = any(
                        tc.tool_name in ("search_code", "search_symbols")
                        and tc.timestamp > last_write.timestamp
                        for tc in tool_log
                    )
                    if has_import_warning and not verified_since:
                        flat_imports = []
                        for e in edits:
                            if isinstance(e, dict) and "new_imports_detected" in e:
                                flat_imports.extend(e["new_imports_detected"])
                        if flat_imports:
                            messages.append({
                                "role": "user",
                                "content": f"WARNING: You added new imports ({', '.join(flat_imports[:5])}) but haven't verified they exist. "
                                           f"Call search_code to confirm each import is a real export before calling finish.",
                            })

            # ── C6: Checkpoint on first write ──────────────────────────────
            # Save clean state before the agent's first edit so we can rollback
            if has_written_any and not edit_history.has_checkpoint:
                edit_history.checkpoint(modified_files, original_files)
                log.info("agent.hypothesis.checkpoint", attempt=edit_history.attempt_count)

            # ── Test Triage: classify failures, detect stuck loops ──────────
            if has_written_any and has_tested:
                all_tests = [tc for tc in tool_log if tc.tool_name == "run_tests"]
                last_test = all_tests[-1]
                already_triaged = len(all_tests) <= triage.total_test_runs
                last_test_failed = (
                    isinstance(last_test.result, dict)
                    and last_test.result.get("test_status") in ("failed", "error")
                )

                if last_test_failed and not already_triaged:
                    repair_count += 1
                    test_output = str(last_test.result.get("output", ""))
                    infra_flag = last_test.result.get("infrastructure_error", False)

                    verdict = classify_test_result(
                        output=test_output,
                        exit_code=1,
                        changed_files=list(modified_files.keys()),
                        baseline_sig=triage.baseline_signature,
                        error_history=triage.error_history,
                        infra_flag=infra_flag,
                    )
                    triage.record_verdict(verdict)

                    log.info("agent.test_triage",
                             repair=repair_count,
                             category=verdict.category,
                             is_agent_fault=verdict.is_agent_fault,
                             signature=verdict.error_signature[:8],
                             total_runs=triage.total_test_runs)

                    # ── C6: Rollback on 2nd failure — try a different approach ──
                    # If repair_count == 2 and we have a checkpoint, the current
                    # approach isn't working. Rollback to clean state and tell
                    # the agent to try something fundamentally different.
                    if repair_count == 2 and edit_history.has_checkpoint:
                        rolled_back = edit_history.rollback(modified_files, original_files, repo_path)
                        if rolled_back:
                            log.info("agent.hypothesis.rollback", attempt=edit_history.attempt_count)
                            # Save new checkpoint for the next attempt
                            edit_history.checkpoint(modified_files, original_files)
                            failure_summary = edit_history.get_failure_summary()
                            messages.append({
                                "role": "user",
                                "content": (
                                    "ROLLBACK: Your previous fix approach failed twice with the same error. "
                                    "All your edits have been REVERTED to the original code.\n\n"
                                    f"{failure_summary}\n\n"
                                    "Now try a COMPLETELY DIFFERENT approach:\n"
                                    "1. Re-read the failing test to understand what it actually expects\n"
                                    "2. Think of an alternative fix strategy (different function, different logic)\n"
                                    "3. Write the new fix with write_file\n"
                                    "4. Run tests again to verify"
                                ),
                            })
                            # Reset repair count for the new attempt
                            repair_count = 0
                            continue  # Skip the normal repair message

                    # Normal repair path
                    repair_msg = triage.get_repair_message(verdict, repair_count)
                    messages.append({
                        "role": "user",
                        "content": repair_msg,
                    })

            # Coupled-files injection: when agent creates a NEW file, remind to check registrations
            if has_written_any:
                for tc in tool_log:
                    if (tc.tool_name == "write_file"
                        and isinstance(tc.result, dict)
                        and tc.result.get("success")
                        and tc.args.get("file_path")
                        and tc.args["file_path"] not in _new_file_warned):
                        fp = tc.args["file_path"]
                        # Check if this was a new file (not in original_files or original was empty)
                        if not original_files.get(fp, "").strip():
                            _new_file_warned.add(fp)
                            messages.append({
                                "role": "user",
                                "content": f"You created a new file: {fp}. Call get_coupled_files on the parent "
                                           f"directory to check if you need to register this module "
                                           f"(e.g., in __init__.py, barrel exports, or config files).",
                            })

        if finished:
            log.info("agent.finished", iteration=iteration, files=len(modified_files))
            break

        # Safety checks — token budget is a soft warning now, not a hard stop
        if check_token_budget(total_prompt_tokens, config.max_total_tokens):
            log.warning("agent.token_budget_warning", tokens=total_prompt_tokens)
            # Don't break — just log. Compression will handle context growth.

        # Oscillation detection — nudge, don't force-stop
        if detect_oscillation(tool_log):
            log.warning("agent.oscillation_detected", iteration=iteration)
            has_written_any = any(tc.tool_name == "write_file" for tc in tool_log)
            if has_written_any:
                messages.append({
                    "role": "user",
                    "content": "You seem to be repeating actions. Consider: call get_diff() to review your changes, then either fix the remaining issue or call finish().",
                })
            else:
                messages.append({
                    "role": "user",
                    "content": "You are repeating similar searches. Try a different approach: "
                               "use a different search query, try get_risk_score() to prioritize files, "
                               "or use get_callers()/get_impact() to explore from a different angle. "
                               "If you have enough context, write your fix with write_file.",
                })

        # Turn-limit — soft warning at 10 turns before max (no forced stop)
        remaining = config.max_iterations - iteration
        if remaining == 10 and not finished:
            log.info("agent.turn_limit_approaching", iteration=iteration, remaining=remaining)
            messages.append({
                "role": "user",
                "content": f"Note: {remaining} turns remaining. No rush — but start converging toward a solution when ready.",
            })

        # History compression (triggers earlier at 80K, structured summaries)
        if total_prompt_tokens > config.compression_threshold:
            log.info("agent.compressing_history", tokens=total_prompt_tokens, count=compression_count)
            trace.log_event("compression", {"tokens_before": total_prompt_tokens, "compression_count": compression_count})
            messages, compression_count = compress(messages, compression_count=compression_count)

            # Challenge E: Inject triage state after compression so agent
            # never loses awareness of baseline and error patterns
            triage_summary = triage.to_prompt()
            if triage_summary.strip():
                messages.append({
                    "role": "user",
                    "content": triage_summary,
                })

    # ── Post-loop ───────────────────────────────────────────────────────────
    # Destroy sandbox container (disposable — one per run)
    if sandbox:
        sandbox.stop()
        log.info("agent.sandbox.destroyed")

    # Flush all changes to disk
    for fp, content in modified_files.items():
        abs_path = repo_path / fp
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(content, encoding="utf-8")

    impl = _build_implementation(
        ticket.ticket_id, ticket.repo_id,
        modified_files, original_files, repo_path, config.model, plan_summary,
    )

    log.info(
        "agent.done",
        ticket_id=ticket.ticket_id,
        iterations=iteration,
        files_changed=len(impl.file_results),
        tool_calls=len(tool_log),
        prompt_tokens=total_prompt_tokens,
        completion_tokens=total_completion_tokens,
    )

    # ── Trace: finalize and write to disk ───────────────────────────────────
    trace_path = trace.finish_trace(
        iterations=iteration,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        files_changed=[fr.file_path for fr in impl.file_results],
        success=True,
    )
    if trace_path:
        log.info("agent.trace_written", path=trace_path)

    # Build exploration cache for potential retry (avoids re-exploration)
    cache = _build_exploration_cache(tool_log, modified_files)

    return AgentResult(
        ticket_id=ticket.ticket_id,
        repo_id=ticket.repo_id,
        implementation=impl,
        tool_calls=tool_log,
        iterations=iteration,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        success=True,
        exploration_cache=cache,
    )
