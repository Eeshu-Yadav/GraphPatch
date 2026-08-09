#!/usr/bin/env python3
"""
Test: Lean agent on django-10880 (COUNT DISTINCT CASE missing space)
Extremely vague ticket — no SQL, no Django ORM terms, no file paths.
Gold: 1 file (django/db/models/aggregates.py), 1-line fix
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent

sys.path.insert(0, str(ROOT / "layer2-indexer" / "src"))
sys.path.insert(0, str(ROOT / "layer3-context"))
sys.path.insert(0, str(ROOT / "layer4-planner"))
sys.path.insert(0, str(ROOT / "layer45-agent"))
sys.path.insert(0, str(ROOT / "layer5-implementer"))
sys.path.insert(0, str(ROOT / "layer6-validator"))
sys.path.insert(0, str(ROOT))

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from lean_agent.agent import run_lean_agent

# ═══════════════════════════════════════════════════════════════════════════
# EXTREMELY VAGUE TICKET
# The actual bug: COUNT(DISTINCT CASE WHEN...) generates COUNT(DISTINCTCASE WHEN...)
# Missing space between DISTINCT and CASE in SQL output
# ═══════════════════════════════════════════════════════════════════════════

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
The issue appears when using annotations with both a condition filter
and distinct counting together.

A developer reported seeing something like "DISTINCTCASE" in the generated
SQL, as if a space is missing between two SQL keywords.
"""

repo_id = "django/django"
repo_path = str(ROOT / "repos" / "django_django")

print("=" * 70)
print("  LEAN AGENT — VAGUE SQL Bug (COUNT DISTINCT CASE)")
print("=" * 70)
print(f"  Title: {TITLE}")
print(f"  Repo: {repo_id}")
print(f"  Gold: 1 file (django/db/models/aggregates.py)")
print("=" * 70)

start = time.time()
result = run_lean_agent(
    ticket_title=TITLE,
    ticket_body=BODY,
    repo_id=repo_id,
    repo_path=repo_path,
    max_explore_turns=15,
    max_write_turns=30,
)
elapsed = time.time() - start

print("\n" + "=" * 70)
print("  RESULTS")
print("=" * 70)
print(f"  Success:         {result['success']}")
print(f"  Explore turns:   {result['explore_turns']}")
print(f"  Write turns:     {result['write_turns']}")
print(f"  Total turns:     {result['total_turns']}")
print(f"  Explore tokens:  {result['explore_tokens']:,}")
print(f"  Write tokens:    {result['write_tokens']:,}")
print(f"  Total tokens:    {result['total_tokens']:,}")
print(f"  Cache read:      {result.get('total_cache_read', 0):,}")
print(f"  Time:            {elapsed:.1f}s")
print(f"  Graph context:   {result['graph_context_chars']} chars")
print(f"  Files to modify: {result['files_to_modify']}")
print(f"  Files changed:   {result['files_changed']}")

gold_files = ["django/db/models/aggregates.py"]
changed = result["files_changed"]
matched = [f for f in gold_files if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]
print(f"\n  Gold files matched: {len(matched)}/{len(gold_files)}")
print(f"    Matched: {matched}")
print(f"    Missed:  {[f for f in gold_files if f not in matched]}")

print(f"\n  Explore summary: {result.get('explore_summary', '')[:300]}")
