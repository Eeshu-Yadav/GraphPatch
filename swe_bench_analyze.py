#!/usr/bin/env python3
"""
Analyze SWE-bench results to find pipeline improvement opportunities.

Usage:
    python swe_bench_analyze.py --predictions predictions.jsonl
    python swe_bench_analyze.py --predictions predictions.jsonl --eval-results eval_dir/
"""
import os
import sys
import json
import argparse
import difflib
from pathlib import Path

ROOT = Path(__file__).parent
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
if sys.executable != VENV_PYTHON and Path(VENV_PYTHON).exists():
    os.execv(VENV_PYTHON, [VENV_PYTHON] + sys.argv)

TRACES_DIR = ROOT / "traces"


def load_predictions(path: str) -> list[dict]:
    preds = []
    with open(path) as f:
        for line in f:
            preds.append(json.loads(line))
    return preds


def load_dataset_instances(instance_ids: list[str]) -> dict[str, dict]:
    from datasets import load_dataset
    ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
    lookup = {}
    for inst in ds:
        if inst["instance_id"] in instance_ids:
            lookup[inst["instance_id"]] = inst
    return lookup


def compare_patches(agent_patch: str, gold_patch: str) -> dict:
    """Compare agent's patch vs gold patch."""
    if not agent_patch.strip():
        return {"status": "empty", "detail": "Agent produced no patch"}

    agent_files = set()
    gold_files = set()

    for line in agent_patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            agent_files.add(line.split("/", 1)[-1] if "/" in line else line)
    for line in gold_patch.splitlines():
        if line.startswith("+++ b/") or line.startswith("--- a/"):
            gold_files.add(line.split("/", 1)[-1] if "/" in line else line)

    # Normalize file paths (remove a/ and b/ prefixes)
    agent_files = {f.strip() for f in agent_files if f.strip() and f.strip() != "/dev/null"}
    gold_files = {f.strip() for f in gold_files if f.strip() and f.strip() != "/dev/null"}

    correct_files = agent_files & gold_files
    wrong_files = agent_files - gold_files
    missed_files = gold_files - agent_files

    # Similarity score
    agent_lines = [l for l in agent_patch.splitlines() if l.startswith("+") and not l.startswith("+++")]
    gold_lines = [l for l in gold_patch.splitlines() if l.startswith("+") and not l.startswith("+++")]

    if agent_lines and gold_lines:
        similarity = difflib.SequenceMatcher(None, agent_lines, gold_lines).ratio()
    else:
        similarity = 0.0

    return {
        "status": "generated",
        "correct_files": sorted(correct_files),
        "wrong_files": sorted(wrong_files),
        "missed_files": sorted(missed_files),
        "agent_files_count": len(agent_files),
        "gold_files_count": len(gold_files),
        "file_accuracy": len(correct_files) / max(len(gold_files), 1),
        "line_similarity": round(similarity, 3),
        "agent_additions": len(agent_lines),
        "gold_additions": len(gold_lines),
    }


def find_trace(instance_id: str) -> dict | None:
    """Find the trace file for an instance."""
    for trace_file in sorted(TRACES_DIR.glob("*.json"), reverse=True):
        try:
            with open(trace_file) as f:
                data = json.load(f)
            if data.get("ticket_id") == instance_id:
                return data
        except Exception:
            continue
    return None


def analyze_trace(trace: dict) -> dict:
    """Extract key metrics from a trace."""
    summary = trace.get("summary", {})
    turns = [t for t in trace.get("turns", []) if "iteration" in t]
    compressions = [t for t in trace.get("turns", []) if t.get("event") == "compression"]

    tools_used = {}
    for turn in turns:
        for tc in turn.get("tool_calls", []):
            name = tc["tool"]
            tools_used[name] = tools_used.get(name, 0) + 1

    # Check which graph tools were used
    graph_tools = {"search_symbols", "get_dependencies", "get_impact", "get_callers",
                   "get_coupled_files", "get_risk_score", "get_test_coverage"}
    graph_used = {t for t in tools_used if t in graph_tools}
    graph_not_used = graph_tools - graph_used

    # Check for write failures
    write_failures = 0
    for turn in turns:
        for tr in turn.get("tool_results", []):
            if "write_file" in tr.get("tool", "") and "success': False" in tr.get("result_preview", ""):
                write_failures += 1

    # Models used
    models = {}
    for turn in turns:
        m = turn.get("model", "unknown")
        models[m] = models.get(m, 0) + 1

    return {
        "iterations": summary.get("iterations", len(turns)),
        "total_cost": summary.get("total_cost_usd", 0),
        "prompt_tokens": summary.get("total_prompt_tokens", 0),
        "completion_tokens": summary.get("total_completion_tokens", 0),
        "compressions": len(compressions),
        "tools_used": tools_used,
        "graph_tools_used": sorted(graph_used),
        "graph_tools_not_used": sorted(graph_not_used),
        "write_failures": write_failures,
        "models": models,
    }


def classify_failure(comparison: dict, trace_analysis: dict | None) -> str:
    """Classify the type of failure for actionable feedback."""
    if comparison["status"] == "empty":
        if trace_analysis and trace_analysis.get("iterations", 0) <= 2:
            return "CONTEXT_FAILURE — Agent couldn't find relevant code (Layer 3 issue)"
        if trace_analysis and trace_analysis.get("write_failures", 0) > 0:
            return "WRITE_FAILURE — Agent found code but edits failed (search string mismatch)"
        return "NO_PATCH — Agent explored but produced no changes"

    if comparison["file_accuracy"] == 0:
        return "WRONG_FILES — Agent modified wrong files entirely"
    if comparison["file_accuracy"] < 1.0:
        return f"PARTIAL_FILES — Agent got {len(comparison['correct_files'])}/{comparison['gold_files_count']} files right, missed: {comparison['missed_files']}"
    if comparison["line_similarity"] < 0.2:
        return "WRONG_LOGIC — Right files but very different changes"
    if comparison["line_similarity"] < 0.5:
        return "PARTIAL_FIX — Right direction but incomplete/different approach"
    return "CLOSE — Similar to gold patch, may pass tests"


def main():
    parser = argparse.ArgumentParser(description="Analyze SWE-bench results")
    parser.add_argument("--predictions", required=True, help="Path to predictions.jsonl")
    args = parser.parse_args()

    predictions = load_predictions(args.predictions)
    instance_ids = [p["instance_id"] for p in predictions]

    print("Loading SWE-bench gold patches...")
    instances = load_dataset_instances(instance_ids)

    print(f"\n{'='*80}")
    print(f"SWE-bench Analysis — {len(predictions)} instances")
    print(f"{'='*80}")

    results = []
    improvement_hints = []

    for pred in predictions:
        iid = pred["instance_id"]
        gold = instances.get(iid, {})
        gold_patch = gold.get("patch", "")

        print(f"\n--- {iid} ---")

        # Compare patches
        comparison = compare_patches(pred.get("model_patch", ""), gold_patch)

        # Find and analyze trace
        trace = find_trace(iid)
        trace_analysis = analyze_trace(trace) if trace else None

        # Classify failure
        classification = classify_failure(comparison, trace_analysis)

        print(f"  Status:         {comparison['status']}")
        print(f"  Classification: {classification}")

        if comparison["status"] == "generated":
            print(f"  File accuracy:  {comparison['file_accuracy']:.0%} ({comparison['agent_files_count']} agent / {comparison['gold_files_count']} gold)")
            if comparison["correct_files"]:
                print(f"  Correct files:  {comparison['correct_files']}")
            if comparison["wrong_files"]:
                print(f"  Wrong files:    {comparison['wrong_files']}")
            if comparison["missed_files"]:
                print(f"  Missed files:   {comparison['missed_files']}")
            print(f"  Line similarity: {comparison['line_similarity']:.1%}")

        if trace_analysis:
            print(f"  Iterations:     {trace_analysis['iterations']}")
            print(f"  Cost:           ${trace_analysis['total_cost']:.2f}")
            print(f"  Compressions:   {trace_analysis['compressions']}")
            print(f"  Graph tools:    {trace_analysis['graph_tools_used'] or 'NONE'}")
            if trace_analysis["graph_tools_not_used"]:
                print(f"  Graph unused:   {trace_analysis['graph_tools_not_used']}")
            print(f"  Write failures: {trace_analysis['write_failures']}")

        # Gold patch info
        gold_files = [l.split("/", 1)[-1] for l in gold_patch.splitlines() if l.startswith("+++ b/")]
        gold_additions = len([l for l in gold_patch.splitlines() if l.startswith("+") and not l.startswith("+++")])
        print(f"  Gold patch:     {len(gold_files)} files, {gold_additions} additions")

        # Generate improvement hints
        if "CONTEXT_FAILURE" in classification:
            improvement_hints.append(f"{iid}: Layer 3 didn't find relevant symbols. Check if semantic search returns useful results for this issue text.")
        elif "WRONG_FILES" in classification:
            improvement_hints.append(f"{iid}: Agent modified wrong files. Gold patch targets: {gold_files}. Improve file discovery in context assembly.")
        elif "WRITE_FAILURE" in classification:
            improvement_hints.append(f"{iid}: Agent found the right area but write_file failed. Improve search string matching or read more context before writing.")
        elif "NO_PATCH" in classification:
            improvement_hints.append(f"{iid}: Agent explored but gave up. Check trace for what blocked it.")
        elif "PARTIAL" in classification:
            improvement_hints.append(f"{iid}: Agent got part of the fix. Missing: {comparison.get('missed_files', [])}. Agent may need get_impact to find all affected locations.")

        results.append({
            "instance_id": iid,
            "classification": classification,
            "comparison": comparison,
            "trace": trace_analysis,
        })

    # Summary
    classifications = [r["classification"].split(" — ")[0] for r in results]
    print(f"\n{'='*80}")
    print("SUMMARY")
    print(f"{'='*80}")
    print(f"Total instances:    {len(results)}")
    print(f"Patches generated:  {sum(1 for r in results if r['comparison']['status'] == 'generated')}/{len(results)}")
    print(f"Close to gold:      {sum(1 for c in classifications if c == 'CLOSE')}/{len(results)}")

    print(f"\nFailure distribution:")
    from collections import Counter
    for cls, count in Counter(classifications).most_common():
        print(f"  {cls:20s} {count}")

    if improvement_hints:
        print(f"\n{'='*80}")
        print("IMPROVEMENT HINTS")
        print(f"{'='*80}")
        for hint in improvement_hints:
            print(f"  • {hint}")

    # Save full analysis
    analysis_path = Path(args.predictions).parent / f"analysis_{Path(args.predictions).stem.split('_', 1)[1]}.json"
    with open(analysis_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull analysis saved: {analysis_path}")


if __name__ == "__main__":
    main()
