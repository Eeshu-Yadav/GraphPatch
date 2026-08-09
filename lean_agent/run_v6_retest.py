#!/usr/bin/env python3
"""
Re-run the 5 UNRESOLVED v5 instances through v6 (forced get_diff before finish)
to test whether W3 improves resolved-rate.

If W3 works, 4 of these should flip from ❌ to ✅ (the 4 "right-file-wrong-fix"
cases). The 5th (django-14034 — wrong file entirely) is not what W3 targets.

Usage:
    python3 lean_agent/run_v6_retest.py
"""
from __future__ import annotations

import os, sys, json, time, subprocess, re
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
LEAN = ROOT / "lean_agent"
TRACES = LEAN / "traces_v6"
TRACES.mkdir(exist_ok=True)

for p in ["layer2-indexer/src", "layer3-context", "layer4-planner", "layer45-agent",
          "layer5-implementer", "layer6-validator", "."]:
    sys.path.insert(0, str(ROOT / p))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from datasets import load_dataset
from lean_agent.agent_v6 import run_lean_agent_v6
from lean_agent.classifier import route_issue
from lean_agent.tier_config import TierConfig
from lean_agent.vagify import vagify
import anthropic

# The 5 v5 failures (from SWE-bench harness ground truth)
UNRESOLVED_V5 = [
    "django__django-14011",   # matched 1/2 gold
    "django__django-14034",   # wrong file
    "django__django-14053",   # right file, wrong fix
    "django__django-14140",   # right file, wrong fix
    "django__django-14155",   # right file, wrong fix
]

_FILE_RE = re.compile(r"^diff --git a/([^ ]+) b/", re.MULTILINE)

# NOTE: this retest is single-attempt per issue. No retry loop. The retry
# workstream (W4) is deferred per V6_DESIGN.md — and when implemented, will
# use ONLY production-safe signals (agent's own run_tests output, end_turn
# without finish, empty diff). It will NEVER consult gold files or the
# SWE-bench grader as a retry trigger. That would be test-data leakage.
# Iteration inside a single run is already handled by W3 (get_diff gate)
# and the finish-nudge escalation.


def main():
    print(f"Loading SWE-bench Verified...")
    ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    by_id = {i["instance_id"]: i for i in ds}

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    batch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []

    for idx, iid in enumerate(UNRESOLVED_V5, start=1):
        inst = by_id[iid]
        repo_id = inst["repo"]
        repo_path = ROOT / "repos" / repo_id.replace("/", "_")
        base_commit = inst["base_commit"]
        real_body = inst["problem_statement"][:3000]
        gold = _FILE_RE.findall(inst["patch"])

        # Reset + vagify (same as v5)
        subprocess.run(["git", "-C", str(repo_path), "reset", "--hard", base_commit],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_path), "clean", "-fd"],
                       check=True, capture_output=True)

        title, body = vagify(iid, real_body, client)
        tier, signals = route_issue(title, body, client)
        cfg = TierConfig.for_tier(tier)

        print(f"\n{'='*70}")
        print(f"[{idx}/{len(UNRESOLVED_V5)}]  {iid}  (v5 verdict: unresolved)")
        print(f"  tier={tier}  max_turns={cfg.max_turns}  gold={gold}")

        log_path = TRACES / f"{batch_ts}_{iid}.log"
        orig_stdout = sys.stdout
        class Tee:
            def __init__(self, p): self.f = open(p, "w", buffering=1); self.s = orig_stdout
            def write(self, d): self.f.write(d); self.s.write(d)
            def flush(self):    self.f.flush(); self.s.flush()
            def close(self):    self.f.close()
        tee = Tee(log_path)
        sys.stdout = tee
        err = None
        t0 = time.time()
        try:
            r = run_lean_agent_v6(
                ticket_title=title, ticket_body=body,
                repo_id=repo_id, repo_path=str(repo_path),
                tier=tier, max_turns=cfg.max_turns, nudge_after_write=cfg.nudge_after_write,
            )
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:200]}"
            r = {}
        finally:
            sys.stdout = orig_stdout
            tee.close()
        elapsed = round(time.time() - t0, 1)

        # Capture diff NOW
        try:
            model_patch = subprocess.run(
                ["git", "-C", str(repo_path), "diff", base_commit],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout
        except Exception:
            model_patch = ""

        changed = r.get("files_changed", [])
        matched = sum(1 for f in gold
                      if any(f in c or c.endswith(f.split("/")[-1]) for c in changed))
        hit = 100 * r.get("total_cache_read", 0) / max(1, r.get("total_tokens", 1))

        summary = {
            "instance_id": iid,
            "tier": tier,
            "gold_files": gold,
            "files_changed": changed,
            "gold_matched": matched,
            "gold_count": len(gold),
            "total_turns": r.get("total_turns", 0),
            "total_tokens": r.get("total_tokens", 0),
            "cache_read": r.get("total_cache_read", 0),
            "cache_create": r.get("total_cache_create", 0),
            "success": r.get("success", False),
            "diff_required_blocks": r.get("diff_required_blocks", 0),
            "last_diff_turn": r.get("last_diff_turn", -1),
            "elapsed_s": elapsed,
            "error": err,
        }
        (TRACES / f"{batch_ts}_{iid}.patch").write_text(model_patch)
        (TRACES / f"{batch_ts}_{iid}.json").write_text(
            json.dumps({**summary, "model_patch": model_patch,
                        "explore_summary": r.get("explore_summary", "")},
                       indent=2, default=str)
        )
        results.append(summary)

        print(f"  → success={summary['success']}  turns={summary['total_turns']} "
              f"tokens={summary['total_tokens']:,} hit={hit:.0f}% "
              f"gold={matched}/{len(gold)} "
              f"diff_blocks={summary['diff_required_blocks']} "
              f"{elapsed}s")
        if err: print(f"  ERROR: {err}")

    # Write predictions for SWE-bench harness
    preds = TRACES / f"{batch_ts}_predictions.jsonl"
    with open(preds, "w") as f:
        for r in results:
            patch_file = TRACES / f"{batch_ts}_{r['instance_id']}.patch"
            patch = patch_file.read_text() if patch_file.exists() else ""
            f.write(json.dumps({
                "instance_id": r["instance_id"],
                "model_name_or_path": "lean_v6_stable",
                "model_patch": patch,
            }) + "\n")

    # Summary
    print(f"\n{'='*70}\n  V6 RETEST SUMMARY  ({len(results)} unresolved-v5 instances)\n{'='*70}")
    print(f"  {'instance':<32} {'turns':>5} {'tokens':>8} {'diff-blocks':>11} {'gold':<6}")
    for r in results:
        print(f"  {r['instance_id']:<32} {r['total_turns']:>5} "
              f"{r['total_tokens']:>8,} {r['diff_required_blocks']:>11} "
              f"{r['gold_matched']}/{r['gold_count']}")
    tot_blocks = sum(r["diff_required_blocks"] for r in results)
    print(f"\n  Total diff_required_blocks: {tot_blocks}   "
          f"(each = 1 finish attempt where get_diff was not called)")
    print(f"  Predictions: {preds}")
    print(f"\n  To grade:")
    print(f"    python -m swebench.harness.run_evaluation \\")
    print(f"      --dataset_name SWE-bench/SWE-bench_Verified \\")
    print(f"      --predictions_path {preds} \\")
    print(f"      --max_workers 4 --run_id v6_{batch_ts}")


if __name__ == "__main__":
    main()
