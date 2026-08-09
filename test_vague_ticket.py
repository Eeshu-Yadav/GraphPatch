#!/usr/bin/env python3
"""
Test: Can the pipeline solve a VAGUE ticket?

We take django-10097 (URLValidator) but strip ALL specific details:
- No class name "URLValidator"
- No file path "django/core/validators.py"
- No RFC reference
- No regex details
- Just a vague production complaint

The agent must find the bug using graph tools alone.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent

# Setup PYTHONPATH
sys.path.insert(0, str(ROOT / "layer2-indexer" / "src"))
sys.path.insert(0, str(ROOT / "layer3-context"))
sys.path.insert(0, str(ROOT / "layer4-planner"))
sys.path.insert(0, str(ROOT / "layer45-agent"))
sys.path.insert(0, str(ROOT / "layer5-implementer"))
sys.path.insert(0, str(ROOT / "layer6-validator"))
sys.path.insert(0, str(ROOT / "layer7-pr-publisher"))

# Load .env
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
# THE VAGUE TICKET — no class names, no file paths, no RFC numbers
# ═══════════════════════════════════════════════════════════════════════════

VAGUE_TITLE = "Invalid URLs are getting through validation"

VAGUE_BODY = """
We got a report from production that some URLs that should be rejected are
being accepted as valid. For example, a user submitted http://foo/bar@example.com
and it passed validation. URLs with special characters like / and @ in the
username part shouldn't be valid.

This is causing problems because these malformed URLs end up in our database
and break downstream processing.

Can someone look into why our URL validation isn't catching these cases?
"""

# ═══════════════════════════════════════════════════════════════════════════

repo_id = "django/django"
ticket_id = "VAGUE-001"

print("=" * 70)
print("  VAGUE TICKET TEST")
print("=" * 70)
print(f"  Title: {VAGUE_TITLE}")
print(f"  Body:  {VAGUE_BODY.strip()[:200]}...")
print(f"  Repo:  {repo_id}")
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

# Step 2: Run agent with NO limits
print("\n[Layer 4.5] Running agent (unlimited mode)...")
config = AgentConfig(
    model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
    explore_model="claude-sonnet-4-20250514",       # Sonnet for exploration (not Haiku)
    plan_model="claude-opus-4-6",                    # Opus for planning
    write_model="claude-sonnet-4-20250514",          # Sonnet for writing
    api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    max_iterations=200,                              # Effectively unlimited
    max_explore_iterations=50,                       # Deep exploration allowed
    max_write_no_test=15,
    max_total_tokens=5_000_000,
    max_repair_iterations=10,
    test_cmd_hint="",                                # No hint — agent must discover test runner
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

# Show files changed
impl = result.implementation
if impl and impl.file_results:
    print(f"\n  Files changed:")
    for fr in impl.file_results:
        print(f"    - {fr.change_type}: {fr.file_path}")
        if fr.explanation:
            print(f"      {fr.explanation[:100]}")

# Show the diff
print(f"\n  Diff:")
if impl:
    diff = impl.to_diff_text()
    if diff:
        for line in diff.split("\n")[:40]:
            print(f"    {line}")
    else:
        print("    (no diff)")
else:
    print("    (no implementation)")

print(f"\n  Verdict: {'AGENT FOUND AND FIXED THE BUG' if result.success and impl and impl.file_results else 'AGENT DID NOT PRODUCE A FIX'}")
