#!/usr/bin/env python3
"""
Run v6 on a single SWE-bench instance — used for A/B testing W3 against v5.

Captures the diff, writes predictions.jsonl, prints grader command.

Usage:
    python3 lean_agent/run_v6_single.py django__django-14053
"""
import os, sys, json, time, argparse, subprocess, re
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

_FILE_RE = re.compile(r"^diff --git a/([^ ]+) b/", re.MULTILINE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("instance_id", help="e.g. django__django-14053")
    ap.add_argument("--no-vague", action="store_true")
    args = ap.parse_args()

    print(f"Loading SWE-bench Verified, looking for {args.instance_id}...")
    ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    inst = next((i for i in ds if i["instance_id"] == args.instance_id), None)
    if inst is None:
        sys.exit(f"  ERROR — {args.instance_id} not found in dataset")

    repo_id = inst["repo"]
    repo_path = ROOT / "repos" / repo_id.replace("/", "_")
    base_commit = inst["base_commit"]
    real_body = inst["problem_statement"][:3000]
    gold = _FILE_RE.findall(inst["patch"])

    print(f"  repo={repo_id}  base={base_commit[:10]}")
    print(f"  gold files ({len(gold)}): {gold}")

    # Reset repo
    subprocess.run(["git", "-C", str(repo_path), "reset", "--hard", base_commit],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "clean", "-fd"],
                   check=True, capture_output=True)

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    if args.no_vague:
        title, body = args.instance_id, real_body
    else:
        title, body = vagify(args.instance_id, real_body, client)

    tier, signals = route_issue(title, body, client)
    cfg = TierConfig.for_tier(tier)
    print(f"  tier={tier}  max_turns={cfg.max_turns}  nudge_after={cfg.nudge_after_write}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = TRACES / f"{ts}_{args.instance_id}.log"

    class Tee:
        def __init__(self, p): self.f = open(p, "w", buffering=1); self.s = sys.stdout
        def write(self, d): self.f.write(d); self.s.write(d)
        def flush(self):    self.f.flush(); self.s.flush()
        def close(self):    self.f.close()

    orig = sys.stdout
    tee = Tee(log_path)
    sys.stdout = tee
    t0 = time.time()
    try:
        r = run_lean_agent_v6(
            ticket_title=title, ticket_body=body,
            repo_id=repo_id, repo_path=str(repo_path),
            tier=tier, max_turns=cfg.max_turns, nudge_after_write=cfg.nudge_after_write,
        )
    finally:
        sys.stdout = orig
        tee.close()
    elapsed = round(time.time() - t0, 1)

    # Capture patch
    patch = subprocess.run(
        ["git", "-C", str(repo_path), "diff", base_commit],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout

    changed = r.get("files_changed", [])
    matched = sum(1 for f in gold
                  if any(f in c or c.endswith(f.split("/")[-1]) for c in changed))
    hit = 100 * r.get("total_cache_read", 0) / max(1, r.get("total_tokens", 1))

    summary = {
        "instance_id": args.instance_id,
        "agent": "v6 (W3: forced get_diff)",
        "tier": tier,
        "gold_files": gold,
        "files_changed": changed,
        "gold_matched": matched,
        "gold_count": len(gold),
        "total_turns": r.get("total_turns"),
        "total_tokens": r.get("total_tokens", 0),
        "cache_read": r.get("total_cache_read", 0),
        "cache_create": r.get("total_cache_create", 0),
        "success": r.get("success"),
        "diff_required_blocks": r.get("diff_required_blocks", 0),
        "last_diff_turn": r.get("last_diff_turn", -1),
        "elapsed_s": elapsed,
    }

    print(f"\n{'='*70}\n  V6 RESULT — {args.instance_id}\n{'='*70}")
    print(f"  tier:              {summary['tier']}")
    print(f"  total turns:       {summary['total_turns']}")
    print(f"  tokens billed:     {summary['total_tokens']:,}")
    print(f"  cache read:        {summary['cache_read']:,} ({hit:.0f}%)")
    print(f"  files changed:     {changed}")
    print(f"  gold matched:      {matched}/{len(gold)}")
    print(f"  success (finish):  {summary['success']}")
    print(f"  diff-block count:  {summary['diff_required_blocks']}  (times W3 blocked a skip-review finish)")
    print(f"  last_diff_turn:    {summary['last_diff_turn']}")
    print(f"  elapsed:           {elapsed}s")

    # Save artifacts
    (TRACES / f"{ts}_{args.instance_id}.patch").write_text(patch)
    (TRACES / f"{ts}_{args.instance_id}.json").write_text(
        json.dumps({**summary, "model_patch": patch,
                    "explore_summary": r.get("explore_summary", "")},
                   indent=2, default=str)
    )
    preds = TRACES / f"{ts}_{args.instance_id}_preds.jsonl"
    preds.write_text(json.dumps({
        "instance_id": args.instance_id,
        "model_name_or_path": "lean_v6",
        "model_patch": patch,
    }) + "\n")
    print(f"\n  Trace:       {log_path}")
    print(f"  Patch:       {TRACES}/{ts}_{args.instance_id}.patch")
    print(f"  Predictions: {preds}")
    print(f"\n  To grade with SWE-bench harness:")
    print(f"    /home/eeshu/Desktop/context/layer5-implementer/.venv/bin/python \\")
    print(f"      -m swebench.harness.run_evaluation \\")
    print(f"      --dataset_name SWE-bench/SWE-bench_Verified \\")
    print(f"      --predictions_path {preds} \\")
    print(f"      --max_workers 1 --run_id v6_{ts}")


if __name__ == "__main__":
    main()
