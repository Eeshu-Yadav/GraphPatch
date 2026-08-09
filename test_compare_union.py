#!/usr/bin/env python3
"""
Head-to-head: lean v2 vs hybrid on django-10554 (Union queryset ordering)
Gold: django/db/models/sql/compiler.py
"""
import os, sys, time
from pathlib import Path
ROOT = Path(__file__).parent
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

TITLE = "Combining database queries and then sorting them breaks when using derived queries"
BODY = """
When we combine multiple database queries together (like taking results from
two different filters and merging them), and then try to sort the combined
result, it works fine with simple queries. But if we first transform the
queries (like selecting specific columns or adding computed values) before
combining them, the sorting breaks.

The error seems to be in how the SQL is generated — the ORDER BY clause
references columns that don't exist in the combined output. It's like the
query builder doesn't properly handle column references when combining
transformed queries.

This affects any case where you combine queries that have been modified
before the combination, then try to sort the result.
"""

GOLD_FILES = ["django/db/models/sql/compiler.py"]
os.environ["AGENT_TRACE"] = "1"

# ═══════════════════════════════════════════════════════════════════
# RUN 1: Lean v2
# ═══════════════════════════════════════════════════════════════════
print("=" * 70)
print("  RUN 1: LEAN V2")
print("=" * 70)
from lean_agent.agent_v2 import run_lean_agent_v2
start1 = time.time()
result1 = run_lean_agent_v2(
    ticket_title=TITLE, ticket_body=BODY,
    repo_id="django/django", repo_path=str(ROOT / "repos" / "django_django"),
    max_turns=30,
)
elapsed1 = time.time() - start1
changed1 = result1['files_changed']
matched1 = [f for f in GOLD_FILES if any(f in c or c.endswith(f.split('/')[-1]) for c in changed1)]
print(f"\n  Lean v2: {result1['total_turns']} turns, {result1['total_tokens']:,} tokens, {elapsed1:.1f}s")
print(f"  Files changed: {changed1}")
print(f"  Gold matched: {len(matched1)}/{len(GOLD_FILES)}")
print(f"  Explore summary: {result1.get('explore_summary','')[:200]}")

# ═══════════════════════════════════════════════════════════════════
# RUN 2: Hybrid
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  RUN 2: HYBRID")
print("=" * 70)
from layer3_context.models.ticket import Ticket
from layer3_context.assembly.assembler import assemble
from layer45_agent.models import AgentConfig
from hybrid_agent.agent import run_hybrid_agent

ticket = Ticket(ticket_id="COMPARE-UNION-001", title=TITLE, body=BODY, repo_id="django/django")
bundle = assemble(ticket)
config = AgentConfig(
    model="claude-sonnet-4-20250514",
    explore_model="claude-sonnet-4-20250514",
    plan_model="claude-opus-4-6",
    write_model="claude-sonnet-4-20250514",
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    max_iterations=30,
)
start2 = time.time()
result2 = run_hybrid_agent(ticket, bundle, config)
elapsed2 = time.time() - start2
impl = result2.implementation
changed2 = [fr.file_path for fr in impl.file_results] if impl and impl.file_results else []
matched2 = [f for f in GOLD_FILES if any(f in c for c in changed2)]
print(f"\n  Hybrid: {result2.iterations} turns, {result2.total_prompt_tokens:,} tokens, {elapsed2:.1f}s")
print(f"  Files changed: {changed2}")
print(f"  Gold matched: {len(matched2)}/{len(GOLD_FILES)}")

# ═══════════════════════════════════════════════════════════════════
# COMPARISON
# ═══════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  HEAD-TO-HEAD")
print("=" * 70)
print(f"  {'Metric':<25} {'Lean v2':<25} {'Hybrid':<25}")
print(f"  {'-'*75}")
print(f"  {'Turns':<25} {result1['total_turns']:<25} {result2.iterations:<25}")
print(f"  {'Tokens':<25} {result1['total_tokens']:,<25} {result2.total_prompt_tokens:,<25}")
print(f"  {'Time':<25} {elapsed1:.1f}s{'':<20} {elapsed2:.1f}s")
print(f"  {'Files changed':<25} {len(changed1):<25} {len(changed2):<25}")
print(f"  {'Gold matched':<25} {len(matched1)}/{len(GOLD_FILES):<23} {len(matched2)}/{len(GOLD_FILES)}")
