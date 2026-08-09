#!/usr/bin/env python3
"""
Test adaptive agent on django-30108 — a hard, untested, multi-backend issue.

Issue: Adding ForeignKey fields via migrations defers the constraint to a
separate SQL statement even on databases that support inline FK definitions.

Gold files (6 source files):
  django/db/backends/base/features.py
  django/db/backends/base/schema.py
  django/db/backends/mysql/schema.py
  django/db/backends/oracle/schema.py
  django/db/backends/postgresql/schema.py
  django/db/backends/sqlite3/features.py

Expected:
  - Classifier should route to HARD (cross-backend schema issue with many keywords)
    or start at MEDIUM and Layer 4 upgrades when agent finds 4+ files
  - Total turns: ~50-80
  - Gold matched: ideally 2+ of 6 (base/schema.py + base/features.py are the core)
"""
import os, sys, json, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
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

from lean_agent.agent_adaptive import run_adaptive_agent

ISSUE = {
    "id": "django-30108",
    "title": "Adding new database fields with foreign keys skips the constraint on some backends",
    "body": (
        "When running migrations that add a new ForeignKey field to an existing model, "
        "the database constraint for that foreign key is always created in a separate "
        "ALTER TABLE statement, even on databases that support defining the constraint "
        "inline when the column is first added. "
        "This means there is a brief window where the column exists without its referential "
        "integrity constraint, which can be problematic in concurrent environments. "
        "The behaviour should differ by backend: databases that support inline FK definitions "
        "should attach the constraint in the same CREATE/ALTER statement, while others "
        "should continue to use the deferred approach. "
        "The base schema editor and each backend's schema module will likely need to "
        "be updated so the right SQL template is picked at migration time."
    ),
    "repo_id": "django/django",
    "repo_path": str(ROOT / "repos" / "django_django"),
    "gold": [
        "django/db/backends/base/features.py",
        "django/db/backends/base/schema.py",
        "django/db/backends/mysql/schema.py",
        "django/db/backends/oracle/schema.py",
        "django/db/backends/postgresql/schema.py",
        "django/db/backends/sqlite3/features.py",
    ],
    "expected_tier": "hard",
}

print(f"\n{'='*65}")
print(f"  {ISSUE['id']}  (expected tier: {ISSUE['expected_tier']})")
print(f"{'='*65}")
print(f"  Gold files ({len(ISSUE['gold'])}):")
for f in ISSUE["gold"]:
    print(f"    {f}")
print()

result = run_adaptive_agent(
    ticket_title=ISSUE["title"],
    ticket_body=ISSUE["body"],
    repo_id=ISSUE["repo_id"],
    repo_path=ISSUE["repo_path"],
)

changed = result.get("files_changed", [])
matched = [f for f in ISSUE["gold"]
           if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]

tier_ok = "✅" if result["tier_classified"] == ISSUE["expected_tier"] else "❌"
upgraded = f" → {result['tier_final']}" if result.get("tier_upgraded_from") else ""

print(f"\n{'='*65}")
print(f"  RESULTS")
print(f"{'='*65}")
print(f"  Tier classified: {result['tier_classified']} {tier_ok}{upgraded}")
print(f"  Total turns:     {result['total_turns']}  "
      f"(explore={result['explore_turns']}, write={result['write_turns']})")
print(f"  Tokens:          {result['total_tokens']:,}")
print(f"  Cache read:      {result['total_cache_read']:,}")
print(f"  Cache create:    {result['total_cache_create']:,}")
print(f"  Files changed:   {changed}")
print(f"  Gold matched:    {len(matched)}/{len(ISSUE['gold'])}  {matched}")
print(f"  Success:         {result['success']}")
print(f"  Time:            {result['elapsed_s']}s")
print(f"  Graph tools:     {[t['tool'] for t in result.get('graph_tools_used', [])]}")
print(f"  Signals:         {result['classifier_signals']}")
print(f"  Explore summary: {result.get('explore_summary', '')[:200]}")

traces_dir = ROOT / "lean_agent" / "traces_adaptive"
traces_dir.mkdir(exist_ok=True)
ts = time.strftime("%Y%m%d_%H%M%S")
output = traces_dir / f"{ts}_{ISSUE['id']}.json"
with open(output, "w") as f:
    json.dump({
        "id": ISSUE["id"],
        "expected_tier": ISSUE["expected_tier"],
        "tier_classified": result["tier_classified"],
        "tier_final": result["tier_final"],
        "tier_ok": result["tier_classified"] == ISSUE["expected_tier"],
        "upgraded": result.get("tier_upgraded_from"),
        "total_turns": result["total_turns"],
        "explore_turns": result["explore_turns"],
        "write_turns": result["write_turns"],
        "tokens": result["total_tokens"],
        "cache_read": result["total_cache_read"],
        "cache_create": result["total_cache_create"],
        "gold_matched": f"{len(matched)}/{len(ISSUE['gold'])}",
        "matched_files": matched,
        "files_changed": changed,
        "success": result["success"],
        "elapsed_s": result["elapsed_s"],
        "signals": result["classifier_signals"],
        "graph_tools": [t["tool"] for t in result.get("graph_tools_used", [])],
        "explore_summary": result.get("explore_summary", ""),
    }, f, indent=2)
print(f"\n  Saved to {output}")
