#!/usr/bin/env python3
"""
Head-to-head: lean v2 vs hybrid agent on django-10914 (FILE_UPLOAD_PERMISSION)
Same vague ticket, same model, compare results.
Gold: 2 files (django/conf/global_settings.py + tests/test_utils/tests.py)
"""
import os, sys, time, json
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

TITLE = "Uploaded files have inconsistent permissions depending on file size"
BODY = """
We discovered that files uploaded through our application get different
permissions depending on their size. Small files (under a few MB) end up
with restrictive permissions that only the owner can read, while larger
files get more permissive settings that anyone can read.

This inconsistency is confusing and potentially a security issue. The
behavior seems to depend on which upload handler processes the file —
one handler creates files with one set of permissions and another handler
creates them with different permissions.

There should be a consistent default permission setting for all uploaded
files regardless of how they're handled internally. The current default
seems to be unset, which causes the OS to decide differently depending
on the upload path.
"""

GOLD_FILES = [
    "django/conf/global_settings.py",
    "tests/test_utils/tests.py",
]

os.environ["AGENT_TRACE"] = "1"

# ═══════════════════════════════════════════════════════════════════════════
# RUN 1: Lean v2
# ═══════════════════════════════════════════════════════════════════════════
print("=" * 70)
print("  RUN 1: LEAN V2 (6 tools + single conversation)")
print("=" * 70)

from lean_agent.agent_v2 import run_lean_agent_v2

start1 = time.time()
result1 = run_lean_agent_v2(
    ticket_title=TITLE, ticket_body=BODY,
    repo_id="django/django",
    repo_path=str(ROOT / "repos" / "django_django"),
    max_turns=30,
)
elapsed1 = time.time() - start1

print(f"\n  Lean v2: {result1['total_turns']} turns, {result1['total_tokens']:,} tokens, {elapsed1:.1f}s")
print(f"  Files changed: {result1['files_changed']}")
matched1 = [f for f in GOLD_FILES if any(f in c or c.endswith(f.split('/')[-1]) for c in result1['files_changed'])]
print(f"  Gold matched: {len(matched1)}/{len(GOLD_FILES)}")

# ═══════════════════════════════════════════════════════════════════════════
# RUN 2: Hybrid (layer45 + lean patterns)
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  RUN 2: HYBRID (layer45 infra + lean v2 patterns)")
print("=" * 70)

from layer3_context.models.ticket import Ticket
from layer3_context.assembly.assembler import assemble
from layer45_agent.models import AgentConfig
from hybrid_agent.agent import run_hybrid_agent

ticket = Ticket(ticket_id="COMPARE-UPLOAD-001", title=TITLE, body=BODY, repo_id="django/django")
bundle = assemble(ticket)
print(f"  Context: {len(bundle.relevant_symbols)} symbols, {len(bundle.relevant_files)} files")

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

# ═══════════════════════════════════════════════════════════════════════════
# COMPARISON
# ═══════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  HEAD-TO-HEAD COMPARISON")
print("=" * 70)
print(f"  {'Metric':<25} {'Lean v2':<20} {'Hybrid':<20}")
print(f"  {'-'*25} {'-'*20} {'-'*20}")
print(f"  {'Total turns':<25} {result1['total_turns']:<20} {result2.iterations:<20}")
print(f"  {'Tokens':<25} {result1['total_tokens']:,<20} {result2.total_prompt_tokens:,<20}")
print(f"  {'Time':<25} {elapsed1:.1f}s{'':<15} {elapsed2:.1f}s")
print(f"  {'Files changed':<25} {len(result1['files_changed']):<20} {len(changed2):<20}")
print(f"  {'Gold matched':<25} {len(matched1)}/{len(GOLD_FILES):<18} {len(matched2)}/{len(GOLD_FILES)}")
print(f"  {'Finished?':<25} {result1.get('success', False):<20} {result2.success:<20}")
