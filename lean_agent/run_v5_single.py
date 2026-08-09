#!/usr/bin/env python3
"""
Run v5 on a single SWE-bench instance — used to retest after fixes.

Usage:
    python3 lean_agent/run_v5_single.py django__django-14007
    python3 lean_agent/run_v5_single.py django__django-14007 --no-vague
"""
import os, sys, json, time, argparse, subprocess, re
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
LEAN = ROOT / "lean_agent"
TRACES = LEAN / "traces_v5"

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
from lean_agent.agent_v5 import run_lean_agent_v5
from lean_agent.classifier import route_issue
from lean_agent.tier_config import TierConfig
from lean_agent.vagify import vagify
import anthropic

_FILE_RE = re.compile(r"^diff --git a/([^ ]+) b/", re.MULTILINE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("instance_id", help="e.g. django__django-14007")
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

    print(f"  repo={repo_id}  base={base_commit[:10]}  gold={gold}")

    # Reset repo to clean base_commit
    subprocess.run(["git", "-C", str(repo_path), "reset", "--hard", base_commit],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "clean", "-fd"],
                   check=True, capture_output=True)

    # Vagify (default) so the agent has to discover the file
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    if args.no_vague:
        title, body = args.instance_id, real_body
    else:
        title, body = vagify(args.instance_id, real_body, client)

    # Classify and pick initial tier config
    tier, signals = route_issue(title, body, client)
    cfg = TierConfig.for_tier(tier)
    print(f"  tier={tier}  max_turns={cfg.max_turns}  nudge_after={cfg.nudge_after_write}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = TRACES / f"{ts}_{args.instance_id}_RETEST.log"

    class Tee:
        def __init__(self, p):
            self.f = open(p, "w", buffering=1); self.s = sys.stdout
        def write(self, d): self.f.write(d); self.s.write(d)
        def flush(self):    self.f.flush(); self.s.flush()
        def close(self):    self.f.close()

    orig_stdout = sys.stdout
    tee = Tee(log_path)
    sys.stdout = tee
    t0 = time.time()
    try:
        r = run_lean_agent_v5(
            ticket_title=title, ticket_body=body,
            repo_id=repo_id, repo_path=str(repo_path),
            tier=tier, max_turns=cfg.max_turns, nudge_after_write=cfg.nudge_after_write,
        )
    finally:
        sys.stdout = orig_stdout
        tee.close()
    elapsed = round(time.time() - t0, 1)

    changed = r.get("files_changed", [])
    matched = [f for f in gold if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]
    hit = 100 * r.get("total_cache_read", 0) / r.get("total_tokens", 1)

    print(f"\n{'='*70}\n  RETEST RESULT — {args.instance_id}\n{'='*70}")
    print(f"  tier:            {r.get('tier')}  (upgraded from {r.get('tier_upgraded_from')})")
    print(f"  turns:           {r.get('total_turns')} (explore={r.get('explore_turns')}, write={r.get('write_turns')})")
    print(f"  tokens billed:   {r.get('total_tokens', 0):,}")
    print(f"  cache read:      {r.get('total_cache_read', 0):,}  (hit {hit:.0f}%)")
    print(f"  files changed:   {changed}")
    print(f"  gold matched:    {len(matched)}/{len(gold)}  {matched}")
    print(f"  success:         {r.get('success')}")
    print(f"  elapsed:         {elapsed}s")
    print(f"  log: {log_path}")

    out_path = TRACES / f"{ts}_{args.instance_id}_RETEST.json"
    out_path.write_text(json.dumps({
        "instance_id": args.instance_id, "tier": tier, "tier_signals": signals,
        "ticket_style": "real" if args.no_vague else "vagified",
        "gold_files": gold, "files_changed": changed, "gold_matched": len(matched),
        "elapsed_s": elapsed, **r,
    }, indent=2, default=str))


if __name__ == "__main__":
    main()
