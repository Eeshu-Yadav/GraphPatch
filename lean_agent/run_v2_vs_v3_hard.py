#!/usr/bin/env python3
"""
HARD TEST: v2 vs v3 on django-10554 (Union queryset ordering)
Deep ORM internals, SQL compiler, hardest Django bug in our set.
Gold: django/db/models/sql/compiler.py
"""
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

TITLE = "Combined query results come back in wrong order"
BODY = """
Users report that when they merge results from different database filters
and then sort them, the sorting is completely wrong. Sometimes it crashes
with a database error about missing columns.

It only happens when the individual queries are transformed before merging.
For example, if you select specific columns or add computed values to each
query, then combine them, and then try to sort — the sort references columns
that don't exist in the combined result.

Plain queries combined and sorted work fine. It's only when the queries
are derived (values_list, annotate, etc.) before being combined.
"""

GOLD_FILES = ["django/db/models/sql/compiler.py"]

# ═══════════════════════════════════════════════════════════════
# RUN 1: v2 (6 explore + 7 write, no graph tools in write)
# ═══════════════════════════════════════════════════════════════
print("=" * 70)
print("  RUN 1: LEAN V2 (6+7 tools, no graph in write)")
print("=" * 70)

from lean_agent.agent_v2 import run_lean_agent_v2
start1 = time.time()
result1 = run_lean_agent_v2(
    ticket_title=TITLE, ticket_body=BODY,
    repo_id="django/django", repo_path=str(ROOT / "repos" / "django_django"),
    max_turns=200,
)
elapsed1 = time.time() - start1
changed1 = result1['files_changed']
matched1 = [f for f in GOLD_FILES if any(f in c or c.endswith(f.split('/')[-1]) for c in changed1)]
print(f"\n  v2: {result1['total_turns']} turns, {result1['total_tokens']:,} tokens, {elapsed1:.1f}s")
print(f"  Explore: {result1['explore_turns']}, Write: {result1['write_turns']}")
print(f"  Files to modify: {result1['files_to_modify']}")
print(f"  Files changed: {changed1}")
print(f"  Gold matched: {len(matched1)}/{len(GOLD_FILES)}")
print(f"  Explore summary: {result1.get('explore_summary','')[:200]}")

# ═══════════════════════════════════════════════════════════════
# RUN 2: v3 (6 explore + 12 write with graph tools)
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  RUN 2: LEAN V3 (6+12 tools, graph tools in write)")
print("=" * 70)

from lean_agent.agent_v3 import run_lean_agent_v3
start2 = time.time()
result2 = run_lean_agent_v3(
    ticket_title=TITLE, ticket_body=BODY,
    repo_id="django/django", repo_path=str(ROOT / "repos" / "django_django"),
    max_turns=50,
)
elapsed2 = time.time() - start2
changed2 = result2['files_changed']
matched2 = [f for f in GOLD_FILES if any(f in c or c.endswith(f.split('/')[-1]) for c in changed2)]
print(f"\n  v3: {result2['total_turns']} turns, {result2['total_tokens']:,} tokens, {elapsed2:.1f}s")
print(f"  Explore: {result2['explore_turns']}, Write: {result2['write_turns']}")
print(f"  Files to modify: {result2['files_to_modify']}")
print(f"  Files changed: {changed2}")
print(f"  Gold matched: {len(matched2)}/{len(GOLD_FILES)}")
print(f"  Graph tools used: {[t['tool'] for t in result2.get('graph_tools_used', [])]}")
print(f"  Explore summary: {result2.get('explore_summary','')[:200]}")

# ═══════════════════════════════════════════════════════════════
# COMPARISON
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  HEAD-TO-HEAD: HARD ISSUE (union queryset ordering)")
print("=" * 70)
print(f"  {'Metric':<25} {'v2 (no graph write)':<25} {'v3 (graph write)':<25}")
print(f"  {'-'*75}")
print(f"  {'Explore turns':<25} {result1['explore_turns']:<25} {result2['explore_turns']:<25}")
print(f"  {'Write turns':<25} {result1['write_turns']:<25} {result2['write_turns']:<25}")
print(f"  {'Total turns':<25} {result1['total_turns']:<25} {result2['total_turns']:<25}")
print(f"  {'Tokens':<25} {result1['total_tokens']:,<25} {result2['total_tokens']:,<25}")
print(f"  {'Time':<25} {elapsed1:.0f}s{'':<22} {elapsed2:.0f}s")
print(f"  {'Files changed':<25} {len(changed1):<25} {len(changed2):<25}")
print(f"  {'Gold matched':<25} {len(matched1)}/{len(GOLD_FILES):<23} {len(matched2)}/{len(GOLD_FILES)}")
print(f"  {'Graph tools used':<25} {'0':<25} {len(result2.get('graph_tools_used', []))}")
print(f"  {'Found compiler.py?':<25} {'compiler.py' in str(result1.get('files_to_modify', [])):<25} {'compiler.py' in str(result2.get('files_to_modify', []))}")
