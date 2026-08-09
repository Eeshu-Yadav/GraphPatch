#!/usr/bin/env python3
"""v3 on django-10554 (union queryset ordering) — hardest issue"""
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).parent.parent
for p in ["layer2-indexer/src", "layer3-context", "layer4-planner", "layer45-agent",
          "layer5-implementer", "layer6-validator", "."]:
    sys.path.insert(0, str(ROOT / p))
for line in open(ROOT / ".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from lean_agent.agent_v3 import run_lean_agent_v3

TITLE = "Combined query results come back in wrong order"
BODY = """
Users report that when they merge results from different database filters
and then sort them, the sorting is completely wrong. Sometimes it crashes
with a database error about missing columns. It only happens when the
individual queries are transformed before merging. Plain queries sort fine.
"""

print("=" * 70)
print("  LEAN V3 — HARD ISSUE (union queryset ordering)")
print("=" * 70)

start = time.time()
result = run_lean_agent_v3(
    ticket_title=TITLE, ticket_body=BODY,
    repo_id="django/django", repo_path=str(ROOT / "repos" / "django_django"),
    max_turns=80,
)
elapsed = time.time() - start

gold = ["django/db/models/sql/compiler.py"]
matched = [f for f in gold if any(f in c or c.endswith(f.split("/")[-1]) for c in result["files_changed"])]

print("\n" + "=" * 70)
print("  RESULTS")
print("=" * 70)
print(f"  Explore: {result['explore_turns']}, Write: {result['write_turns']}, Total: {result['total_turns']}")
print(f"  Tokens: {result['total_tokens']:,}, Cache: {result['total_cache_read']:,}")
print(f"  Time: {elapsed:.1f}s")
print(f"  Files to modify: {result['files_to_modify']}")
print(f"  Files changed: {result['files_changed']}")
print(f"  Graph tools: {[t['tool'] for t in result.get('graph_tools_used', [])]}")
print(f"  Gold matched: {len(matched)}/{len(gold)}")
print(f"  Finished: {result['success']}")
print(f"\n  v2 on same issue: explore=30, wrote to compiler.py at turn 43, killed at turn 73 (no finish)")
