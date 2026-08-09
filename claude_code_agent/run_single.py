#!/usr/bin/env python3
"""
Run Claude Code agent on a single SWE-bench instance.

This script:
1. Loads the SWE-bench issue
2. Resets repo to base_commit
3. Spawns a Claude Code session with /fix-ticket
4. Captures git diff as the patch
5. Writes SWE-bench compatible output (.json, .patch, _preds.jsonl)

Usage:
    python3 claude_code_agent/run_single.py django__django-14053
    python3 claude_code_agent/run_single.py django__django-14053 --no-vague

Output written to: claude_code_agent/traces/
"""
import os
import sys
import json
import time
import re
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
TRACES = Path(__file__).parent / "traces"
TRACES.mkdir(exist_ok=True)

# Load .env
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

_FILE_RE = re.compile(r"^diff --git a/([^ ]+) b/", re.MULTILINE)


def main():
    ap = argparse.ArgumentParser(description="Run Claude Code agent on a SWE-bench instance")
    ap.add_argument("instance_id", help="e.g. django__django-14053")
    ap.add_argument("--no-vague", action="store_true", help="Use original issue text, don't vagify")
    ap.add_argument("--issue-file", help="Path to issue .md file (skips SWE-bench dataset loading)")
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # ── Load issue ───────────────────────────────────────────────────
    if args.issue_file:
        # Load from local issue file (lean_agent/issues/*.md format)
        issue_text = Path(args.issue_file).read_text()
        lines = issue_text.strip().split("\n")
        meta = {}
        for line in lines[:5]:
            if ":" in line and not line.startswith("**"):
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        repo_id = meta.get("repo_id", "")
        ticket_id = meta.get("ticket_id", args.instance_id)
        # Extract title (line starting with **Title:**)
        title = ""
        body_start = 0
        for i, line in enumerate(lines):
            if line.startswith("**Title:**"):
                title = line.replace("**Title:**", "").strip()
                body_start = i + 1
                break
        body = "\n".join(lines[body_start:]).strip()
        repo_path = ROOT / "repos" / repo_id.replace("/", "_")
        base_commit = None  # Not available from issue file
        gold = []
        print(f"  Loaded from: {args.issue_file}")
    else:
        # Load from SWE-bench dataset
        try:
            from datasets import load_dataset
        except ImportError:
            sys.exit("ERROR: Install datasets: pip install datasets")

        print(f"Loading SWE-bench Verified, looking for {args.instance_id}...")
        ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
        inst = next((i for i in ds if i["instance_id"] == args.instance_id), None)
        if inst is None:
            sys.exit(f"  ERROR — {args.instance_id} not found in dataset")

        repo_id = inst["repo"]
        repo_path = ROOT / "repos" / repo_id.replace("/", "_")
        base_commit = inst["base_commit"]
        title = args.instance_id
        body = inst["problem_statement"][:3000]
        gold = _FILE_RE.findall(inst["patch"])
        ticket_id = args.instance_id

    print(f"  repo={repo_id}  path={repo_path}")
    if gold:
        print(f"  gold files ({len(gold)}): {gold}")

    # ── Reset repo to base commit ────────────────────────────────────
    if base_commit:
        print(f"  Resetting to {base_commit[:10]}...")
        subprocess.run(["git", "-C", str(repo_path), "reset", "--hard", base_commit],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_path), "clean", "-fd"],
                       check=True, capture_output=True)
    else:
        print("  No base_commit — using repo as-is")

    # ── Build the /fix-ticket prompt ─────────────────────────────────
    fix_prompt = (
        f"/fix-ticket {repo_id} {ticket_id} "
        f"\"{title}\" "
        f"\"{body[:2000]}\""
    )

    # ── Write the prompt to a file for the user to copy ──────────────
    prompt_file = TRACES / f"{ts}_{args.instance_id}_prompt.txt"
    prompt_file.write_text(fix_prompt)

    # ── Write a runner script that captures output ───────────────────
    runner_script = TRACES / f"{ts}_{args.instance_id}_run.sh"
    runner_script.write_text(f"""#!/bin/bash
# Auto-generated runner for {args.instance_id}
# Run this in a terminal to spawn Claude Code on this issue.
#
# After Claude Code finishes, run the capture script:
#   bash {TRACES}/{ts}_{args.instance_id}_capture.sh

set -e
cd {repo_path}

echo "=== Starting Claude Code for {args.instance_id} ==="
echo "Working directory: $(pwd)"
echo ""
echo "Paste this into Claude Code:"
echo ""
cat {prompt_file}
echo ""
echo ""
echo "Or run non-interactively:"
echo "  claude --print '{fix_prompt[:200]}...'"
echo ""
echo "When done, run:"
echo "  bash {TRACES}/{ts}_{args.instance_id}_capture.sh"
""")
    runner_script.chmod(0o755)

    # ── Write a capture script (run AFTER Claude Code finishes) ──────
    capture_script = TRACES / f"{ts}_{args.instance_id}_capture.sh"
    diff_ref = base_commit if base_commit else "HEAD~1"
    capture_script.write_text(f"""#!/bin/bash
# Capture results after Claude Code session for {args.instance_id}
# Run this AFTER the Claude Code session completes.

set -e
REPO="{repo_path}"
TRACES="{TRACES}"
TS="{ts}"
INSTANCE="{args.instance_id}"
BASE="{diff_ref}"
GOLD_FILES='{json.dumps(gold)}'

echo "=== Capturing results for $INSTANCE ==="

# 1. Capture the git diff (the patch)
PATCH=$(git -C "$REPO" diff "$BASE")
echo "$PATCH" > "$TRACES/${{TS}}_${{INSTANCE}}.patch"
echo "  Patch written: $TRACES/${{TS}}_${{INSTANCE}}.patch"

# 2. Extract changed files from the diff
CHANGED=$(echo "$PATCH" | grep "^diff --git" | sed 's|diff --git a/\\([^ ]*\\) b/.*|\\1|')
echo "  Files changed: $CHANGED"

# 3. Compare with gold files
GOLD_MATCHED=0
GOLD_COUNT=$(echo '$GOLD_FILES' | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
for gf in $(echo '$GOLD_FILES' | python3 -c "import sys,json; [print(f) for f in json.load(sys.stdin)]"); do
    if echo "$CHANGED" | grep -q "$gf"; then
        GOLD_MATCHED=$((GOLD_MATCHED + 1))
    fi
done

# 4. Write summary JSON
python3 -c "
import json
summary = {{
    'instance_id': '$INSTANCE',
    'agent': 'claude-code-agent (native /fix-ticket skill)',
    'gold_files': $GOLD_FILES,
    'files_changed': '$CHANGED'.strip().split('\\n') if '$CHANGED'.strip() else [],
    'gold_matched': $GOLD_MATCHED,
    'gold_count': $GOLD_COUNT,
    'success': len('$PATCH'.strip()) > 0,
    'model_patch': open('$TRACES/${{TS}}_${{INSTANCE}}.patch').read(),
}}
with open('$TRACES/${{TS}}_${{INSTANCE}}.json', 'w') as f:
    json.dump(summary, f, indent=2)
print(f'  Summary: $TRACES/${{TS}}_${{INSTANCE}}.json')
"

# 5. Write SWE-bench predictions JSONL
python3 -c "
import json
pred = {{
    'instance_id': '$INSTANCE',
    'model_name_or_path': 'claude_code_agent',
    'model_patch': open('$TRACES/${{TS}}_${{INSTANCE}}.patch').read(),
}}
with open('$TRACES/${{TS}}_${{INSTANCE}}_preds.jsonl', 'w') as f:
    f.write(json.dumps(pred) + '\\n')
print(f'  Predictions: $TRACES/${{TS}}_${{INSTANCE}}_preds.jsonl')
"

# 6. Print results
echo ""
echo "=========================================="
echo "  RESULT — $INSTANCE"
echo "=========================================="
echo "  files changed:  $CHANGED"
echo "  gold matched:   $GOLD_MATCHED/$GOLD_COUNT"
echo "  patch size:     $(echo "$PATCH" | wc -l) lines"
echo ""
echo "  To grade with SWE-bench harness:"
echo "    python -m swebench.harness.run_evaluation \\\\"
echo "      --dataset_name SWE-bench/SWE-bench_Verified \\\\"
echo "      --predictions_path $TRACES/${{TS}}_${{INSTANCE}}_preds.jsonl \\\\"
echo "      --max_workers 1 --run_id claude_code_$TS"

# 7. Reset repo back to base commit
git -C "$REPO" reset --hard "$BASE"
git -C "$REPO" clean -fd
echo ""
echo "  Repo reset to $BASE"
""")
    capture_script.chmod(0o755)

    # ── Print prompt directly (copy-paste into Claude Code) ─────────
    print(f"\n{'='*70}")
    print(f"  READY — {args.instance_id}")
    print(f"{'='*70}")
    if gold:
        print(f"  gold files: {gold}")
    print(f"\n  PROMPT (copy-paste into Claude Code):\n")
    print(fix_prompt)
    print(f"\n{'='*70}")
    print(f"  After Claude Code finishes, capture results:")
    print(f"    bash {capture_script}")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
