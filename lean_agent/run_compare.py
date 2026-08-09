#!/usr/bin/env python3
"""
Compare v2 / v3 / v4-adaptive on the same issue.

Runs each agent in sequence, saves a trace JSON to:
  traces_v2/<timestamp>_<issue_id>.json
  traces_v3/<timestamp>_<issue_id>.json
  traces_adaptive/<timestamp>_<issue_id>.json

Usage:
  python3 lean_agent/run_compare.py             # default: django-10554
  python3 lean_agent/run_compare.py --v2-only
  python3 lean_agent/run_compare.py --v3-only
  python3 lean_agent/run_compare.py --v4-only
"""
import os, sys, json, time, argparse, atexit
from pathlib import Path
from datetime import datetime


class Tee:
    """Duplicate stdout to a file so we always keep a log even if killed."""
    def __init__(self, path):
        self.file = open(path, "w", buffering=1)  # line-buffered
        self.stdout = sys.stdout

    def write(self, data):
        self.file.write(data)
        self.stdout.write(data)

    def flush(self):
        self.file.flush()
        self.stdout.flush()

    def close(self):
        self.file.close()

ROOT = Path(__file__).parent.parent
LEAN = ROOT / "lean_agent"

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

# ── Issue definition ───────────────────────────────────────────────────────────
ISSUE = {
    "id": "django-10554",
    "title": "Combined query results come back in wrong order after deriving a new queryset",
    "body": (
        "Users report that when they combine results from different database filters "
        "using union() and then sort them, a second evaluation of the original queryset "
        "returns wrong results. "
        "The bug only manifests after calling order_by().values_list() on the combined "
        "queryset — the original queryset is mutated and subsequent evaluations give "
        "incorrect output or raise database errors about missing columns. "
        "Plain querysets without union() are unaffected. The problem is in how "
        "the query object is cloned when deriving a new queryset."
    ),
    "repo_id": "django/django",
    "repo_path": str(ROOT / "repos" / "django_django"),
    "gold": ["django/db/models/sql/query.py"],
}


def _save_trace(folder: Path, issue_id: str, agent: str, result: dict, elapsed: float) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = folder / f"{ts}_{issue_id}.json"
    payload = {
        "agent": agent,
        "issue_id": issue_id,
        "timestamp": ts,
        "elapsed_s": round(elapsed, 1),
        **result,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def _print_result(agent: str, result: dict, elapsed: float, gold: list[str]):
    changed = result.get("files_changed", [])
    matched = [f for f in gold if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]
    print(f"\n  Agent:         {agent}")
    print(f"  Turns:         {result.get('total_turns')}  "
          f"(explore={result.get('explore_turns')}, write={result.get('write_turns')})")
    print(f"  Tokens:        {result.get('total_tokens', 0):,}")
    print(f"  Cache read:    {result.get('total_cache_read', 0):,}")
    print(f"  Files changed: {changed}")
    print(f"  Gold matched:  {len(matched)}/{len(gold)}  {matched}")
    print(f"  Success:       {result.get('success')}")
    print(f"  Time:          {elapsed:.1f}s")
    if result.get("tier_classified"):
        upgraded = f" → {result['tier_final']}" if result.get("tier_upgraded_from") else ""
        print(f"  Tier:          {result['tier_classified']}{upgraded}")
    if result.get("graph_tools_used"):
        print(f"  Graph tools:   {[t['tool'] for t in result['graph_tools_used']]}")


# ── Parse args ─────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser()
parser.add_argument("--v2-only", action="store_true")
parser.add_argument("--v3-only", action="store_true")
parser.add_argument("--v4-only", action="store_true")
parser.add_argument("--v5-only", action="store_true")
args = parser.parse_args()

any_only = args.v2_only or args.v3_only or args.v4_only or args.v5_only
run_v2 = args.v2_only or not any_only
run_v3 = args.v3_only or not any_only
run_v4 = args.v4_only or not any_only
run_v5 = args.v5_only   # v5 is opt-in (new experiment)

summary = []

print(f"\n{'='*65}")
print(f"  COMPARE: {ISSUE['id']}")
print(f"  Gold: {ISSUE['gold']}")
print(f"{'='*65}")

# ── v2 ─────────────────────────────────────────────────────────────────────────
if run_v2:
    from lean_agent.agent_v2 import run_lean_agent_v2
    print(f"\n{'─'*65}")
    print("  Running v2...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LEAN / "traces_v2" / f"{ts}_{ISSUE['id']}.log"
    tee = Tee(log_path)
    sys.stdout = tee
    try:
        t0 = time.time()
        r = run_lean_agent_v2(
            ticket_title=ISSUE["title"], ticket_body=ISSUE["body"],
            repo_id=ISSUE["repo_id"], repo_path=ISSUE["repo_path"],
            max_turns=50,
        )
        elapsed = time.time() - t0
    finally:
        sys.stdout = tee.stdout
        tee.close()
    _print_result("v2", r, elapsed, ISSUE["gold"])
    path = _save_trace(LEAN / "traces_v2", ISSUE["id"], "v2", r, elapsed)
    print(f"  Trace: {path.name} + {log_path.name}")
    summary.append(("v2", r, elapsed))

# ── v3 ─────────────────────────────────────────────────────────────────────────
if run_v3:
    from lean_agent.agent_v3 import run_lean_agent_v3
    print(f"\n{'─'*65}")
    print("  Running v3...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LEAN / "traces_v3" / f"{ts}_{ISSUE['id']}.log"
    tee = Tee(log_path)
    sys.stdout = tee
    try:
        t0 = time.time()
        r = run_lean_agent_v3(
            ticket_title=ISSUE["title"], ticket_body=ISSUE["body"],
            repo_id=ISSUE["repo_id"], repo_path=ISSUE["repo_path"],
            max_turns=60,
        )
        elapsed = time.time() - t0
    finally:
        sys.stdout = tee.stdout
        tee.close()
    _print_result("v3", r, elapsed, ISSUE["gold"])
    path = _save_trace(LEAN / "traces_v3", ISSUE["id"], "v3", r, elapsed)
    print(f"  Trace: {path.name} + {log_path.name}")
    summary.append(("v3", r, elapsed))

# ── v4 (adaptive) ──────────────────────────────────────────────────────────────
if run_v4:
    from lean_agent.agent_adaptive import run_adaptive_agent
    print(f"\n{'─'*65}")
    print("  Running v4 (adaptive)...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LEAN / "traces_adaptive" / f"{ts}_{ISSUE['id']}.log"
    tee = Tee(log_path)
    sys.stdout = tee
    try:
        t0 = time.time()
        r = run_adaptive_agent(
            ticket_title=ISSUE["title"], ticket_body=ISSUE["body"],
            repo_id=ISSUE["repo_id"], repo_path=ISSUE["repo_path"],
        )
        elapsed = time.time() - t0
    finally:
        sys.stdout = tee.stdout
        tee.close()
    _print_result("v4-adaptive", r, elapsed, ISSUE["gold"])
    path = _save_trace(LEAN / "traces_adaptive", ISSUE["id"], "v4-adaptive", r, elapsed)
    print(f"  Trace: {path.name} + {log_path.name}")
    summary.append(("v4-adaptive", r, elapsed))

# ── v5 (no tool swap — cache experiment) ──────────────────────────────────────
if run_v5:
    from lean_agent.agent_v5 import run_lean_agent_v5
    from lean_agent.classifier import route_issue
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
    tier, signals = route_issue(ISSUE["title"], ISSUE["body"], client)
    print(f"\n{'─'*65}")
    print(f"  Running v5 (stable tools, classifier tier={tier})...")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LEAN / "traces_v5" / f"{ts}_{ISSUE['id']}.log"
    tee = Tee(log_path)
    sys.stdout = tee
    try:
        t0 = time.time()
        # Tier-aware caps identical to adaptive's CONFIGS
        max_turns = {"easy": 40, "medium": 55, "hard": 80}[tier]
        nudge_after = {"easy": 10, "medium": 15, "hard": 20}[tier]
        r = run_lean_agent_v5(
            ticket_title=ISSUE["title"], ticket_body=ISSUE["body"],
            repo_id=ISSUE["repo_id"], repo_path=ISSUE["repo_path"],
            tier=tier, max_turns=max_turns, nudge_after_write=nudge_after,
        )
        elapsed = time.time() - t0
        r["tier_classified"] = tier
        r["tier_final"] = r.get("tier", tier)
        r["classifier_signals"] = signals
    finally:
        sys.stdout = tee.stdout
        tee.close()
    _print_result("v5-stable", r, elapsed, ISSUE["gold"])
    path = _save_trace(LEAN / "traces_v5", ISSUE["id"], "v5-stable", r, elapsed)
    print(f"  Trace: {path.name} + {log_path.name}")
    summary.append(("v5-stable", r, elapsed))

# ── Summary table ──────────────────────────────────────────────────────────────
if len(summary) > 1:
    gold = ISSUE["gold"]
    print(f"\n\n{'='*65}")
    print("  COMPARISON SUMMARY")
    print(f"{'='*65}")
    print(f"  {'Agent':<14} {'Turns':<6} {'Tokens':>9} {'CacheR':>8} {'Hit%':>5} {'Gold':<6} {'Done':<6} {'Time'}")
    print(f"  {'-'*75}")
    for agent, r, elapsed in summary:
        changed = r.get("files_changed", [])
        matched = [f for f in gold if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]
        tot = r.get("total_tokens", 0)
        cr = r.get("total_cache_read", 0)
        hit = f"{100*cr/tot:.0f}%" if tot else "—"
        print(f"  {agent:<14} {r.get('total_turns',0):<6} "
              f"{tot:>9,} {cr:>8,} {hit:>5} "
              f"{len(matched)}/{len(gold):<4}  "
              f"{str(r.get('success','?')):<6} {elapsed:.0f}s")
