#!/usr/bin/env python3
"""Test: Hybrid agent on django-10880 — same vague SQL bug ticket"""
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

os.environ["AGENT_TRACE"] = "1"

from layer3_context.models.ticket import Ticket
from layer3_context.assembly.assembler import assemble
from layer45_agent.models import AgentConfig
from hybrid_agent.agent import run_hybrid_agent

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
print("  HYBRID AGENT — layer45 infra + lean v2 patterns")
print("=" * 70)

ticket = Ticket(ticket_id="HYBRID-SQL-001", title=TITLE, body=BODY, repo_id="django/django")
print("[Layer 3] Assembling context...")
bundle = assemble(ticket)
print(f"  Symbols: {len(bundle.relevant_symbols)}, Files: {len(bundle.relevant_files)}")

config = AgentConfig(
    model="claude-sonnet-4-20250514",
    explore_model="claude-sonnet-4-20250514",
    plan_model="claude-opus-4-6",
    write_model="claude-sonnet-4-20250514",
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    max_iterations=30,
    max_explore_iterations=50,
    max_write_no_test=15,
    max_total_tokens=5_000_000,
    max_repair_iterations=10,
)

start = time.time()
result = run_hybrid_agent(ticket, bundle, config)
elapsed = time.time() - start

print("\n" + "=" * 70)
print("  RESULTS")
print("=" * 70)
print(f"  Success:    {result.success}")
print(f"  Iterations: {result.iterations}")
print(f"  Tokens:     {result.total_prompt_tokens:,} prompt + {result.total_completion_tokens:,} completion")
print(f"  Time:       {elapsed:.1f}s")

impl = result.implementation
if impl and impl.file_results:
    changed = [fr.file_path for fr in impl.file_results]
    print(f"  Files changed: {changed}")
    gold = ["django/db/models/aggregates.py"]
    matched = [f for f in gold if any(f in c for c in changed)]
    print(f"  Gold matched: {len(matched)}/{len(gold)}")
else:
    print("  Files changed: 0")
    print("  Gold matched: 0/1")

print(f"\n  COMPARISON:")
print(f"    layer45 (35 tools):     0/1 gold, 190K tokens, 253s")
print(f"    lean v2 (6 tools):      1/1 gold, 180K tokens, 110s")
print(f"    hybrid (lean in layer45): {result.iterations} turns, {result.total_prompt_tokens:,} tokens, {elapsed:.1f}s")
