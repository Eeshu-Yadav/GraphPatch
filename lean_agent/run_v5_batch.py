#!/usr/bin/env python3
"""
Batch runner for v5-stable on a range of SWE-bench Verified instances.

Picks middle-range django+astropy issues (already-indexed repos), runs each
through v5, saves per-instance trace to traces_v5/, and writes a summary JSON.

IMPORTANT:
  • Resets the repo (git reset --hard + clean -fd) before each run so runs
    are independent (doesn't carry over prior modifications).
  • Checks out the instance's base_commit before exploring.
  • Uses the REAL problem_statement as the ticket body (not vague).
  • Gold = instance.patch touched files (parsed from the diff).

Usage:
    python3 lean_agent/run_v5_batch.py                 # run middle 20
    python3 lean_agent/run_v5_batch.py --limit 5       # smaller batch
    python3 lean_agent/run_v5_batch.py --offset 126    # custom start
"""
from __future__ import annotations

import os, sys, json, time, argparse, subprocess, re
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent.parent
LEAN = ROOT / "lean_agent"
TRACES = LEAN / "traces_v5"
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
from lean_agent.agent_v5 import run_lean_agent_v5
from lean_agent.classifier import route_issue
from lean_agent.vagify import vagify
import anthropic


# ── Helpers ────────────────────────────────────────────────────────────────────

_FILE_RE = re.compile(r"^diff --git a/([^ ]+) b/", re.MULTILINE)

def gold_files_from_patch(patch: str) -> list[str]:
    """Extract touched files from a unified git diff."""
    return _FILE_RE.findall(patch or "")


def reset_repo(repo_path: Path, base_commit: str):
    """Reset to base_commit, clean untracked (survives prior agent runs)."""
    subprocess.run(["git", "-C", str(repo_path), "reset", "--hard", base_commit],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo_path), "clean", "-fd"],
                   check=True, capture_output=True)


def count_matched(gold: list[str], changed: list[str]) -> int:
    return sum(
        1 for f in gold
        if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)
    )


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="how many instances")
    ap.add_argument("--offset", type=int, default=None,
                    help="index into filtered list; default = middle")
    ap.add_argument("--repos", nargs="+", default=["django/django", "astropy/astropy"],
                    help="only run instances for these repos (must be cloned)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print picks without running")
    ap.add_argument("--no-vague", action="store_true",
                    help="pass the REAL problem_statement (default: Haiku-rewrite to vague)")
    args = ap.parse_args()

    print(f"Loading SWE-bench Verified...")
    ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    repo_allow = set(args.repos)
    filtered = [(i, ds[i]) for i in range(len(ds)) if ds[i]["repo"] in repo_allow]
    print(f"  {len(ds)} total, {len(filtered)} matching {sorted(repo_allow)}")

    offset = args.offset if args.offset is not None else (len(filtered) // 2)
    picks = filtered[offset : offset + args.limit]
    print(f"  Running {len(picks)} instances starting at filtered idx {offset}")
    for i, inst in picks:
        print(f"    [{i}] {inst['instance_id']}")

    if args.dry_run:
        return

    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    results = []
    batch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    for idx, (ds_idx, inst) in enumerate(picks, start=1):
        iid = inst["instance_id"]
        repo_id = inst["repo"]
        repo_slug = repo_id.replace("/", "_")
        repo_path = ROOT / "repos" / repo_slug
        base_commit = inst["base_commit"]
        real_title = iid
        real_body = inst["problem_statement"][:3000]
        gold = gold_files_from_patch(inst["patch"])

        # Vagify the ticket (default) so the agent has to DISCOVER the file,
        # not just read the path off the problem_statement.
        if args.no_vague:
            title, body = real_title, real_body
            ticket_style = "real SWE-bench (not vague)"
        else:
            title, body = vagify(real_title, real_body, client)
            ticket_style = "vagified via Haiku"

        print(f"\n{'='*70}")
        print(f"[{idx}/{len(picks)}]  {iid}   [{ticket_style}]")
        print(f"  ds_idx={ds_idx}  repo={repo_id}  base={base_commit[:10]}")
        print(f"  gold_files ({len(gold)}): {gold[:3]}{'…' if len(gold) > 3 else ''}")
        if not args.no_vague:
            print(f"  vague body preview: {body[:160]}...")

        if not repo_path.exists():
            print(f"  SKIP — repo not cloned at {repo_path}")
            continue

        # Reset repo to instance's base_commit
        try:
            reset_repo(repo_path, base_commit)
        except subprocess.CalledProcessError as e:
            print(f"  SKIP — git reset failed: {e.stderr.decode()[:100]}")
            continue

        # Classify
        tier, signals = route_issue(title, body, client)
        max_turns = {"easy": 40, "medium": 55, "hard": 80}[tier]
        nudge_after = {"easy": 10, "medium": 15, "hard": 20}[tier]
        print(f"  tier={tier}  max_turns={max_turns}  signals={signals}")

        # Run v5, tee'd to per-instance log
        log_path = TRACES / f"{batch_ts}_{iid}.log"
        original_stdout = sys.stdout
        class Tee:
            def __init__(self, p):
                self.f = open(p, "w", buffering=1); self.s = original_stdout
            def write(self, d):
                self.f.write(d); self.s.write(d)
            def flush(self): self.f.flush(); self.s.flush()
            def close(self): self.f.close()
        tee = Tee(log_path)
        sys.stdout = tee

        err = None
        t0 = time.time()
        try:
            r = run_lean_agent_v5(
                ticket_title=title, ticket_body=body,
                repo_id=repo_id, repo_path=str(repo_path),
                tier=tier, max_turns=max_turns, nudge_after_write=nudge_after,
            )
        except KeyboardInterrupt:
            sys.stdout = original_stdout; tee.close()
            print("\n  INTERRUPTED — breaking batch"); break
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:200]}"
            r = {}
        finally:
            sys.stdout = original_stdout
            tee.close()
        elapsed = round(time.time() - t0, 1)

        # CAPTURE the unified diff NOW (before the next iteration resets the repo).
        # This is what SWE-bench evaluator needs as `model_patch`.
        try:
            model_patch = subprocess.run(
                ["git", "-C", str(repo_path), "diff", base_commit],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            model_patch = ""
            print(f"  WARN — could not capture model_patch: {e}")

        changed = r.get("files_changed", [])
        matched = count_matched(gold, changed)
        summary_row = {
            "instance_id": iid,
            "ds_idx": ds_idx,
            "repo": repo_id,
            "tier": tier,
            "tier_signals": signals,
            "ticket_style": ticket_style,
            "gold_files": gold,
            "gold_count": len(gold),
            "files_changed": changed,
            "model_patch_chars": len(model_patch),
            "gold_matched": matched,
            "total_turns": r.get("total_turns", 0),
            "explore_turns": r.get("explore_turns", 0),
            "write_turns": r.get("write_turns", 0),
            "total_tokens": r.get("total_tokens", 0),
            "cache_read": r.get("total_cache_read", 0),
            "cache_create": r.get("total_cache_create", 0),
            "success": r.get("success", False),
            "elapsed_s": elapsed,
            "error": err,
            "log_file": log_path.name,
        }
        # Save the patch as its own .patch file (easier to inspect)
        (TRACES / f"{batch_ts}_{iid}.patch").write_text(model_patch)
        # Save a structured per-instance json INCLUDING the vague body we fed it
        (TRACES / f"{batch_ts}_{iid}.json").write_text(
            json.dumps({
                **summary_row,
                "real_problem_statement": real_body[:1500],
                "vague_title": title,
                "vague_body": body,
                "explore_summary": r.get("explore_summary", ""),
                "model_patch": model_patch,
            }, indent=2, default=str)
        )
        results.append(summary_row)

        # Single-line progress
        hit = ""
        if r.get("total_tokens"):
            hit = f" hit={100*r.get('total_cache_read',0)/r['total_tokens']:.0f}%"
        print(f"  → success={r.get('success')}  turns={r.get('total_turns')} "
              f"tokens={r.get('total_tokens',0):,}{hit} gold={matched}/{len(gold)} {elapsed}s")
        if err: print(f"  ERROR: {err}")

    # ── Batch summary ─────────────────────────────────────────────────────────
    if results:
        summary_path = TRACES / f"{batch_ts}_BATCH_SUMMARY.json"
        summary_path.write_text(json.dumps(results, indent=2, default=str))
        print(f"\n{'='*70}")
        print(f"  BATCH DONE — {len(results)} instances")
        print(f"{'='*70}")
        print(f"  {'instance':<32} {'tier':<7} {'turn':>4} {'tokens':>9} {'hit':>4} {'gold':<6} {'done'}")
        for r in results:
            hit = f"{100*r['cache_read']/r['total_tokens']:.0f}%" if r['total_tokens'] else "—"
            print(f"  {r['instance_id']:<32} {r['tier']:<7} {r['total_turns']:>4} "
                  f"{r['total_tokens']:>9,} {hit:>4} "
                  f"{r['gold_matched']}/{r['gold_count']:<4} {str(r['success'])}")
        tot_tok = sum(r["total_tokens"] for r in results)
        tot_cache = sum(r["cache_read"] for r in results)
        wins = sum(1 for r in results if r["success"])
        gold_hits = sum(1 for r in results if r["gold_matched"] > 0)
        print(f"\n  Totals: tokens={tot_tok:,}  cached={tot_cache:,}  "
              f"hit%={100*tot_cache/tot_tok:.1f}%  "
              f"finished={wins}/{len(results)}  gold-hit={gold_hits}/{len(results)}")
        print(f"  Saved: {summary_path.name}")

        # Write predictions JSONL for SWE-bench harness evaluation
        preds_path = TRACES / f"{batch_ts}_predictions.jsonl"
        with open(preds_path, "w") as f:
            for row in results:
                # Re-load the patch text for each instance
                patch_file = TRACES / f"{batch_ts}_{row['instance_id']}.patch"
                patch = patch_file.read_text() if patch_file.exists() else ""
                f.write(json.dumps({
                    "instance_id": row["instance_id"],
                    "model_name_or_path": "lean_v5_stable",
                    "model_patch": patch,
                }) + "\n")
        print(f"  Predictions: {preds_path.name}")
        print(f"\n  To grade with SWE-bench harness:")
        print(f"    python -m swebench.harness.run_evaluation \\")
        print(f"      --dataset_name SWE-bench/SWE-bench_Verified \\")
        print(f"      --predictions_path {preds_path} \\")
        print(f"      --max_workers 4 --run_id v5_{batch_ts}")


if __name__ == "__main__":
    main()
