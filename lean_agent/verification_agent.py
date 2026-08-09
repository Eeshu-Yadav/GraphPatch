"""
Independent Verification Sub-Agent — adapted from Claude Code's VERIFICATION_AGENT.

A full agent (multi-turn conversation with tools) that runs AFTER the builder
agent finishes. It has READ-ONLY access to the repo and can run tests, but
CANNOT modify any files. Its job is to try to BREAK the fix, not confirm it works.

Architecture (mirrors Claude Code verificationAgent.ts):
  - Separate conversation, fresh context (no shared memory with builder)
  - Adversarial stance ("try to break it")
  - Anti-rationalization prompts ("reading is not verification — run it")
  - Can run: read_file, search_code, run_command, run_tests, list_directory
  - Cannot run: write_file, find_files, done_exploring, finish
  - Can write ephemeral test scripts to /tmp
  - Returns VERDICT: PASS / FAIL / PARTIAL with command-backed evidence
  - Max 15 turns (verification should be focused)

Usage:
    from lean_agent.verification_agent import run_verification

    verdict, report = run_verification(
        bug_description="...",
        files_changed=["django/contrib/staticfiles/storage.py"],
        approach_summary="Changed post_process to deduplicate yields",
        repo_path="/path/to/repo",
        repo_id="django/django",
    )
    # verdict: "PASS" | "FAIL" | "PARTIAL"
    # report: full verification report with command output evidence
"""
from __future__ import annotations

from pathlib import Path
import json
import os
import re

import structlog
import anthropic

log = structlog.get_logger(__name__)


# ── System prompt ─────────────────────────────────────────────────────────────
# Adapted directly from Claude Code's verificationAgent.ts with modifications
# for our bug-fix context (no browser/frontend, focused on Python/Django).

VERIFIER_SYSTEM_PROMPT = """\
You are a verification specialist. Your job is not to confirm the implementation \
works — it's to try to break it.

You have two documented failure patterns. First, verification avoidance: when \
faced with a check, you find reasons not to run it — you read code, narrate \
what you would test, write "PASS," and move on. Second, being seduced by the \
first 80%: you see a plausible change and feel inclined to pass it, not noticing \
the edge cases, regressions, or subtle breakages. The first 80% is easy. Your \
entire value is in finding the last 20%.

=== CRITICAL: DO NOT MODIFY THE PROJECT ===
You are STRICTLY PROHIBITED from:
- Creating, modifying, or deleting any files IN THE PROJECT DIRECTORY
- Running git write operations (add, commit, push, reset)

You MAY write ephemeral test scripts to /tmp via run_command when inline \
commands aren't sufficient. Clean up after yourself.

=== WHAT YOU RECEIVE ===
You will receive:
  1. The original bug description
  2. List of files changed by the builder agent
  3. The builder's approach/summary
  4. The repo is on disk with the builder's changes already applied

=== VERIFICATION STRATEGY FOR BUG FIXES ===
1. Reproduce the original bug — run a test or command that would trigger it
2. Verify the fix — does the same test/command now pass?
3. Run regression tests — run the existing tests in the same test file/module
4. Check for side effects — look at related functionality the change might break

=== REQUIRED STEPS ===
1. Read the changed files to understand WHAT was modified
2. Find the existing test file for this module (search_code or list_directory)
3. Run the existing tests — failing tests = automatic FAIL
4. If possible, write an ephemeral reproduction test in /tmp and run it
5. Check for regressions in related code

Test suite results are context, not evidence. The implementer is an LLM too — \
its tests may be heavy on mocks or happy-path coverage. Verify independently.

=== RECOGNIZE YOUR OWN RATIONALIZATIONS ===
- "The code looks correct based on my reading" — reading is not verification. Run it.
- "The implementer's tests already pass" — the implementer is an LLM. Verify independently.
- "This is probably fine" — probably is not verified. Run it.
- "This would take too long" — not your call.
If you catch yourself writing an explanation instead of a command, stop. Run the command.

=== OUTPUT FORMAT ===
Every check MUST follow this structure. A check without a Command run block \
is not a PASS — it's a skip.

### Check: [what you're verifying]
**Command run:** [exact command]
**Output observed:** [actual output, not paraphrased]
**Result: PASS** (or FAIL — with Expected vs Actual)

End with EXACTLY one line:
  VERDICT: PASS
  VERDICT: FAIL — <one-sentence reason>
  VERDICT: PARTIAL — <what couldn't be verified and why>

PARTIAL is for environmental limitations only (no test framework, tool unavailable) \
— not for "I'm unsure." If you can run the check, you must decide PASS or FAIL.
"""


# ── Verification tools (READ-ONLY subset) ────────────────────────────────────
# These match the lean agent's tool schemas but exclude all write operations.

VERIFIER_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file's contents. Use start_line/end_line for large files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to the file"},
                "start_line": {"type": "integer", "description": "Start line (optional)"},
                "end_line": {"type": "integer", "description": "End line (optional)"},
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "search_code",
        "description": "Search file contents with regex (like grep).",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern"},
                "file_glob": {"type": "string", "description": "File pattern filter"},
                "max_results": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "run_command",
        "description": "Run a shell command. Use for running tests, checking output. Timeout: 120s.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "run_tests",
        "description": "Run the project's test suite. Auto-detects test framework.",
        "input_schema": {
            "type": "object",
            "properties": {
                "test_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific test files to run (optional)",
                },
            },
        },
    },
    {
        "name": "list_directory",
        "description": "List files in a directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"},
                "pattern": {"type": "string", "description": "Filter pattern (optional)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "verdict",
        "description": (
            "Submit your final verification verdict. Call this when done checking. "
            "verdict must be PASS, FAIL, or PARTIAL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["PASS", "FAIL", "PARTIAL"],
                    "description": "Your verification verdict",
                },
                "report": {
                    "type": "string",
                    "description": "Full report with command-backed evidence for each check",
                },
                "failures": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of specific failures found (if FAIL)",
                },
            },
            "required": ["verdict", "report"],
        },
    },
]

# Tools the verifier is NOT allowed to use (safety: don't modify the project)
_DISALLOWED_FOR_VERIFIER = frozenset([
    "write_file", "find_files", "file_outline", "done_exploring", "finish",
    "get_diff", "get_impact", "get_callers", "get_dependencies",
    "get_test_coverage", "get_risk_score",
])


# ── Main verification loop ───────────────────────────────────────────────────

def run_verification(
    bug_description: str,
    files_changed: list[str],
    approach_summary: str,
    repo_path: str,
    repo_id: str,
    api_key: str | None = None,
    model: str = "claude-sonnet-4-20250514",
    max_turns: int = 15,
) -> tuple[str, str, list[dict]]:
    """
    Run the independent verification sub-agent.

    Returns:
        (verdict, report, tool_log)
        verdict: "PASS" | "FAIL" | "PARTIAL" | "ERROR"
        report: full verification report text
        tool_log: list of {turn, tool, args_keys} for tracing
    """
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    # Build the initial message with all context the verifier needs
    files_text = "\n".join(f"  - {f}" for f in files_changed) if files_changed else "  (no files reported)"
    initial_message = (
        f"## Bug Description\n{bug_description[:3000]}\n\n"
        f"## Files Changed by Builder\n{files_text}\n\n"
        f"## Builder's Approach\n{approach_summary[:1000]}\n\n"
        f"## Your Task\n"
        f"Verify this bug fix is correct. The repo at {repo_path} has the "
        f"builder's changes already applied. Read the changed files, run the "
        f"existing tests, try to reproduce the original bug, and check for "
        f"regressions. End by calling the verdict tool with PASS, FAIL, or PARTIAL."
    )

    messages = [{"role": "user", "content": initial_message}]
    tool_log: list[dict] = []
    total_tokens = 0
    verdict = "ERROR"
    report = ""

    log.info("verifier.start", files=len(files_changed), repo=repo_id)
    print(f"  🔍 VERIFIER starting ({max_turns} max turns)", flush=True)

    for turn in range(max_turns):
        turn_num = turn + 1
        log.info("verifier.turn", n=turn_num)

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=VERIFIER_SYSTEM_PROMPT,
            messages=messages,
            tools=VERIFIER_TOOLS,
            temperature=0.0,
        )

        usage = response.usage
        total_tokens += usage.input_tokens + usage.output_tokens

        assistant_content = response.content
        messages.append({"role": "assistant", "content": assistant_content})

        tool_results = []
        done = False

        for block in assistant_content:
            if block.type != "tool_use":
                continue

            tool_name = block.name
            tool_input = block.input
            tool_log.append({"turn": turn_num, "tool": tool_name})
            print(f"  🔍 [{turn_num}] {tool_name}", flush=True)

            # Handle the verdict tool — verification complete
            if tool_name == "verdict":
                verdict = tool_input.get("verdict", "ERROR")
                report = tool_input.get("report", "")
                failures = tool_input.get("failures", [])
                log.info("verifier.verdict",
                         verdict=verdict, failures=len(failures), turn=turn_num)
                print(f"  🔍 VERDICT: {verdict}", flush=True)
                result = {"acknowledged": True, "verdict": verdict}
                done = True

            # Safety: block any write operations that somehow get through
            elif tool_name in _DISALLOWED_FOR_VERIFIER:
                result = {
                    "error": f"BLOCKED: {tool_name} is not allowed for the verifier. "
                             f"You are read-only. Use read_file, search_code, "
                             f"run_command, run_tests, list_directory."
                }

            # Execute read-only tools via the standard tool dispatcher
            else:
                from layer45_agent.tools import execute_tool
                result = execute_tool(
                    name=tool_name,
                    args=tool_input,
                    repo_path=Path(repo_path),
                    repo_id=repo_id,
                    modified_files={},      # empty — verifier reads from disk
                    original_files={},
                    sandbox=None,
                )

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if done:
            break

        # If the model stopped without calling verdict, try to parse from text
        if response.stop_reason == "end_turn":
            last_text = ""
            for block in assistant_content:
                if hasattr(block, "text"):
                    last_text = block.text
            match = re.search(r"VERDICT:\s*(PASS|FAIL|PARTIAL)", last_text, re.IGNORECASE)
            if match:
                verdict = match.group(1).upper()
                report = last_text
                log.info("verifier.verdict_from_text", verdict=verdict, turn=turn_num)
                print(f"  🔍 VERDICT (from text): {verdict}", flush=True)
            else:
                verdict = "PARTIAL"
                report = f"Verifier ended without explicit verdict after {turn_num} turns."
                log.info("verifier.no_verdict", turn=turn_num)
            break

    log.info("verifier.done",
             verdict=verdict, turns=turn_num, tokens=total_tokens,
             tool_calls=len(tool_log))

    return verdict, report, tool_log
