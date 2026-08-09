#!/usr/bin/env python3
"""
Test: Can the pipeline solve a HARD, MULTI-FILE vague ticket?

astropy-13398: ITRS to Observed coordinate transforms
- Gold solution requires 4 files changed
- Previous best: 1/4 files (partial)
- This is a FEATURE addition, not a bug fix

We make the ticket VAGUE:
- No mention of specific files
- No mention of ITRS, AltAz, HADec class names
- No code snippets
- Just a user complaint about inaccurate satellite position calculations
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent

sys.path.insert(0, str(ROOT / "layer2-indexer" / "src"))
sys.path.insert(0, str(ROOT / "layer3-context"))
sys.path.insert(0, str(ROOT / "layer4-planner"))
sys.path.insert(0, str(ROOT / "layer45-agent"))
sys.path.insert(0, str(ROOT / "layer5-implementer"))
sys.path.insert(0, str(ROOT / "layer6-validator"))
sys.path.insert(0, str(ROOT / "layer7-pr-publisher"))

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
from layer45_agent.agent import run_agent
from layer45_agent.models import AgentConfig

# ═══════════════════════════════════════════════════════════════════════════
# THE VAGUE TICKET — no class names, no file paths, no code
# ═══════════════════════════════════════════════════════════════════════════

VAGUE_TITLE = "Satellite position calculations are inaccurate when converting between Earth-fixed and observer coordinates"

VAGUE_BODY = """
We keep getting reports from users trying to track satellites and aircraft
that the coordinate transformations between the Earth-fixed reference frame
and the observer's local horizon frame (altitude/azimuth) are giving wrong
results. The positions are off by a significant amount.

The root issue seems to be that the current transformation path goes through
intermediate celestial frames, which introduces unnecessary errors from
aberration corrections that don't apply to nearby objects like satellites.

Users want a direct transformation that stays in the terrestrial reference
frame. Instead of going Earth-fixed → celestial → back to observer, it
should just do a simple geometric rotation from Earth-fixed coordinates
directly to the observer's local horizon.

This has been raised multiple times by different users working with satellite
tracking, aircraft observation, and even terrestrial line-of-sight calculations.

Can we add a direct transformation path that avoids the celestial detour?
"""

# ═══════════════════════════════════════════════════════════════════════════

repo_id = "astropy/astropy"
ticket_id = "VAGUE-ASTROPY-001"

print("=" * 70)
print("  VAGUE TICKET TEST — HARD (multi-file feature)")
print("=" * 70)
print(f"  Title: {VAGUE_TITLE}")
print(f"  Body:  {VAGUE_BODY.strip()[:200]}...")
print(f"  Repo:  {repo_id}")
print(f"  Gold:  4 files needed")
print("=" * 70)

# Step 1: Assemble context
print("\n[Layer 3] Assembling context from graph...")
ticket = Ticket(ticket_id=ticket_id, title=VAGUE_TITLE, body=VAGUE_BODY, repo_id=repo_id)
bundle = assemble(ticket)
print(f"  Symbols found: {len(bundle.relevant_symbols)}")
print(f"  Files found:   {len(bundle.relevant_files)}")
print(f"  Token estimate: {bundle.token_estimate}")
if bundle.relevant_symbols:
    print(f"  Top symbols:")
    for s in bundle.relevant_symbols[:5]:
        print(f"    - {s.name} @ {s.file_path}:{s.line_start} (score: {s.score:.3f})")

# Step 2: Run agent
print("\n[Layer 4.5] Running agent (unlimited mode)...")
config = AgentConfig(
    model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
    explore_model="claude-sonnet-4-20250514",
    plan_model="claude-opus-4-6",
    write_model="claude-sonnet-4-20250514",
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    max_iterations=200,
    max_explore_iterations=50,
    max_write_no_test=15,
    max_total_tokens=5_000_000,
    max_repair_iterations=10,
    test_cmd_hint="",
)

start = time.time()
result = run_agent(ticket, bundle, config)
elapsed = time.time() - start

# Step 3: Results
print("\n" + "=" * 70)
print("  RESULTS")
print("=" * 70)
print(f"  Success:    {result.success}")
print(f"  Iterations: {result.iterations}")
print(f"  Time:       {elapsed:.1f}s")
print(f"  Tokens:     {result.total_prompt_tokens:,} prompt + {result.total_completion_tokens:,} completion")
if result.error:
    print(f"  Error:      {result.error}")

impl = result.implementation
if impl and impl.file_results:
    print(f"\n  Files changed ({len(impl.file_results)}):")
    for fr in impl.file_results:
        print(f"    - {fr.change_type}: {fr.file_path}")
        if fr.explanation:
            print(f"      {fr.explanation[:100]}")

    # Check against gold files
    gold_files = [
        "astropy/coordinates/builtin_frames/itrs_observed_transforms.py",
        "astropy/coordinates/builtin_frames/__init__.py",
        "astropy/coordinates/builtin_frames/intermediate_rotation_transforms.py",
        "astropy/coordinates/builtin_frames/itrs.py",
    ]
    changed = [fr.file_path for fr in impl.file_results]
    matched = [f for f in gold_files if f in changed]
    missed = [f for f in gold_files if f not in changed]
    extra = [f for f in changed if f not in gold_files]

    print(f"\n  Gold file comparison:")
    print(f"    Matched: {len(matched)}/4 — {matched}")
    print(f"    Missed:  {missed}")
    print(f"    Extra:   {extra}")
else:
    print("    (no files changed)")

print(f"\n  Diff:")
if impl:
    diff = impl.to_diff_text()
    if diff:
        for line in diff.split("\n")[:50]:
            print(f"    {line}")
    else:
        print("    (no diff)")

prev_best = "1/4 files"
current = f"{len(matched)}/4 files" if impl and impl.file_results else "0/4 files"
print(f"\n  Previous best (detailed ticket): {prev_best}")
print(f"  This run (vague ticket):         {current}")
