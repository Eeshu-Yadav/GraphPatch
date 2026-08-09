#!/usr/bin/env python3
"""
Test adaptive agent on known issues to verify classifier + config routing.

Expected behaviour:
  django-10880  (1-line SQL fix)           → EASY,   ~25-35 turns
  django-10914  (upload permissions)       → MEDIUM, ~30-45 turns
  astropy-13398 (ITRS transforms, 4 files) → HARD,   ~45-70 turns
"""
import os, sys, time, json
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

from lean_agent.agent_adaptive import run_adaptive_agent

ISSUES = [
    {
        "id": "django-10880",
        "title": "Counting with filter produces broken SQL",
        "body": (
            "We have a report that counting records with both a condition and uniqueness "
            "flag generates malformed SQL. The database rejects the query with a syntax error. "
            "It looks like two SQL keywords are running together without a space separator. "
            "Works fine if you remove either the condition or the uniqueness — only breaks "
            "when both are combined."
        ),
        "repo_id": "django/django",
        "repo_path": str(ROOT / "repos" / "django_django"),
        "gold": ["django/db/models/aggregates.py"],
        "expected_tier": "easy",
    },
    {
        "id": "django-10914",
        "title": "Uploaded files have inconsistent permissions depending on file size",
        "body": (
            "When uploading files, the resulting file permission depends on whether the file "
            "was small enough to be held in memory or large enough to be written to a temp file. "
            "Small files get one set of permissions, large files get another. "
            "There should be a consistent default permission that applies to all uploaded files."
        ),
        "repo_id": "django/django",
        "repo_path": str(ROOT / "repos" / "django_django"),
        "gold": ["django/conf/global_settings.py", "tests/test_utils/tests.py"],
        "expected_tier": "medium",
    },
    {
        "id": "astropy-13398",
        "title": "Satellite position calculations are inaccurate when converting between Earth-fixed and observer coordinates",
        "body": (
            "When converting satellite positions from Earth-fixed (ITRS) coordinates to "
            "observer-relative coordinates (AltAz or HADec), the results are inaccurate. "
            "The current implementation routes through an intermediate celestial frame "
            "which introduces errors for objects close to Earth. A direct ITRS-to-observed "
            "transform is needed that properly accounts for the observer position."
        ),
        "repo_id": "astropy/astropy",
        "repo_path": str(ROOT / "repos" / "astropy_astropy"),
        "gold": [
            "astropy/coordinates/builtin_frames/itrs_observed_transforms.py",
            "astropy/coordinates/builtin_frames/__init__.py",
            "astropy/coordinates/builtin_frames/intermediate_rotation_transforms.py",
            "astropy/coordinates/builtin_frames/itrs.py",
        ],
        "expected_tier": "hard",
    },
]

results = []

for issue in ISSUES:
    print(f"\n{'='*65}")
    print(f"  {issue['id']}  (expected tier: {issue['expected_tier']})")
    print(f"{'='*65}")

    result = run_adaptive_agent(
        ticket_title=issue["title"],
        ticket_body=issue["body"],
        repo_id=issue["repo_id"],
        repo_path=issue["repo_path"],
    )

    changed = result.get("files_changed", [])
    matched = [f for f in issue["gold"]
               if any(f in c or c.endswith(f.split("/")[-1]) for c in changed)]

    tier_ok = "✅" if result["tier_classified"] == issue["expected_tier"] else "❌"
    upgraded = f" → {result['tier_final']}" if result.get("tier_upgraded_from") else ""

    print(f"\n  Tier classified: {result['tier_classified']} {tier_ok}{upgraded}")
    print(f"  Total turns:     {result['total_turns']}")
    print(f"  Tokens:          {result['total_tokens']:,}")
    print(f"  Cache read:      {result['total_cache_read']:,}")
    print(f"  Cache create:    {result['total_cache_create']:,}")
    print(f"  Files changed:   {changed}")
    print(f"  Gold matched:    {len(matched)}/{len(issue['gold'])}")
    print(f"  Success:         {result['success']}")
    print(f"  Time:            {result['elapsed_s']}s")
    print(f"  Graph tools:     {[t['tool'] for t in result.get('graph_tools_used', [])]}")
    print(f"  Signals:         {result['classifier_signals']}")

    results.append({
        "id": issue["id"],
        "expected_tier": issue["expected_tier"],
        "tier_classified": result["tier_classified"],
        "tier_final": result["tier_final"],
        "tier_ok": result["tier_classified"] == issue["expected_tier"],
        "upgraded": result.get("tier_upgraded_from"),
        "total_turns": result["total_turns"],
        "tokens": result["total_tokens"],
        "cache_read": result["total_cache_read"],
        "gold_matched": f"{len(matched)}/{len(issue['gold'])}",
        "success": result["success"],
        "elapsed_s": result["elapsed_s"],
    })

# Summary
print(f"\n\n{'='*65}")
print("  ADAPTIVE AGENT — SUMMARY")
print(f"{'='*65}")
print(f"  {'Issue':<20} {'Tier':<8} {'OK':<4} {'Turns':<7} {'Tokens':<10} {'Gold':<6} {'Done'}")
print(f"  {'-'*65}")
for r in results:
    tier_str = r["tier_classified"]
    if r.get("upgraded"):
        tier_str += f"→{r['tier_final']}"
    print(f"  {r['id']:<20} {tier_str:<8} {'✅' if r['tier_ok'] else '❌':<4} "
          f"{r['total_turns']:<7} {r['tokens']:>9,} {r['gold_matched']:<6} {r['success']}")

traces_dir = ROOT / "lean_agent" / "traces_adaptive"
traces_dir.mkdir(exist_ok=True)
ts = time.strftime("%Y%m%d_%H%M%S")
output = traces_dir / f"{ts}_adaptive_batch.json"
with open(output, "w") as f:
    json.dump(results, f, indent=2)
print(f"\n  Saved to {output}")
