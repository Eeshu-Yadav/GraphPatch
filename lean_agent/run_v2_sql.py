#!/usr/bin/env python3
"""Test: Lean agent v2 (single conversation) on django-10880"""
import os, sys, time
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

from lean_agent.agent_v2 import run_lean_agent_v2

TITLE = "Database query crashes when using conditional counting with unique values"
BODY = """
We're getting a database syntax error in production when we try to count
unique values with a condition. The query works fine without the condition,
and it works fine without the unique counting, but combining both together
produces invalid SQL.

The error seems to be a malformed query — like something is getting
concatenated wrong in the query builder. Two keywords are being smashed
together without a space between them.

This is happening on Django 2.2 and affects all database backends.
A developer reported seeing something like "DISTINCTCASE" in the generated
SQL, as if a space is missing between two SQL keywords.
"""

print("=" * 70)
print("  LEAN AGENT v2 — Single Conversation (SQL DISTINCT CASE bug)")
print("=" * 70)

start = time.time()
result = run_lean_agent_v2(
    ticket_title=TITLE, ticket_body=BODY,
    repo_id="django/django",
    repo_path=str(ROOT / "repos" / "django_django"),
    max_turns=30,
)
elapsed = time.time() - start

print("\n" + "=" * 70)
print("  RESULTS")
print("=" * 70)
print(f"  Success:        {result['success']}")
print(f"  Explore turns:  {result['explore_turns']}")
print(f"  Write turns:    {result['write_turns']}")
print(f"  Total turns:    {result['total_turns']}")
print(f"  Total tokens:   {result['total_tokens']:,}")
print(f"  Cache read:     {result['total_cache_read']:,}")
print(f"  Time:           {elapsed:.1f}s")
print(f"  Files to modify:{result['files_to_modify']}")
print(f"  Files changed:  {result['files_changed']}")

gold = ["django/db/models/aggregates.py"]
matched = [f for f in gold if any(f in c or c.endswith(f.split("/")[-1]) for c in result["files_changed"])]
print(f"\n  Gold matched: {len(matched)}/{len(gold)}")
print(f"  Explore tools: {[t['tool'] for t in result['explore_tools']]}")
print(f"  Write tools:   {[t['tool'] for t in result['write_tools']]}")

print(f"\n  v1 (two conversations): 9 explore + 30 write (never wrote) = 39 turns")
print(f"  v2 (single conversation): {result['explore_turns']} explore + {result['write_turns']} write = {result['total_turns']} turns")
