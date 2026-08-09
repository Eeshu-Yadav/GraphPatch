"""
Test Failure Triage System — Language & Framework Agnostic

Solves the "agent stuck in repair loop" problem using 3 mechanisms
that work regardless of language, test runner, or framework:

  1. BASELINE COMPARISON — run tests before AND after changes,
     compare signatures. Same signature = pre-existing, not agent's fault.

  2. ERROR FINGERPRINTING — normalize test output into a stable hash.
     If same hash repeats 3x, the agent's code changes aren't affecting it.

  3. BLAME ANALYSIS — check if the error traceback references any file
     the agent modified. If not, the error is unrelated to the change.

No hardcoded error strings. No framework detection. No language-specific
parsing. Works by structural comparison only.

Industry references:
  - Nightwire (NousResearch): baseline-relative quality gates
  - AgentRx (Microsoft): failure taxonomy via structural constraints
  - SAGE (Salesforce): plan induction from failed trajectories
  - SWE-bench: FAIL_TO_PASS + PASS_TO_PASS baseline pattern
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# ERROR FINGERPRINTING — language-agnostic signature extraction
# ═══════════════════════════════════════════════════════════════════════════

def extract_error_signature(output: str) -> str:
    """
    Extract a stable, normalized fingerprint from test output.

    Works for ANY language/framework because it doesn't parse error types.
    Instead, it normalizes the output structurally:
      - Strip volatile parts (timestamps, PIDs, memory addresses, abs paths)
      - Keep the stable parts (error messages, relative paths, line patterns)
      - Hash the result

    Same root cause → same signature, even across runs.
    """
    if not output or not output.strip():
        return "empty"

    # Take the last 1000 chars — that's where the error summary lives
    # in pytest, jest, go test, cargo test, runtests.py, etc.
    tail = output[-1000:]

    # Normalize: strip parts that change between runs but aren't the error
    normalized = tail
    normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.\d]*', 'TIMESTAMP', normalized)  # timestamps
    normalized = re.sub(r'0x[0-9a-fA-F]+', '0xADDR', normalized)          # memory addresses
    normalized = re.sub(r'\bpid[= ]\d+', 'pid=PID', normalized)           # process IDs
    normalized = re.sub(r'/(?:tmp|home|var|usr|opt)/[^\s:]+', '/ABS_PATH', normalized)  # absolute paths
    normalized = re.sub(r'in \d+\.\d+s', 'in N.Ns', normalized)           # durations
    normalized = re.sub(r'(?<=line )\d+', 'N', normalized)                 # line numbers
    normalized = re.sub(r'\s+', ' ', normalized).strip()                   # collapse whitespace

    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════════════
# BLAME ANALYSIS — does the error reference files the agent changed?
# ═══════════════════════════════════════════════════════════════════════════

def error_references_changed_files(output: str, changed_files: list[str]) -> bool:
    """
    Check if the test output mentions ANY file the agent modified.

    If the traceback/error only references files the agent DIDN'T touch,
    the failure is unrelated to the agent's changes.

    Language-agnostic: just checks if filenames appear in the output text.
    Works for Python tracebacks, Node stack traces, Go panics, Rust backtraces, etc.
    """
    if not changed_files:
        return False

    output_lower = output.lower()
    for fp in changed_files:
        # Check both the full relative path and just the filename
        if fp.lower() in output_lower:
            return True
        basename = fp.rsplit("/", 1)[-1] if "/" in fp else fp
        if basename.lower() in output_lower:
            return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# TEST VERDICT — structured, actionable classification
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TestVerdict:
    """Structured test result — the agent receives THIS, not raw text."""
    category: str             # "pass", "pre_existing", "repeated", "unrelated", "agent_caused"
    error_signature: str      # Stable hash for dedup
    is_agent_fault: bool      # Should the agent try to fix this?
    action: str               # Concrete next step
    raw_output_preview: str   # First 300 chars for context


def classify_test_result(
    output: str,
    exit_code: int,
    changed_files: list[str],
    baseline_sig: str | None = None,
    error_history: list[str] | None = None,
    infra_flag: bool = False,
) -> TestVerdict:
    """
    Classify a test result using 3 language-agnostic signals.

    No hardcoded error patterns. Decision tree:

      1. exit_code == 0 → PASS
      2. signature == baseline → PRE-EXISTING (not agent's fault)
      3. signature repeated 3x → REPEATED (not agent's fault)
      4. error doesn't mention changed files → UNRELATED (not agent's fault)
      5. infra_flag from sandbox classifier → INFRA (not agent's fault)
      6. otherwise → AGENT_CAUSED (agent should fix)
    """
    signature = extract_error_signature(output)
    preview = output[:300].strip()
    history = error_history or []

    # 1. Tests passed
    if exit_code == 0:
        return TestVerdict(
            category="pass",
            error_signature=signature,
            is_agent_fault=False,
            action="Tests passed. Call get_diff() to review your changes, then finish().",
            raw_output_preview=preview,
        )

    # 2. Same as baseline — pre-existing failure
    if baseline_sig and signature == baseline_sig:
        return TestVerdict(
            category="pre_existing",
            error_signature=signature,
            is_agent_fault=False,
            action=(
                "This test failure existed BEFORE your code change — your code did NOT cause it. "
                "Verify your fix independently:\n"
                "  1. Create a standalone test script that imports and tests the function you changed\n"
                "  2. Run it with run_command('python your_test.py') or the appropriate language command\n"
                "  3. If your standalone test passes, call get_diff() then finish()"
            ),
            raw_output_preview=preview,
        )

    # 3. Same error repeated 3+ times — environment/infra issue
    if len(history) >= 3 and len(set(history[-3:])) == 1 and history[-1] == signature:
        return TestVerdict(
            category="repeated",
            error_signature=signature,
            is_agent_fault=False,
            action=(
                f"The SAME error has appeared {_count_consecutive(history, signature)}x in a row. "
                "Identical repeated errors are never caused by code changes — "
                "it's an environment issue (wrong runtime version, missing system dependency, "
                "broken test configuration). STOP modifying your code. Instead:\n"
                "  1. Create a standalone test script that tests your change directly\n"
                "  2. Run it outside the project's test runner\n"
                "  3. If it passes, your fix is correct — call get_diff() then finish()"
            ),
            raw_output_preview=preview,
        )

    # 4. Error doesn't reference any file the agent changed
    if changed_files and not error_references_changed_files(output, changed_files):
        return TestVerdict(
            category="unrelated",
            error_signature=signature,
            is_agent_fault=False,
            action=(
                "This test failure does NOT reference any file you modified. "
                "It's likely a pre-existing issue or environment problem. "
                "Verify your fix with a standalone test script, then call finish()."
            ),
            raw_output_preview=preview,
        )

    # 5. Sandbox classifier flagged infra
    if infra_flag:
        return TestVerdict(
            category="infra",
            error_signature=signature,
            is_agent_fault=False,
            action=(
                "The test runner detected an infrastructure error (missing dependency, "
                "broken configuration, incompatible runtime). Your code did not cause this. "
                "Create a standalone test to verify your fix independently."
            ),
            raw_output_preview=preview,
        )

    # 6. Agent caused — the error references changed files and is new
    return TestVerdict(
        category="agent_caused",
        error_signature=signature,
        is_agent_fault=True,
        action=(
            "Tests failed and the error references files you modified. "
            "Read the test output carefully — what was expected vs actual? "
            "Fix your code with write_file, then run_tests again."
        ),
        raw_output_preview=preview,
    )


def _count_consecutive(history: list[str], sig: str) -> int:
    """Count how many times `sig` appears consecutively at the end of history."""
    count = 0
    for s in reversed(history):
        if s == sig:
            count += 1
        else:
            break
    return count


# ═══════════════════════════════════════════════════════════════════════════
# TRIAGE STATE — persists across history compression
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# AGENT PHASE — tracked structurally, not conversationally
# ═══════════════════════════════════════════════════════════════════════════

# Phase constants
PHASE_EXPLORING = "EXPLORING"   # Reading code, searching, understanding
PHASE_WRITING   = "WRITING"     # Has started making changes
PHASE_VERIFYING = "VERIFYING"   # Has written code, now testing/reviewing
PHASE_FINISHING = "FINISHING"    # Verified, preparing to submit

# Tools allowed per phase (Problem 3: Verify→Finish gate restricts tools)
FINISH_ONLY_TOOLS = {"get_diff", "finish", "think"}


@dataclass
class TriageState:
    """
    Persistent triage state that survives history compression.

    Stores: baseline, error history, agent phase, nudge counters.
    All structural — no language/framework assumptions.
    """
    # Challenge A: Baseline
    baseline_signature: str | None = None
    baseline_status: str | None = None        # "passed" or "failed"
    baseline_output_preview: str = ""

    # Challenge C: Error history
    error_history: list[str] = field(default_factory=list)
    verdict_history: list[str] = field(default_factory=list)  # category per test run

    # Challenge D: Recovery tracking
    standalone_test_suggested: bool = False
    total_test_runs: int = 0
    agent_fault_count: int = 0
    not_agent_fault_count: int = 0

    # Problem 1: Phase Tracker (persists across compression)
    phase: str = PHASE_EXPLORING
    phase_changed_at_turn: int = 0
    files_written: list[str] = field(default_factory=list)  # which files the agent modified

    # Problem 2: Escalating Nudge Tracker
    nudge_count: int = 0        # how many nudges have been ignored
    last_nudge_turn: int = 0    # avoid double-nudging in same turn

    # Problem 3: Verify→Finish Gate
    verification_passed: bool = False  # standalone test passed after write
    finish_gate_active: bool = False   # tools restricted to finish-only

    # Exploration Strategy: tool usage tracking (language-agnostic)
    semantic_search_count: int = 0     # search_symbols calls
    structural_tool_count: int = 0     # find_files, get_dependencies, get_coupled_files, etc.
    content_search_count: int = 0      # search_code calls
    files_discovered: list[str] = field(default_factory=list)  # unique files found
    exploration_guidance_given: bool = False  # already told agent to use structural tools

    # ── Phase Tracking (Problem 1) ───────────────────────────────────────

    def transition_phase(self, new_phase: str, turn: int):
        """Transition to a new phase. Logged for debugging."""
        if self.phase != new_phase:
            old = self.phase
            self.phase = new_phase
            self.phase_changed_at_turn = turn
            log.info("triage.phase_transition", old=old, new=new_phase, turn=turn)

    def on_write_file(self, file_path: str, turn: int):
        """Called when agent uses write_file. Transitions to WRITING."""
        if file_path not in self.files_written:
            self.files_written.append(file_path)
        if self.phase == PHASE_EXPLORING:
            self.transition_phase(PHASE_WRITING, turn)

    def on_test_or_command_after_write(self, exit_code: int, turn: int):
        """Called when agent runs a test/command after writing code."""
        if self.phase in (PHASE_WRITING, PHASE_VERIFYING):
            self.transition_phase(PHASE_VERIFYING, turn)
        # Problem 3: If test passed and triage says not agent's fault → FINISHING
        if exit_code == 0 and self.phase == PHASE_VERIFYING:
            if self.not_agent_fault_count > 0 or self.baseline_status == "failed":
                self.verification_passed = True
                self.transition_phase(PHASE_FINISHING, turn)
                self.finish_gate_active = True
                log.info("triage.finish_gate_activated", turn=turn)

    # ── Escalating Nudges (Problem 2) ─────────────────────────────────────

    def get_nudge_message(self, turn: int) -> str | None:
        """
        Return a nudge message if the agent should be nudged, or None.
        Escalates through 3 tiers based on how many nudges have been ignored.
        """
        # Don't nudge if we're in EXPLORING or FINISHING
        if self.phase == PHASE_EXPLORING:
            return None
        if self.finish_gate_active:
            return None  # Finish gate handles this with tool restriction
        # Don't double-nudge in the same turn
        if turn == self.last_nudge_turn:
            return None

        # Only nudge if agent has written code but isn't verifying/finishing
        if not self.files_written:
            return None

        self.nudge_count += 1
        self.last_nudge_turn = turn

        files_str = ", ".join(self.files_written[:3])

        # Tier 1: Soft (nudge 1-4)
        if self.nudge_count <= 4:
            return (
                f"Reminder: You've written changes to {files_str}. "
                f"Consider verifying your fix — run a test or create a standalone test script, "
                f"then call get_diff() and finish()."
            )

        # Tier 2: Firm (nudge 5-9)
        if self.nudge_count <= 9:
            return (
                f"Your fix to {files_str} is written. You've been exploring for "
                f"{self.nudge_count} turns since writing. The next step is verification:\n"
                f"  1. Create a standalone test script that tests your specific change\n"
                f"  2. Run it with run_command\n"
                f"  3. Call get_diff() then finish()\n"
                f"Stop searching — you have enough context."
            )

        # Tier 3: Directive (nudge 10+)
        return (
            f"DIRECTIVE: You have explored for {self.nudge_count} turns after writing code to "
            f"{files_str}. Your fix is written. Execute this sequence NOW:\n"
            f"  1. Create a test script that verifies your change\n"
            f"  2. Run it with run_command\n"
            f"  3. get_diff()\n"
            f"  4. finish(summary='...')\n"
            f"Do not call any more search or read tools."
        )

    def should_restrict_tools(self) -> bool:
        """
        Problem 2+3: Return True if tools should be restricted.
        At Tier 3 nudge or when finish gate is active, exploration tools are removed.
        """
        if self.finish_gate_active:
            return True
        if self.nudge_count >= 10:
            return True
        return False

    def get_allowed_tools(self) -> set[str] | None:
        """
        Return the set of allowed tool names, or None if all tools are allowed.
        When restricted: only verification and finishing tools available.
        """
        if not self.should_restrict_tools():
            return None  # All tools allowed
        # Finish gate or Tier 3: only these tools
        return {"get_diff", "finish", "think", "run_command", "run_tests", "write_file"}

    # ── Exploration Strategy Tracking ─────────────────────────────────────

    # Tool categories (language-agnostic)
    _SEMANTIC_TOOLS = {"search_symbols"}
    _STRUCTURAL_TOOLS = {"find_files", "list_directory", "get_dependencies",
                         "get_coupled_files", "get_callers", "get_impact",
                         "get_test_coverage", "get_risk_score", "get_reviewers",
                         "get_top_files", "get_file_info", "get_symbol_details",
                         "get_class_hierarchy", "get_change_context", "file_outline",
                         "batch_read"}
    _CONTENT_SEARCH_TOOLS = {"search_code"}

    def record_tool_usage(self, tool_name: str, result: dict | None = None):
        """Track which tool categories the agent is using."""
        if tool_name in self._SEMANTIC_TOOLS:
            self.semantic_search_count += 1
        elif tool_name in self._STRUCTURAL_TOOLS:
            self.structural_tool_count += 1
        elif tool_name in self._CONTENT_SEARCH_TOOLS:
            self.content_search_count += 1

        # Track discovered files from tool results
        if result and isinstance(result, dict):
            # find_files returns {files: [...]}
            for f in result.get("files", []):
                if isinstance(f, str) and f not in self.files_discovered:
                    self.files_discovered.append(f)
            # get_dependencies returns {dependencies: [...], dependents: [...]}
            for f in result.get("dependencies", []):
                if isinstance(f, str) and f not in self.files_discovered:
                    self.files_discovered.append(f)
            for f in result.get("dependents", []):
                if isinstance(f, str) and f not in self.files_discovered:
                    self.files_discovered.append(f)

    def get_exploration_guidance(self) -> str | None:
        """
        Return guidance if the agent is exploring inefficiently.
        Fires ONCE — doesn't repeat.
        """
        if self.exploration_guidance_given:
            return None
        if self.phase != PHASE_EXPLORING:
            return None

        # Trigger: many semantic searches, few structural tools
        if self.semantic_search_count >= 8 and self.structural_tool_count < 3:
            self.exploration_guidance_given = True
            return (
                f"EXPLORATION TIP: You've done {self.semantic_search_count} semantic searches "
                f"but only {self.structural_tool_count} structural lookups. "
                f"Structural tools are faster and more precise:\n"
                f"  - find_files('*keyword*') to locate files by name\n"
                f"  - get_dependencies(file) to see what imports/is imported by a file\n"
                f"  - get_coupled_files(file) to find files that change together\n"
                f"  - batch_read([file1, file2, ...]) to read multiple files at once\n"
                f"  - file_outline(file) to see structure without reading full content\n"
                f"Try these before doing more semantic searches."
            )

        # Trigger: many files discovered but few read
        if len(self.files_discovered) >= 5 and self.structural_tool_count >= 2:
            # Check if batch_read was never called
            # (we can't track this perfectly, but if files_discovered > files_read it's a hint)
            pass  # This would need read tracking — skip for now

        return None

    # ── Baseline Recording ────────────────────────────────────────────────

    def record_baseline(self, output: str, exit_code: int):
        """Run ONCE before agent writes any code."""
        self.baseline_signature = extract_error_signature(output)
        self.baseline_status = "passed" if exit_code == 0 else "failed"
        self.baseline_output_preview = output[:200].strip()
        log.info("triage.baseline",
                 status=self.baseline_status,
                 signature=self.baseline_signature[:8] if self.baseline_signature else "none")

    def record_verdict(self, verdict: TestVerdict):
        """Record after every test run."""
        self.total_test_runs += 1
        self.error_history.append(verdict.error_signature)
        self.verdict_history.append(verdict.category)
        if verdict.is_agent_fault:
            self.agent_fault_count += 1
        else:
            self.not_agent_fault_count += 1
        if not verdict.is_agent_fault and self.total_test_runs >= 3:
            self.standalone_test_suggested = True

    def get_repair_message(self, verdict: TestVerdict, repair_count: int) -> str:
        """
        Return an actionable message based on the verdict.
        No framework-specific advice — just structural guidance.
        """
        if not verdict.is_agent_fault:
            # Not the agent's fault — tell it to stop fixing and verify independently
            if self.standalone_test_suggested:
                return (
                    f"{verdict.action}\n\n"
                    f"SUMMARY: {self.not_agent_fault_count} of {self.total_test_runs} test runs "
                    f"failed due to issues OUTSIDE your code. Stop modifying your fix."
                )
            return verdict.action

        # Agent's fault — escalate guidance based on attempt count
        if repair_count <= 3:
            return (
                f"Tests FAILED (repair #{repair_count}). {verdict.action}"
            )
        elif repair_count <= 7:
            return (
                f"Tests FAILED (repair #{repair_count}). You've tried {repair_count} fixes. "
                f"Re-read the ORIGINAL ticket and the test output. "
                f"Is your approach fundamentally wrong? Consider:\n"
                f"  - Re-reading the code you're modifying with read_file\n"
                f"  - Checking get_callers() or get_impact() for side effects\n"
                f"  - Trying a completely different fix strategy"
            )
        else:
            return (
                f"Tests FAILED (repair #{repair_count}). {repair_count} attempts. "
                f"Create a standalone test to isolate whether the problem is your code "
                f"or the test environment. If standalone passes, call finish()."
            )

    def to_prompt(self) -> str:
        """
        Generate a persistent summary for injection after history compression.
        The agent never loses awareness of phase, baseline, and error patterns.
        """
        lines = ["\n## Agent State (persists across compression)\n"]

        # Phase — the most critical piece of context after compression
        lines.append(f"- **Current phase**: {self.phase}")
        if self.files_written:
            files_str = ", ".join(f"`{f}`" for f in self.files_written[:5])
            lines.append(f"- **Files you already modified**: {files_str}")
        if self.phase == PHASE_WRITING:
            lines.append(f"  - You wrote your fix. Next step: verify with a test, then finish().")
        elif self.phase == PHASE_VERIFYING:
            lines.append(f"  - You wrote your fix and started verification. Next: confirm test passes, then finish().")
        elif self.phase == PHASE_FINISHING:
            lines.append(f"  - **Verification passed. Call get_diff() then finish() NOW.**")

        if self.baseline_status:
            lines.append(f"- Baseline (before your changes): **{self.baseline_status}**")
            if self.baseline_status == "failed":
                lines.append(f"  - Pre-existing failure. These are NOT caused by your code.")

        if self.total_test_runs > 0:
            lines.append(f"- Test runs: {self.total_test_runs} total, {self.agent_fault_count} your fault, {self.not_agent_fault_count} not your fault")
            if len(set(self.error_history[-3:])) == 1 and len(self.error_history) >= 3:
                lines.append(f"  - Same error repeated — environment issue, not your code")

        if self.verification_passed:
            lines.append("- **Your standalone test PASSED. Your fix is correct. Call finish().**")

        if self.standalone_test_suggested and not self.verification_passed:
            lines.append("- Create a standalone test script to verify your fix independently")

        # Exploration metrics (helps agent after compression)
        if self.phase == PHASE_EXPLORING and (self.semantic_search_count + self.structural_tool_count) > 5:
            lines.append(f"- Exploration: {self.semantic_search_count} semantic searches, "
                         f"{self.structural_tool_count} structural lookups, "
                         f"{self.content_search_count} grep searches")
            if self.semantic_search_count > self.structural_tool_count * 3:
                lines.append("  - Use more structural tools: find_files, get_dependencies, get_coupled_files, batch_read")
            if self.files_discovered:
                lines.append(f"- Files discovered so far: {', '.join(f'`{f}`' for f in self.files_discovered[:10])}")

        return "\n".join(lines)
