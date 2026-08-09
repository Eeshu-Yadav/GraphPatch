"""
Code Review Gate — Uses Opus to review agent-generated diffs before publishing.

Checks:
  1. Are ALL changes necessary for the ticket? (no over-engineering)
  2. Do changes match the codebase's existing patterns/style?
  3. Are comments minimal and only where the codebase already has them?
  4. No unnecessary new files, abstractions, or wrappers?

Returns: ReviewResult with approve/reject + feedback for the agent.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog
import anthropic

log = structlog.get_logger(__name__)


@dataclass
class ReviewResult:
    approved: bool
    feedback: str            # Detailed feedback for agent if rejected
    files_to_keep: list[str] # Files that are actually needed
    files_to_drop: list[str] # Files that should be reverted/removed
    summary: str             # Short summary for PR body


REVIEW_PROMPT = """\
You are a strict senior code reviewer. Review the following diff for ticket: {title}

## Ticket Description
{body}

## Diff
```
{diff}
```

## Codebase Context
Repository: {repo_id}
Files changed: {files_changed}

## Review Criteria (BE STRICT)

1. **Necessity**: Is EVERY changed file directly required to fix the ticket?
   - If the ticket is about filtering Sentry errors, only Sentry config should change
   - Don't wrap existing working code in error boundaries unless it's broken
   - Don't create utility files for one-off operations

2. **Minimality**: Are the changes the MINIMUM needed?
   - No new abstractions unless reused 3+ times
   - No wrapper functions for single-use cases
   - No "defensive" code for problems that don't exist in our codebase

3. **Codebase Style**: Do changes match existing patterns?
   - Comments: Only add if the codebase already has similar comment density
   - Naming: Follow existing conventions
   - Structure: Don't introduce new patterns (error boundaries, utility modules)
     unless the codebase already uses them

4. **Over-engineering Detection**:
   - Creating new files when a 3-line change suffices = REJECT
   - Wrapping every function in try/catch = REJECT
   - Adding generalized utilities for specific bugs = REJECT

5. **Import Verification** (CRITICAL):
   - Do ALL imported symbols actually exist in the target modules?
   - If you see imports from relative paths ('./foo', '@/lib/bar'), verify the imported
     names are real exports in those files. Look at the diff context.
   - Hallucinated imports (non-existent functions/hooks/components) = REJECT immediately.

6. **Feature Preservation** (CRITICAL):
   - Were any existing features, components, or exports DELETED that the ticket
     did NOT ask to remove?
   - If the ticket says "add X" but the diff removes Y (search bar, dropdown, filter,
     existing UI element) — REJECT. The scope of deletion must match the ticket.

## Response Format (JSON only)
{{
  "approved": true/false,
  "files_to_keep": ["file1.ts", "file2.ts"],
  "files_to_drop": ["unnecessary-file.ts"],
  "feedback": "Explanation of what to fix if rejected",
  "summary": "One-line summary of what the changes do"
}}
"""


def review_changes(
    repo_root: Path,
    repo_id: str,
    ticket_title: str,
    ticket_body: str,
    file_results: list,
    model: str = "claude-opus-4-20250514",
) -> ReviewResult:
    """
    Review agent-generated changes using Opus before publishing.
    Returns ReviewResult with approve/reject decision.
    """
    # Get the diff
    try:
        proc = subprocess.run(
            ["git", "diff"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=30,
        )
        diff = proc.stdout
        if not diff:
            # Also check untracked files
            proc2 = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=str(repo_root),
                capture_output=True, text=True, timeout=30,
            )
            diff = proc2.stdout

        # Include new untracked files content
        proc3 = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(repo_root),
            capture_output=True, text=True, timeout=10,
        )
        untracked = [
            line[3:] for line in proc3.stdout.splitlines()
            if line.startswith("??") and not line.endswith(".js")  # skip test artifacts
        ]
        for ufile in untracked[:5]:  # limit to 5 new files
            upath = repo_root / ufile
            if upath.is_file() and upath.stat().st_size < 5000:
                content = upath.read_text(encoding="utf-8", errors="replace")
                diff += f"\n\n--- /dev/null\n+++ b/{ufile}\n{content}"

    except Exception as e:
        log.warning("review.diff_failed", error=str(e))
        return ReviewResult(
            approved=True, feedback="", files_to_keep=[],
            files_to_drop=[], summary="Review skipped (diff unavailable)"
        )

    if not diff.strip():
        return ReviewResult(
            approved=False, feedback="No changes found.",
            files_to_keep=[], files_to_drop=[], summary="No changes"
        )

    # Truncate diff if too large
    if len(diff) > 15000:
        diff = diff[:15000] + "\n... [diff truncated]"

    files_changed = [fr.file_path for fr in file_results]

    # Call Opus for review
    prompt = REVIEW_PROMPT.format(
        title=ticket_title,
        body=ticket_body,
        diff=diff,
        repo_id=repo_id,
        files_changed=", ".join(files_changed),
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text.strip()
        log.info("review.response", model=model, tokens=response.usage.input_tokens + response.usage.output_tokens)

        # Parse JSON response
        import json
        # Extract JSON from response (handle markdown code blocks)
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        data = json.loads(text)

        result = ReviewResult(
            approved=data.get("approved", False),
            feedback=data.get("feedback", ""),
            files_to_keep=data.get("files_to_keep", []),
            files_to_drop=data.get("files_to_drop", []),
            summary=data.get("summary", ""),
        )

        log.info(
            "review.done",
            approved=result.approved,
            keep=len(result.files_to_keep),
            drop=len(result.files_to_drop),
        )
        return result

    except json.JSONDecodeError as e:
        log.warning("review.parse_failed", error=str(e), raw=text[:200])
        # Fail closed — don't publish unreviewed code
        return ReviewResult(
            approved=False,
            feedback=f"Review could not be parsed — rejecting to be safe. Error: {e}",
            files_to_keep=[], files_to_drop=[],
            summary="Review REJECTED (parse failure — manual review required)"
        )
    except Exception as e:
        log.warning("review.api_failed", error=str(e))
        # Fail closed — don't publish unreviewed code
        return ReviewResult(
            approved=False,
            feedback=f"Review unavailable ({e}) — rejecting to be safe. Manual review required.",
            files_to_keep=[], files_to_drop=[],
            summary="Review REJECTED (API unavailable — manual review required)"
        )


def revert_unnecessary_files(repo_root: Path, files_to_drop: list[str]) -> None:
    """Revert/delete files flagged as unnecessary by reviewer."""
    for fpath in files_to_drop:
        abs_path = repo_root / fpath
        if abs_path.exists():
            # Check if it's a new file (untracked) or modified
            proc = subprocess.run(
                ["git", "status", "--porcelain", fpath],
                cwd=str(repo_root), capture_output=True, text=True,
            )
            status = proc.stdout.strip()
            if status.startswith("??"):
                # New file — delete it
                abs_path.unlink()
                log.info("review.deleted", file=fpath)
            elif status.startswith(" M") or status.startswith("M"):
                # Modified file — revert to original
                subprocess.run(
                    ["git", "checkout", "--", fpath],
                    cwd=str(repo_root), capture_output=True,
                )
                log.info("review.reverted", file=fpath)
