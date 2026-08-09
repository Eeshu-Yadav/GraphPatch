from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class ValidationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"      # could not run (missing deps, syntax error, etc.)
    SKIPPED = "skipped"  # no tests found


@dataclass
class TestResult:
    status: ValidationStatus
    passed: int
    failed: int
    errors: int
    output: str          # full pytest stdout+stderr (truncated to 4000 chars)
    duration_s: float


@dataclass
class LintResult:
    status: ValidationStatus
    issues: list[str]    # list of issue strings
    output: str


@dataclass
class SyntaxResult:
    status: ValidationStatus
    errors: list[str]    # list of "file:line: error" strings


@dataclass
class ValidationResult:
    ticket_id: str
    repo_id: str
    overall: ValidationStatus     # worst of test/lint/syntax
    syntax: SyntaxResult
    tests: TestResult
    lint: LintResult
    files_written: list[str]
    repo_root: str
    typecheck: LintResult | None = None  # mypy results (non-blocking)
    build: LintResult | None = None      # build/compile check results

    def passed(self) -> bool:
        return self.overall == ValidationStatus.PASSED

    def summary(self) -> str:
        lines = [
            f"Validation: {self.overall.value.upper()} — {self.ticket_id}",
            f"  Syntax:  {self.syntax.status.value}",
            f"  Tests:   {self.tests.status.value} ({self.tests.passed} passed, {self.tests.failed} failed, {self.tests.errors} errors) [{self.tests.duration_s:.1f}s]",
            f"  Lint:    {self.lint.status.value} ({len(self.lint.issues)} issues)",
        ]
        if self.build:
            lines.append(f"  Build:   {self.build.status.value} ({len(self.build.issues)} issues)")
        if self.typecheck:
            lines.append(f"  Types:   {self.typecheck.status.value} ({len(self.typecheck.issues)} issues)")
        if self.tests.failed > 0 or self.tests.errors > 0:
            lines.append("\n--- Test Output (last 60 lines) ---")
            tail = "\n".join(self.tests.output.splitlines()[-60:])
            lines.append(tail)
        if self.syntax.errors:
            lines.append("\n--- Syntax Errors ---")
            lines.extend(f"  {e}" for e in self.syntax.errors)
        if self.lint.issues:
            lines.append("\n--- Lint Issues ---")
            lines.extend(f"  {i}" for i in self.lint.issues[:20])
        if self.build and self.build.issues:
            lines.append("\n--- Build Errors ---")
            lines.extend(f"  {i}" for i in self.build.issues[:15])
        if self.typecheck and self.typecheck.issues:
            lines.append("\n--- Type Errors (non-blocking) ---")
            lines.extend(f"  {i}" for i in self.typecheck.issues[:15])
        return "\n".join(lines)
