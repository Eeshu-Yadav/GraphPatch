#!/usr/bin/env python3
"""
Batch run: 5 new vague issues through lean v2.
Results collected for Excel + MD update.
"""
import os, sys, time, json
from pathlib import Path
ROOT = Path(__file__).parent.parent
for p in ["layer2-indexer/src", "layer3-context", "layer4-planner", "layer45-agent",
          "layer5-implementer", "layer6-validator", "."]:
    sys.path.insert(0, str(ROOT / p))
for line in open(ROOT / ".env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from lean_agent.agent_v2 import run_lean_agent_v2

ISSUES = [
    {
        "id": "astropy-12907",
        "title": "Matrix calculation gives wrong results for nested combined models",
        "body": """The mathematical function that determines whether model inputs and outputs
are independent is giving incorrect results. When you combine simple models
together and then combine those combinations, the matrix that describes which
outputs depend on which inputs is wrong. Simple combinations work fine, but
nesting them breaks the calculation. The diagonal matrix that should show
independence comes back as all-connected instead.""",
        "repo_id": "astropy/astropy",
        "repo_path": str(ROOT / "repos" / "astropy_astropy"),
        "gold": ["astropy/modeling/separable.py"],
    },
    {
        "id": "astropy-13033",
        "title": "Error message is confusing when a required column is missing from data",
        "body": """When creating a time series object and a required column is missing entirely,
the error message says something like 'expected time as first column but found flux'
which makes it sound like the columns are in the wrong ORDER. But the real problem
is that the column is completely MISSING. The error should distinguish between
'column exists but is in wrong position' vs 'column does not exist at all'.""",
        "repo_id": "astropy/astropy",
        "repo_path": str(ROOT / "repos" / "astropy_astropy"),
        "gold": ["astropy/timeseries/core.py"],
    },
    {
        "id": "astropy-13236",
        "title": "Adding structured array data to a table silently converts it to wrong type",
        "body": """When you add a numpy array with named fields (structured array) to a data table,
it gets silently converted to a different internal type instead of staying as a
regular column. This produces a deprecation warning and the data behaves differently
than expected. The conversion was a historical workaround that shouldn't happen anymore.""",
        "repo_id": "astropy/astropy",
        "repo_path": str(ROOT / "repos" / "astropy_astropy"),
        "gold": ["astropy/table/table.py"],
    },
    {
        "id": "django-10097-v2",
        "title": "Website accepts URLs that should be blocked",
        "body": """Our form validation is letting through URLs that have weird characters in them.
Someone submitted a URL with slashes and @ symbols in the wrong place and it
passed validation. These URLs cause problems downstream. The validation regex
seems too loose — it allows any non-space character where it should be more
restrictive about what's allowed before the @ sign.""",
        "repo_id": "django/django",
        "repo_path": str(ROOT / "repos" / "django_django"),
        "gold": ["django/core/validators.py"],
    },
    {
        "id": "django-10880-v2",
        "title": "Counting with filter produces broken SQL",
        "body": """We have a report that counting records with both a condition and uniqueness
flag generates malformed SQL. The database rejects the query with a syntax error.
It looks like two SQL keywords are running together without a space separator.
Works fine if you remove either the condition or the uniqueness — only breaks
when both are combined.""",
        "repo_id": "django/django",
        "repo_path": str(ROOT / "repos" / "django_django"),
        "gold": ["django/db/models/aggregates.py"],
    },
]

all_results = []

for i, issue in enumerate(ISSUES):
    print(f"\n{'='*70}")
    print(f"  ISSUE {i+1}/{len(ISSUES)}: {issue['id']}")
    print(f"  Title: {issue['title']}")
    print(f"  Gold: {issue['gold']}")
    print(f"{'='*70}")

    start = time.time()
    try:
        result = run_lean_agent_v2(
            ticket_title=issue["title"],
            ticket_body=issue["body"],
            repo_id=issue["repo_id"],
            repo_path=issue["repo_path"],
            max_turns=40,
        )
        elapsed = time.time() - start

        changed = result.get("files_changed", [])
        matched = [f for f in issue["gold"]
                   if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]

        summary = {
            "id": issue["id"],
            "title": issue["title"],
            "gold": issue["gold"],
            "explore_turns": result.get("explore_turns", 0),
            "write_turns": result.get("write_turns", 0),
            "total_turns": result.get("total_turns", 0),
            "tokens": result.get("total_tokens", 0),
            "time": round(elapsed, 1),
            "files_to_modify": result.get("files_to_modify", []),
            "files_changed": changed,
            "gold_matched": len(matched),
            "gold_total": len(issue["gold"]),
            "finished": result.get("success", False),
            "explore_summary": result.get("explore_summary", "")[:200],
        }
        all_results.append(summary)

        print(f"\n  Result: {summary['gold_matched']}/{summary['gold_total']} gold, "
              f"{summary['total_turns']} turns, {summary['tokens']:,} tokens, {elapsed:.0f}s")
        print(f"  Files changed: {changed}")
        print(f"  Finished: {summary['finished']}")

    except Exception as e:
        elapsed = time.time() - start
        print(f"\n  ERROR: {str(e)[:200]}")
        all_results.append({
            "id": issue["id"], "title": issue["title"], "gold": issue["gold"],
            "error": str(e)[:200], "time": round(elapsed, 1),
            "explore_turns": 0, "write_turns": 0, "total_turns": 0,
            "tokens": 0, "files_changed": [], "gold_matched": 0,
            "gold_total": len(issue["gold"]), "finished": False,
        })

# Save results
output_path = ROOT / "batch_results.json"
with open(output_path, "w") as f:
    json.dump(all_results, f, indent=2)
print(f"\n\nResults saved to {output_path}")

# Print summary table
print(f"\n{'='*70}")
print(f"  BATCH SUMMARY")
print(f"{'='*70}")
print(f"  {'Issue':<25} {'Gold':<8} {'Turns':<8} {'Tokens':<10} {'Time':<8} {'Finished'}")
print(f"  {'-'*75}")
for r in all_results:
    print(f"  {r['id']:<25} {r['gold_matched']}/{r['gold_total']:<6} "
          f"{r['total_turns']:<8} {r.get('tokens',0):>9,} {r['time']:>6.0f}s  {r['finished']}")
