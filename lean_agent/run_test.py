#!/usr/bin/env python3
"""
Test: Lean agent on astropy-13398 (the 97-turn exploration issue)
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
sys.path.insert(0, str(ROOT))  # so "lean_agent" resolves from root

env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from lean_agent.agent import run_lean_agent

TITLE = "Satellite position calculations are inaccurate when converting between Earth-fixed and observer coordinates"

BODY = """
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

repo_id = "astropy/astropy"
repo_path = str(ROOT / "repos" / "astropy_astropy")

print("=" * 70)
print("  LEAN AGENT TEST — astropy-13398 (97-turn issue)")
print("=" * 70)
print(f"  Ticket: {TITLE[:80]}...")
print(f"  Repo: {repo_id}")
print(f"  Previous: 97 turns to start writing (layer45-agent)")
print("=" * 70)

start = time.time()
result = run_lean_agent(
    ticket_title=TITLE,
    ticket_body=BODY,
    repo_id=repo_id,
    repo_path=repo_path,
    max_explore_turns=20,
    max_write_turns=100,
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
print(f"  Cache read:      {result.get('total_cache_read', 0):,} tokens (saved from re-reading system prompt)")
print(f"  Time:            {elapsed:.1f}s")
print(f"  Graph context:   {result['graph_context_chars']} chars")
print(f"  Files to modify: {result['files_to_modify']}")
print(f"  Files changed:   {result['files_changed']}")
print(f"  Explore tools:   {result['explore_tools']}")
print(f"  Write tools:     {result['write_tools']}")

# Compare with gold
gold_files = [
    "astropy/coordinates/builtin_frames/itrs_observed_transforms.py",
    "astropy/coordinates/builtin_frames/__init__.py",
    "astropy/coordinates/builtin_frames/intermediate_rotation_transforms.py",
    "astropy/coordinates/builtin_frames/itrs.py",
]
identified = result["files_to_modify"]
matched = [f for f in gold_files if any(f in m or m in f for m in identified)]
print(f"\n  Gold files matched: {len(matched)}/4")
print(f"    Matched: {matched}")
print(f"    Missed:  {[f for f in gold_files if f not in matched]}")

print(f"\n  COMPARISON:")
print(f"    layer45-agent: 97 turns to start writing, 126 total, killed")
print(f"    lean-agent:    {result['explore_turns']} explore + {result['write_turns']} write = {result['total_turns']} total")
