#!/usr/bin/env python3
"""
Test: Lean agent on django-10973 with VERY VAGUE ticket.
The actual issue: modernize PostgreSQL dbshell to use subprocess.run + PGPASSWORD env var.
Gold solution: 2 files (client.py + test_postgresql.py)
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
# VERY VAGUE TICKET — no file paths, no function names, no specific APIs
# ═══════════════════════════════════════════════════════════════════════════

TITLE = "Database shell command is handling passwords in an insecure and overly complex way"

BODY = """
When developers use the database shell management command to connect to
PostgreSQL, the way passwords are passed to the psql process is problematic.

Currently it's using some kind of temporary file or signal handling hack
to pass the password, which is both insecure (password might end up on disk)
and unnecessarily complex. There should be a simpler way to pass credentials
to the subprocess using environment variables.

Also, the code is using older Python subprocess APIs when newer, cleaner
alternatives are available that support passing custom environment variables
directly.

Can someone clean this up? The current approach feels like a workaround
from when Python didn't have better subprocess handling.
"""

repo_id = "django/django"
repo_path = str(ROOT / "repos" / "django_django")

print("=" * 70)
print("  LEAN AGENT — VAGUE Django Ticket (dbshell/postgres)")
print("=" * 70)
print(f"  Title: {TITLE}")
print(f"  Repo: {repo_id}")
print(f"  Gold: 2 files (client.py + test_postgresql.py)")
print("=" * 70)

start = time.time()
result = run_lean_agent(
    ticket_title=TITLE,
    ticket_body=BODY,
    repo_id=repo_id,
    repo_path=repo_path,
    max_explore_turns=20,
    max_write_turns=50,
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

# Gold comparison
gold_files = [
    "django/db/backends/postgresql/client.py",
    "tests/dbshell/test_postgresql.py",
]
changed = result["files_changed"]
matched = [f for f in gold_files if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]
missed = [f for f in gold_files if f not in matched]
print(f"\n  Gold files matched: {len(matched)}/2")
print(f"    Matched: {matched}")
print(f"    Missed:  {missed}")

print(f"\n  Explore tools: {result['explore_tools']}")
print(f"  Write tools:   {result['write_tools']}")
