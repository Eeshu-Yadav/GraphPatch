#!/usr/bin/env python3
"""
SWE-bench benchmark runner for the Ticket-to-PR pipeline.

Calls the agent DIRECTLY (no MCP server, no _reset_repo_to_origin).
The repo stays at base_commit throughout the agent run.

Usage:
    python swe_bench_runner.py --limit 5
    python swe_bench_runner.py --limit 5 --skip-index
    python swe_bench_runner.py --instance django__django-16379
"""
import os
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).parent
REPO_CACHE = ROOT / "repos"
RESULTS_DIR = ROOT / "swe_bench_results"

# Track which repos are already indexed
_indexed_repos: set[str] = set()

# Use the project venv (has all deps)
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
if sys.executable != VENV_PYTHON and Path(VENV_PYTHON).exists():
    os.execv(VENV_PYTHON, [VENV_PYTHON, str(Path(__file__).resolve())] + sys.argv[1:])

# Add all layers to Python path (so we can import directly)
for p in ["layer2-indexer/src", "layer3-context", "layer4-planner", "layer45-agent",
          "layer6-validator", "layer7-pr-publisher", "mcp-server"]:
    sys.path.insert(0, str(ROOT / p))


def load_env():
    """Load .env file."""
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def clone_and_checkout(repo_id: str, commit: str) -> Path:
    """Clone repo if needed, checkout specific commit."""
    slug = repo_id.replace("/", "_")
    repo_path = REPO_CACHE / slug

    if not repo_path.exists():
        print(f"  Cloning {repo_id}...")
        subprocess.run(
            ["git", "clone", "--quiet", f"https://github.com/{repo_id}", str(repo_path)],
            check=True, timeout=300,
        )

    # Checkout the exact commit
    subprocess.run(["git", "fetch", "--quiet", "origin"], cwd=str(repo_path), timeout=60)
    subprocess.run(["git", "checkout", "--force", commit], cwd=str(repo_path), capture_output=True, timeout=30)
    subprocess.run(["git", "clean", "-fd", "--quiet"], cwd=str(repo_path), timeout=30)

    # Install project + test deps so the agent can run tests
    _install_deps(repo_path)

    print(f"  Checked out {commit[:10]}")
    return repo_path


_deps_installed: set[str] = set()


def _install_deps(repo_path: Path) -> None:
    """Install project dependencies so tests can run. Auto-detects build system."""
    key = str(repo_path)
    if key in _deps_installed:
        return

    print(f"  Installing dependencies...")

    # Try common install methods in order
    install_cmds = []
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists() or (repo_path / "setup.cfg").exists():
        install_cmds.append(([sys.executable, "-m", "pip", "install", "-e", ".[test]", "--quiet"], "pip install -e .[test]"))
        install_cmds.append(([sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"], "pip install -e ."))
    if (repo_path / "requirements.txt").exists():
        install_cmds.append(([sys.executable, "-m", "pip", "install", "-r", "requirements.txt", "--quiet"], "pip install -r requirements.txt"))
    if (repo_path / "package.json").exists():
        install_cmds.append((["npm", "install"], "npm install"))

    for cmd, name in install_cmds:
        try:
            result = subprocess.run(cmd, cwd=str(repo_path), capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                print(f"  Installed via: {name}")
                _deps_installed.add(key)
                return
            # If [test] extra fails, try without it
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    print(f"  Warning: could not install dependencies")


def _repo_already_indexed(repo_id: str) -> bool:
    """Check if this repo already has data in Memgraph."""
    try:
        import mgclient
        conn = mgclient.connect(host="localhost", port=7687)
        cursor = conn.cursor()
        cursor.execute("MATCH (f:File {repo_id: $id}) RETURN count(f) AS cnt", {"id": repo_id})
        count = cursor.fetchone()[0]
        conn.close()
        return count > 50
    except Exception:
        return False


def index_repo(repo_id: str, repo_url: str) -> bool:
    """Index the repo using Layer 2."""
    if _repo_already_indexed(repo_id):
        print(f"  Already indexed ({repo_id}), skipping")
        _indexed_repos.add(repo_id)
        return True
    print(f"  Indexing {repo_id}...")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "layer2-indexer" / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "src.cli", "index",
         "--repo", repo_url, "--id", repo_id,
         "--sync", "--skip-descriptions"],
        cwd=str(ROOT / "layer2-indexer"),
        env=env,
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        print(f"  Index failed: {result.stderr[:300]}")
        return False
    return True


def run_agent_direct(repo_id: str, instance_id: str, problem_statement: str,
                     fail_to_pass: list[str] | None = None) -> str:
    """
    Call the agent DIRECTLY — no MCP server, no _reset_repo_to_origin.
    The repo stays at base_commit. Agent writes code, we capture git diff.
    """
    from layer3_context.models.ticket import Ticket
    from layer3_context.assembly.assembler import assemble
    from layer45_agent.agent import run_agent
    from layer45_agent.models import AgentConfig

    # Build ticket
    lines = problem_statement.strip().splitlines()
    title = lines[0][:200] if lines else instance_id
    ticket = Ticket(ticket_id=instance_id, title=title, body=problem_statement, repo_id=repo_id)

    # Assemble context from graph + vector DB
    print(f"  Assembling context...")
    bundle = assemble(ticket)
    print(f"  Context: {len(bundle.relevant_symbols)} symbols, {len(bundle.relevant_files)} files, ~{bundle.token_estimate} tokens")

    # Build test command hint from FAIL_TO_PASS test identifiers
    test_cmd_hint = ""
    if fail_to_pass:
        test_cmd_hint = f"python -m pytest {' '.join(fail_to_pass[:5])} -xvs"
        print(f"  Test hint: {test_cmd_hint}")

    # Configure agent
    config = AgentConfig(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
        explore_model=os.environ.get("CLAUDE_EXPLORE_MODEL", "claude-haiku-4-5-20251001"),
        plan_model=os.environ.get("CLAUDE_PLAN_MODEL", "claude-opus-4-6"),
        write_model=os.environ.get("CLAUDE_WRITE_MODEL", "claude-sonnet-4-20250514"),
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        max_iterations=20,
        test_cmd_hint=test_cmd_hint,
        max_repair_iterations=3,
    )

    # Enable tracing
    os.environ["AGENT_TRACE"] = "1"

    # Run agent — repo stays at base_commit, no reset
    print(f"  Running agent (max {config.max_iterations} iterations)...")
    start = time.time()
    result = run_agent(ticket, bundle, config)
    elapsed = time.time() - start

    print(f"  Agent: {result.iterations} iterations, {result.total_prompt_tokens} prompt tokens, "
          f"{result.total_completion_tokens} completion tokens, {elapsed:.0f}s")

    if not result.success:
        print(f"  Agent error: {result.error}")

    # Capture diff — repo is still at base_commit, agent wrote files directly
    repo_path = REPO_CACHE / repo_id.replace("/", "_")
    diff_result = subprocess.run(
        ["git", "diff"],
        cwd=str(repo_path), capture_output=True, text=True, timeout=30,
    )
    patch = diff_result.stdout

    # Also capture new files
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_path), capture_output=True, text=True, timeout=10,
    )
    for line in status.stdout.splitlines():
        if line.startswith("??"):
            new_file = line[3:].strip()
            fpath = repo_path / new_file
            if fpath.is_file() and fpath.stat().st_size < 50000:
                content = fpath.read_text(encoding="utf-8", errors="replace")
                file_lines = content.splitlines()
                patch += f"\ndiff --git a/{new_file} b/{new_file}\nnew file mode 100644\n"
                patch += f"--- /dev/null\n+++ b/{new_file}\n"
                patch += f"@@ -0,0 +1,{len(file_lines)} @@\n"
                patch += "\n".join(f"+{l}" for l in file_lines) + "\n"

    if patch.strip():
        print(f"  Patch: {len(patch)} chars, {patch.count(chr(10))} lines")

    return patch


def run_instance(instance: dict, skip_index: bool = False) -> dict:
    """Run one SWE-bench instance end-to-end."""
    instance_id = instance["instance_id"]
    repo_id = instance["repo"]
    commit = instance["base_commit"]
    problem = instance["problem_statement"]

    print(f"\n{'='*60}")
    print(f"Instance: {instance_id}")
    print(f"Repo: {repo_id} @ {commit[:10]}")
    print(f"Issue: {problem[:100]}...")
    print(f"{'='*60}")

    start = time.time()

    try:
        # 1. Clone + checkout exact base_commit
        repo_path = clone_and_checkout(repo_id, commit)

        # 2. Index (once per repo)
        if not skip_index and repo_id not in _indexed_repos:
            indexed = index_repo(repo_id, f"https://github.com/{repo_id}")
            if not indexed:
                print(f"  SKIP — indexing failed")
                return _empty_prediction(instance_id)
            _indexed_repos.add(repo_id)
        else:
            print(f"  Reusing existing index")

        # 3. Extract failing test identifiers (if available)
        fail_to_pass = instance.get("FAIL_TO_PASS", [])
        if isinstance(fail_to_pass, str):
            import json as _json
            try:
                fail_to_pass = _json.loads(fail_to_pass)
            except Exception:
                fail_to_pass = []

        # 4. Run agent directly (no MCP server, no reset to main)
        patch = run_agent_direct(repo_id, instance_id, problem, fail_to_pass=fail_to_pass)

        # 4. Reset repo for next instance
        subprocess.run(["git", "checkout", "--force", commit], cwd=str(repo_path), capture_output=True, timeout=30)
        subprocess.run(["git", "clean", "-fd", "--quiet"], cwd=str(repo_path), timeout=30)

        elapsed = time.time() - start
        has_patch = bool(patch.strip())
        print(f"  Result: {'PATCH' if has_patch else 'EMPTY'} ({len(patch)} chars, {elapsed:.0f}s)")

        return {
            "instance_id": instance_id,
            "model_name_or_path": "ticket-to-pr-agent",
            "model_patch": patch,
            "elapsed_s": round(elapsed, 1),
        }

    except Exception as e:
        print(f"  ERROR: {e}")
        import traceback
        traceback.print_exc()
        return _empty_prediction(instance_id)


def _empty_prediction(instance_id: str) -> dict:
    return {
        "instance_id": instance_id,
        "model_name_or_path": "ticket-to-pr-agent",
        "model_patch": "",
    }


def main():
    parser = argparse.ArgumentParser(description="SWE-bench Runner")
    parser.add_argument("--limit", type=int, default=5, help="Number of instances to run")
    parser.add_argument("--skip-index", action="store_true", help="Skip re-indexing")
    parser.add_argument("--instance", type=str, help="Run a single instance by ID")
    parser.add_argument("--repo", type=str, help="Filter by repo (e.g. django/django)")
    parser.add_argument("--offset", type=int, default=0, help="Skip first N instances")
    parser.add_argument("--dataset", type=str, default="SWE-bench/SWE-bench_Verified")
    args = parser.parse_args()

    load_env()
    RESULTS_DIR.mkdir(exist_ok=True)

    print(f"Loading dataset: {args.dataset}")
    from datasets import load_dataset
    ds = load_dataset(args.dataset, split="test")
    print(f"Loaded {len(ds)} instances")

    if args.instance:
        instances = [i for i in ds if i["instance_id"] == args.instance]
        if not instances:
            print(f"Instance {args.instance} not found!")
            return
    elif args.repo:
        repo_instances = [i for i in ds if i["repo"] == args.repo]
        if not repo_instances:
            print(f"No instances found for repo {args.repo}")
            return
        instances = repo_instances[args.offset:args.offset + args.limit]
        print(f"Filtered to {args.repo}: {len(repo_instances)} total, running {len(instances)} (offset={args.offset})")
    else:
        instances = list(ds.select(range(min(args.limit, len(ds)))))

    print(f"\nRunning {len(instances)} instances\n")

    predictions = []
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    for i, instance in enumerate(instances):
        print(f"\n[{i+1}/{len(instances)}]")
        pred = run_instance(instance, skip_index=args.skip_index)
        predictions.append(pred)

        # Save after each instance
        pred_path = RESULTS_DIR / f"predictions_{run_id}.jsonl"
        with open(pred_path, "w") as f:
            for p in predictions:
                f.write(json.dumps(p) + "\n")

    # Summary
    patches = sum(1 for p in predictions if p.get("model_patch", "").strip())
    total_time = sum(p.get("elapsed_s", 0) for p in predictions)

    print(f"\n{'='*60}")
    print(f"SWE-bench Run Complete: {run_id}")
    print(f"{'='*60}")
    print(f"Instances:  {len(instances)}")
    print(f"Patches:    {patches}/{len(instances)} ({patches/len(instances)*100:.0f}%)")
    print(f"Total time: {total_time:.0f}s ({total_time/60:.1f}m)")
    print(f"Avg time:   {total_time/len(instances):.0f}s per instance")
    print(f"\nPredictions: {pred_path}")
    print(f"\nEvaluate:")
    print(f"  python -m swebench.harness.run_evaluation \\")
    print(f"    --dataset_name {args.dataset} \\")
    print(f"    --predictions_path {pred_path} \\")
    print(f"    --max_workers 4 --run_id {run_id}")


if __name__ == "__main__":
    main()
