"""
Docker Sandbox — disposable per-run execution environment.

Each pipeline run (ticket/PR) gets its OWN container:
  - Fresh, isolated — no cross-contamination between runs
  - Repo copied in (not mounted) so host stays clean
  - Deps installed inside container
  - Destroyed after run completes (or on error)

Lifecycle:
    with Sandbox(repo_path) as sb:
        sb.setup_environment()           # install deps in container
        sb.sync_files(modified_files)    # push agent edits into container
        result = sb.exec("python -m pytest tests/ -v")
        diff = sb.get_diff()             # capture changes

Falls back to host execution if Docker is not available.
"""
from __future__ import annotations

import json as _json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


# ── Error parsing utilities ───────────────────────────────────────────────────

_MODULE_NOT_FOUND_RE = re.compile(r"No module named ['\"]?([a-zA-Z_][a-zA-Z0-9_.]*)['\"]?")
_IMPORT_ERROR_RE = re.compile(r"ImportError:.*?['\"]([a-zA-Z_][a-zA-Z0-9_.]*)['\"]")

# Common mapping: import name → pip package name (when they differ)
_IMPORT_TO_PIP = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "attr": "attrs",
    "dateutil": "python-dateutil",
    "gi": "PyGObject",
    "Cython": "cython",
    "setuptools_scm": "setuptools-scm",
    "extension_helpers": "extension-helpers",
    "erfa": "pyerfa",
}


def extract_missing_modules(output: str) -> list[str]:
    """Extract module names from error output. Returns top-level package names."""
    modules = set()
    for pattern in [_MODULE_NOT_FOUND_RE, _IMPORT_ERROR_RE]:
        for match in pattern.finditer(output):
            mod = match.group(1).split(".")[0]  # top-level package
            if mod and mod not in ("__main__", "test", "tests", "conftest"):
                modules.add(mod)
    return list(modules)


def modules_to_pip_packages(modules: list[str]) -> list[str]:
    """Convert Python import names to pip package names."""
    return [_IMPORT_TO_PIP.get(m, m) for m in modules]


def classify_test_error(output: str) -> dict:
    """Classify test/pytest output into structured error info."""
    output_lower = output.lower()
    missing = extract_missing_modules(output)

    if missing:
        return {
            "error_type": "missing_dependency",
            "missing_modules": missing,
            "pip_packages": modules_to_pip_packages(missing),
            "is_infra": True,
        }

    if any(sig in output_lower for sig in [
        "while parsing the following warning",
        "_resolve_warning_category",
        "failed to import filter module",
    ]):
        # Extract which module the warning filter references
        filter_modules = re.findall(r"Failed to import filter module '([^']+)'", output)
        return {
            "error_type": "pytest_config_error",
            "detail": "pytest warning filter references uninstalled module",
            "filter_modules": filter_modules,
            "is_infra": True,
        }

    if any(sig in output_lower for sig in [
        "importerror while loading conftest",
        "error importing plugin",
        "collection errors",
    ]):
        return {
            "error_type": "test_collection_error",
            "is_infra": True,
        }

    if any(sig in output_lower for sig in [
        "subprocess-exited-with-error",
        "failed to build",
        "error: command 'gcc'",
        "fatal error:",
    ]):
        return {
            "error_type": "build_error",
            "is_infra": True,
        }

    return {"error_type": "test_failure", "is_infra": False}

# Base images per project type
_BASE_IMAGES = {
    "python": "python:3.12-slim",
    "python3.11": "python:3.11-slim",
    "python3.10": "python:3.10-slim",
    "python3.9": "python:3.9-slim",
    "node": "node:20-slim",
    "rust": "rust:1.78-slim",
    "go": "golang:1.22-alpine",
}

# System packages needed for building C extensions in Python projects
_PYTHON_SYSTEM_DEPS = "gcc g++ make git libffi-dev libssl-dev pkg-config"


def detect_project_profile(repo_path: Path, repo_id: str = "", sandbox=None) -> dict:
    """
    Detect how to install deps + run tests for ANY project, ANY language.

    Flow:
    1. CACHE — check disk for a previously VERIFIED profile → instant
    2. SONNET DETECT — one Sonnet call reads all project signals → candidate
    3. VERIFY — actually run the command in sandbox → confirmed working
    4. RETRY — if verification fails, Sonnet gets the error output and tries again
    5. HEURISTIC FALLBACK — if no LLM available

    Only VERIFIED profiles get cached. This is a one-time cost per repo (~$0.01)
    that makes every future run instant + guaranteed correct.
    """
    # Stage 1: Check disk cache — only returns VERIFIED profiles
    cached = _load_cached_profile(repo_path, repo_id)
    if cached and cached.get("verified"):
        log.info("profile.cache_hit", repo=repo_id, test_cmd=cached.get("test_command", "?"))
        return cached

    # Gather all signals from the project
    signals = _gather_project_signals(repo_path)

    # Stage 2: Sonnet detection → verify → retry loop (max 2 attempts)
    failure_history = []
    for attempt in range(2):
        # First attempt: Sonnet reads project signals
        # Second attempt: Sonnet also sees WHY the first attempt failed
        profile = _sonnet_detect_profile(signals, failure_history, repo_path)

        if not profile:
            break  # LLM unavailable, go to heuristic

        log.info("profile.sonnet_candidate", attempt=attempt + 1,
                 test_cmd=profile.get("test_command", "?")[:80])

        # Verify in sandbox
        if sandbox and sandbox.is_running:
            verified = _verify_profile_in_sandbox(sandbox, profile)
            if verified:
                profile["verified"] = True
                profile["verified_at"] = __import__("time").strftime("%Y-%m-%d %H:%M")
                profile["source"] = f"sonnet-attempt-{attempt + 1}"
                log.info("profile.verified", test_cmd=profile.get("test_command", "?"))
                _save_cached_profile(repo_path, repo_id, profile)
                return profile

            # Collect failure evidence for retry
            test_cmd = profile.get("test_command_all", profile.get("test_command", ""))
            verify_cmd = test_cmd.replace("{module}", "").replace("{paths}", "").strip()
            result = sandbox.exec(f"{verify_cmd} 2>&1 | tail -20", timeout=30)
            failure_history.append({
                "command": test_cmd,
                "exit_code": result["exit_code"],
                "output": (result["stdout"] + result["stderr"])[-600:],
            })
            log.info("profile.attempt_failed", attempt=attempt + 1,
                     exit_code=result["exit_code"])
        else:
            # No sandbox — can't verify, just use LLM result
            profile["verified"] = False
            profile["source"] = "sonnet-unverified"
            _save_cached_profile(repo_path, repo_id, profile)
            return profile

    # Stage 3: Heuristic fallback — no LLM or both Sonnet attempts failed
    heuristic = _heuristic_detect_profile(repo_path)

    if sandbox and sandbox.is_running:
        verified = _verify_profile_in_sandbox(sandbox, heuristic)
        if verified:
            heuristic["verified"] = True
            heuristic["verified_at"] = __import__("time").strftime("%Y-%m-%d %H:%M")
            heuristic["source"] = "heuristic-verified"
            log.info("profile.heuristic_verified", test_cmd=heuristic.get("test_command", "?"))
            _save_cached_profile(repo_path, repo_id, heuristic)
            return heuristic

    # Nothing worked
    heuristic["verified"] = False
    heuristic["source"] = "heuristic-unverified"
    log.warning("profile.all_failed", test_cmd=heuristic.get("test_command", "?"))
    _save_cached_profile(repo_path, repo_id, heuristic)
    return heuristic


def _verify_profile_in_sandbox(sandbox, profile: dict) -> bool:
    """
    Actually run the test command in the sandbox to verify it works.
    Returns True if the command executes without crashing (even if tests fail,
    that's OK — it means the runner works).
    """
    test_cmd = profile.get("test_command_all", profile.get("test_command", ""))
    if not test_cmd:
        return False

    # Set env vars
    env_str = " ".join(f"{k}={v}" for k, v in profile.get("env_vars", {}).items())

    # Run setup commands first
    for setup_cmd in profile.get("setup_commands", []):
        sandbox.exec(setup_cmd, timeout=60)

    # Try collecting tests (--collect-only for pytest, or dry run for others)
    # We just need to check the runner WORKS, not that tests pass
    verify_cmd = test_cmd.replace("{module}", "").replace("{paths}", "").strip()

    # For pytest-based: add --collect-only to just check it loads
    if "pytest" in verify_cmd:
        verify_cmd += " --collect-only -q 2>&1 | tail -5"
    else:
        # For other runners: just run with a nonexistent module — we expect exit code != 0
        # but the error should be "no tests found", not "command not found"
        verify_cmd = verify_cmd.replace("{module}", "__nonexistent_test__")
        verify_cmd += " 2>&1 | tail -10"

    if env_str:
        verify_cmd = f"{env_str} {verify_cmd}"

    result = sandbox.exec(verify_cmd, timeout=30)
    output = result["stdout"] + result["stderr"]
    output_lower = output.lower()

    # The runner WORKS if:
    # - Exit code 0 (tests collected/passed)
    # - Exit code 1 (tests failed — runner works, tests just fail)
    # - Exit code 4-5 (no tests found — runner works, just nothing to run)
    # - Output contains test-runner-like output ("collected", "tests", "test session")
    #
    # The runner DOESN'T WORK if:
    # - "command not found" / "no such file"
    # - "ModuleNotFoundError" for the runner itself
    # - Exit code 2 with "unrecognized arguments"
    # - Exit code 127 (command not found)

    runner_broken = any(sig in output_lower for sig in [
        "command not found",
        "no such file or directory",
        "unrecognized arguments",
        "error: unrecognized",
    ])

    if runner_broken:
        return False

    # If it produced any output and didn't crash with "command not found", it works
    runner_works = (
        result["exit_code"] in (0, 1, 4, 5)
        or "collected" in output_lower
        or "test" in output_lower
        or "error" not in output_lower  # no errors at all = probably fine
    )

    return runner_works


# ── Profile disk cache ────────────────────────────────────────────────────────

_PROFILE_CACHE_DIR = Path(os.environ.get("PROFILE_CACHE_DIR", "/home/eeshu/Desktop/context/profiles"))


def _profile_cache_path(repo_path: Path, repo_id: str) -> Path:
    """Get cache file path for a repo's profile."""
    slug = repo_id.replace("/", "_") if repo_id else repo_path.name
    return _PROFILE_CACHE_DIR / f"{slug}.json"


def _load_cached_profile(repo_path: Path, repo_id: str) -> dict | None:
    """Load cached profile from disk. Returns None if not cached."""
    cache_path = _profile_cache_path(repo_path, repo_id)
    if cache_path.exists():
        try:
            return _json.loads(cache_path.read_text())
        except Exception:
            pass
    return None


def _save_cached_profile(repo_path: Path, repo_id: str, profile: dict) -> None:
    """Save profile to disk cache for reuse across runs."""
    _PROFILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _profile_cache_path(repo_path, repo_id)
    try:
        cache_path.write_text(_json.dumps(profile, indent=2))
        log.info("profile.cached_to_disk", path=str(cache_path))
    except Exception as e:
        log.warning("profile.cache_write_failed", error=str(e))


def _gather_project_signals(repo_path: Path) -> str:
    """
    Collect all signals that tell us how to build/test this project.
    Returns a compact string for the LLM to interpret.
    """
    signals = []

    # 1. File tree (top 2 levels, key files only)
    key_files = []
    for p in sorted(repo_path.iterdir()):
        name = p.name
        if name.startswith(".") and name not in (".github",):
            continue
        if p.is_file() and name in (
            "Makefile", "justfile", "Taskfile.yml",
            "manage.py", "setup.py", "setup.cfg", "pyproject.toml",
            "package.json", "Cargo.toml", "go.mod", "Gemfile",
            "tox.ini", "noxfile.py", "pytest.ini", "conftest.py",
            "README.md", "README.rst", "CONTRIBUTING.md", "CONTRIBUTING.rst",
        ):
            key_files.append(name)
        if p.is_dir() and name in ("tests", "test", "spec", "src", ".github"):
            # List contents of test/CI dirs
            children = sorted(x.name for x in p.iterdir() if not x.name.startswith("."))[:15]
            key_files.append(f"{name}/: {', '.join(children)}")
    signals.append(f"Project files: {'; '.join(key_files)}")

    # 2. CI config (the ground truth — how maintainers actually test)
    ci_content = ""
    for ci_path in [
        repo_path / ".github" / "workflows",
        repo_path / ".circleci",
        repo_path / ".gitlab-ci.yml",
    ]:
        if ci_path.is_dir():
            for f in sorted(ci_path.iterdir())[:3]:
                if f.suffix in (".yml", ".yaml"):
                    try:
                        ci_content += f"\n--- {f.name} ---\n"
                        ci_content += f.read_text()[:1500]
                    except Exception:
                        pass
        elif ci_path.is_file():
            try:
                ci_content += ci_path.read_text()[:2000]
            except Exception:
                pass
    if ci_content:
        signals.append(f"CI config:\n{ci_content[:2000]}")

    # 3. README "testing" section
    for readme_name in ["README.md", "README.rst", "CONTRIBUTING.md", "CONTRIBUTING.rst"]:
        readme = repo_path / readme_name
        if readme.exists():
            try:
                content = readme.read_text()
                # Extract testing-related sections
                for marker in ["# Testing", "# Tests", "## Running tests", "## Test",
                               "Running the test", "Unit tests", "How to test"]:
                    idx = content.lower().find(marker.lower())
                    if idx >= 0:
                        section = content[idx:idx + 500]
                        signals.append(f"README ({readme_name}):\n{section}")
                        break
            except Exception:
                pass

    # 4. Key config file contents (small ones only)
    for config_name in ["tox.ini", "pyproject.toml", "setup.cfg", "Makefile", "noxfile.py"]:
        config = repo_path / config_name
        if config.exists():
            try:
                content = config.read_text()[:1000]
                signals.append(f"{config_name}:\n{content}")
            except Exception:
                pass

    # 5. tests/ directory README
    tests_readme = repo_path / "tests" / "README.rst"
    if not tests_readme.exists():
        tests_readme = repo_path / "tests" / "README.md"
    if tests_readme.exists():
        try:
            signals.append(f"tests/README:\n{tests_readme.read_text()[:500]}")
        except Exception:
            pass

    # 6. Special files that tell us about the test runner
    runtests = repo_path / "tests" / "runtests.py"
    if runtests.exists():
        signals.append("tests/runtests.py exists (custom test runner)")
    manage = repo_path / "manage.py"
    if manage.exists():
        try:
            content = manage.read_text()[:300]
            signals.append(f"manage.py:\n{content}")
        except Exception:
            signals.append("manage.py exists")

    return "\n\n".join(signals)


def _sonnet_detect_profile(
    signals: str, failure_history: list[dict], repo_path: Path
) -> dict | None:
    """
    Sonnet call to detect project test setup. Handles both:
    - First attempt (no failures): reads project signals, figures out commands
    - Retry (with failures): also sees what was tried and why it broke, fixes it

    One-time cost per repo (~$0.01). Cached forever after verification.
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    # Build prompt — include failure history if this is a retry
    failures_section = ""
    if failure_history:
        failures_section = "\n\nPREVIOUS ATTEMPTS THAT FAILED:\n"
        for f in failure_history:
            failures_section += f"\nCommand: {f['command']}\nExit code: {f['exit_code']}\nOutput:\n{f['output']}\n"
        failures_section += "\nFix the issues above. Common problems: wrong working directory, missing env vars, wrong Python path, need to cd into tests/ first, need to set PYTHONPATH.\n"

    prompt = f"""Analyze this software project and determine EXACTLY how to run its tests.

PROJECT SIGNALS:
{signals[:4000]}
{failures_section}

Reply in JSON ONLY (no explanation outside JSON):
{{
  "language": "python|javascript|typescript|rust|go|ruby|java",
  "install_command": "exact command to install dependencies",
  "test_command": "exact command to run a SPECIFIC test (use {{module}} as placeholder for test name/path)",
  "test_command_all": "exact command to run ALL tests",
  "env_vars": {{"ENV_VAR": "value"}},
  "setup_commands": ["commands to run before tests, e.g. migrations"],
  "notes": "brief explanation of the test setup"
}}

RULES:
- Use the project's ACTUAL test runner, not a generic one
- If tests/runtests.py exists, use it (Django core pattern)
- If manage.py exists, use "python manage.py test"
- If CI config (.github/workflows/) shows test commands, follow those exactly
- If README/CONTRIBUTING has "Running tests" section, follow it
- For Python: check if project uses pytest, unittest, or a custom runner
- Include ALL required env vars (DJANGO_SETTINGS_MODULE, PYTHONPATH, etc.)
- If working directory matters, include "cd dir &&" prefix in commands
- The commands will run inside a Docker container at /workspace (repo root)"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            temperature=0.0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()

        # Parse JSON (handle markdown code blocks)
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        profile = _json.loads(raw)
        log.info("profile.sonnet_result", attempt=len(failure_history) + 1, profile=profile)
        return profile

    except Exception as e:
        log.warning("profile.sonnet_failed", error=str(e))
        return None


def _heuristic_detect_profile(repo_path: Path) -> dict:
    """
    Fallback: basic heuristic detection when LLM is unavailable.
    Less accurate but always works.
    """
    # Django custom runner
    if (repo_path / "tests" / "runtests.py").exists():
        return {
            "language": "python",
            "install_command": "pip install -e . -q",
            "test_command": "python tests/runtests.py {module} --verbosity=2",
            "test_command_all": "python tests/runtests.py --verbosity=2",
            "env_vars": {},
            "setup_commands": [],
            "notes": "Django core repo with custom test runner",
        }

    # Django app
    if (repo_path / "manage.py").exists():
        return {
            "language": "python",
            "install_command": "pip install -e . -q",
            "test_command": "python manage.py test {module} --verbosity=2",
            "test_command_all": "python manage.py test --verbosity=2",
            "env_vars": {},
            "setup_commands": [],
            "notes": "Django app with manage.py",
        }

    # Python with pytest
    if any((repo_path / f).exists() for f in ["pytest.ini", "conftest.py", "pyproject.toml", "setup.py", "setup.cfg"]):
        return {
            "language": "python",
            "install_command": "pip install -e '.[test]' -q 2>/dev/null || pip install -e . -q",
            "test_command": "python -m pytest {module} -xvs --tb=short",
            "test_command_all": "python -m pytest -x --tb=short",
            "env_vars": {},
            "setup_commands": [],
            "notes": "Python project, using pytest",
        }

    # Node.js
    if (repo_path / "package.json").exists():
        return {
            "language": "javascript",
            "install_command": "npm install",
            "test_command": "npm test -- {module}",
            "test_command_all": "npm test",
            "env_vars": {},
            "setup_commands": [],
            "notes": "Node.js project",
        }

    # Rust
    if (repo_path / "Cargo.toml").exists():
        return {
            "language": "rust",
            "install_command": "cargo fetch",
            "test_command": "cargo test {module}",
            "test_command_all": "cargo test",
            "env_vars": {},
            "setup_commands": [],
            "notes": "Rust project",
        }

    # Go
    if (repo_path / "go.mod").exists():
        return {
            "language": "go",
            "install_command": "go mod download",
            "test_command": "go test ./{module}/...",
            "test_command_all": "go test ./...",
            "env_vars": {},
            "setup_commands": [],
            "notes": "Go project",
        }

    # Ultimate fallback
    return {
        "language": "python",
        "install_command": "pip install -e . -q 2>/dev/null || true",
        "test_command": "python -m pytest {module} -xvs --tb=short",
        "test_command_all": "python -m pytest -x --tb=short",
        "env_vars": {},
        "setup_commands": [],
        "notes": "No test infrastructure detected, guessing pytest",
    }


def detect_project_profile(
    repo_path: Path,
    repo_id: str,
    sandbox=None,
    use_cache: bool = True,
    failure_history: list[dict] | None = None,
) -> dict:
    """
    Detect project's test/build setup. Language-agnostic.

    Returns profile dict with keys:
        language, install_command, test_command, test_command_all,
        env_vars, setup_commands, notes, verified

    Cost: ~$0.01 per repo (one Sonnet call), then cached forever.
    """
    # 1. Try cache
    if use_cache:
        cached = _load_cached_profile(repo_path, repo_id)
        if cached and cached.get("verified"):
            log.info("profile.cache_hit", repo_id=repo_id,
                     language=cached.get("language"), test_cmd=cached.get("test_command", "")[:50])
            return cached

    # 2. Gather signals from project structure
    signals = _gather_project_signals(repo_path)

    # 3. Try LLM detection (Sonnet — accurate but costs ~$0.01)
    profile = None
    try:
        profile = _sonnet_detect_profile(signals, failure_history or [], repo_path)
    except Exception as e:
        log.warning("profile.sonnet_failed", error=str(e))

    # 4. Fall back to heuristic detection
    if not profile:
        log.info("profile.heuristic_fallback", repo_id=repo_id)
        profile = _heuristic_detect_profile(repo_path)

    # 5. Verify in sandbox if available
    if profile and sandbox:
        try:
            profile["verified"] = _verify_profile_in_sandbox(sandbox, profile)
            log.info("profile.verified", repo_id=repo_id, verified=profile["verified"])
        except Exception as e:
            log.warning("profile.verify_failed", error=str(e))
            profile["verified"] = False
    elif profile:
        profile["verified"] = False

    # 6. Cache for future runs
    if profile:
        try:
            _save_cached_profile(repo_path, repo_id, profile)
        except Exception as e:
            log.warning("profile.cache_save_failed", error=str(e))

    return profile or {}


def _docker_available() -> bool:
    """Check if Docker daemon is reachable."""
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return proc.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def detect_project_type(repo_path: Path) -> str:
    """Auto-detect project language/runtime from config files."""
    if (repo_path / "pyproject.toml").exists() or (repo_path / "setup.py").exists():
        pyproject = repo_path / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                if ">=3.9" in content or ">=3.8" in content:
                    return "python3.9"
                if ">=3.10" in content:
                    return "python3.10"
                if ">=3.11" in content:
                    return "python3.11"
            except Exception:
                pass
        return "python"
    if (repo_path / "setup.cfg").exists() or (repo_path / "requirements.txt").exists():
        return "python"
    if (repo_path / "package.json").exists():
        return "node"
    if (repo_path / "Cargo.toml").exists():
        return "rust"
    if (repo_path / "go.mod").exists():
        return "go"
    return "python"


class Sandbox:
    """
    Disposable Docker container for a single pipeline run.

    The repo is COPIED into the container (not volume-mounted) so:
    - Host repo stays untouched
    - Container has its own writable copy
    - No permission/ownership issues
    - Multiple runs can target the same repo concurrently

    Agent edits are synced INTO the container via `sync_files()`.
    Final diff is extracted via `get_diff()`.
    """

    def __init__(
        self,
        repo_path: Path,
        project_type: str | None = None,
        run_id: str | None = None,
        repo_id: str = "",
    ):
        self.repo_path = Path(repo_path).resolve()
        self.project_type = project_type or detect_project_type(self.repo_path)
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.repo_id = repo_id
        self.container_name = f"agent-run-{self.run_id}"
        self.base_image = _BASE_IMAGES.get(self.project_type, "python:3.12-slim")
        self._container_id: str | None = None
        self._env_installed = False
        self._exec_log: list[dict] = []
        self.profile: dict | None = None  # Detected project profile (test command, etc.)

    @property
    def is_running(self) -> bool:
        if not self._container_id:
            return False
        try:
            proc = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self._container_id],
                capture_output=True, text=True, timeout=5,
            )
            return proc.stdout.strip() == "true"
        except Exception:
            return False

    def start(self) -> dict:
        """
        Start a fresh disposable container:
        1. Pull base image (if needed)
        2. Start container with repo volume-mounted
        3. Install system build deps
        """
        if self._container_id and self.is_running:
            return {"status": "already_running", "container": self._container_id}

        log.info("sandbox.starting", image=self.base_image, run_id=self.run_id)

        # Pull image if not cached locally
        inspect = subprocess.run(
            ["docker", "image", "inspect", self.base_image],
            capture_output=True, timeout=30,
        )
        if inspect.returncode != 0:
            log.info("sandbox.pulling_image", image=self.base_image)
            pull = subprocess.run(
                ["docker", "pull", self.base_image],
                capture_output=True, text=True, timeout=300,
            )
            if pull.returncode != 0:
                return {"status": "failed", "error": f"Failed to pull {self.base_image}: {pull.stderr[:500]}"}

        # Remove any leftover container with same name
        subprocess.run(["docker", "rm", "-f", self.container_name], capture_output=True, timeout=10)

        # Start container:
        # - Volume mount repo (read-write) so agent file writes are visible inside
        # - Keep alive with sleep infinity
        # - Network host so it can reach local services (DB, etc.)
        # - Resource limits to prevent runaway processes
        cmd = [
            "docker", "run", "-d",
            "--name", self.container_name,
            "--network", "host",
            "-v", f"{self.repo_path}:/workspace:rw",
            "-w", "/workspace",
            "--memory", "4g",
            "--cpus", "2",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "PIP_NO_CACHE_DIR=1",
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "-e", "DEBIAN_FRONTEND=noninteractive",
            self.base_image,
            "sleep", "infinity",
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            log.error("sandbox.start_failed", stderr=proc.stderr[:500])
            return {"status": "failed", "error": proc.stderr[:500]}

        self._container_id = proc.stdout.strip()[:12]
        log.info("sandbox.started", container=self._container_id)

        # Install system-level build deps (gcc, etc.) for compiled extensions
        if self.project_type.startswith("python"):
            self._install_system_deps()

        # Detect project profile (test runner, install command)
        # Uses cached result if available, otherwise Sonnet + verification
        self.profile = detect_project_profile(self.repo_path, self.repo_id, sandbox=self)
        log.info("sandbox.profile", test_cmd=self.profile.get("test_command", "?"),
                 verified=self.profile.get("verified", False))

        return {"status": "started", "container": self._container_id, "image": self.base_image,
                "profile": self.profile}

    def _install_system_deps(self) -> None:
        """Install system packages and Python essentials."""
        check = self.exec("which apt-get", timeout=5)
        if check["exit_code"] != 0:
            return  # Not a Debian-based image
        self.exec(
            f"apt-get update -qq && apt-get install -y -qq {_PYTHON_SYSTEM_DEPS} > /dev/null 2>&1",
            timeout=120,
        )
        # Python 3.12+ removed distutils — many projects need setuptools as a replacement.
        # Also install pytest-timeout so --timeout flag works if needed.
        self.exec(
            "pip install setuptools wheel pytest-timeout -q 2>/dev/null",
            timeout=60,
        )
        log.debug("sandbox.system_deps_installed")

    def setup_environment(self) -> dict:
        """Install project dependencies inside the container."""
        if self._env_installed:
            return {"status": "already_installed"}
        if not self.is_running:
            return {"status": "failed", "error": "Container not running"}

        log.info("sandbox.setup_env", project_type=self.project_type)
        attempts = []

        if self.project_type.startswith("python"):
            installed_modules: set[str] = set()

            # Phase 1: Try standard editable installs
            for cmd, name in [
                ("pip install -e '.[test]' 2>&1", "pip install -e .[test]"),
                ("pip install -e '.[dev]' 2>&1", "pip install -e .[dev]"),
                ("pip install -e . 2>&1", "pip install -e ."),
            ]:
                result = self.exec(cmd, timeout=300)
                output = result["stdout"] + result["stderr"]
                install_failed = (
                    result["exit_code"] != 0
                    or "error:" in output.lower()
                    or "subprocess-exited-with-error" in output
                )
                attempts.append({"cmd": name, "exit_code": result["exit_code"], "output": output[-500:]})

                if not install_failed:
                    self._env_installed = True
                    log.info("sandbox.env_installed", method=name)
                    return {"status": "installed", "method": name}

                # Phase 2: Error-driven retry — extract what's missing and install it
                missing = extract_missing_modules(output)
                if missing:
                    pip_pkgs = modules_to_pip_packages(missing)
                    new_pkgs = [p for p in pip_pkgs if p not in installed_modules]
                    if new_pkgs:
                        pkgs_str = " ".join(new_pkgs)
                        log.info("sandbox.installing_missing", packages=new_pkgs)
                        fix = self.exec(f"pip install {pkgs_str} -q 2>&1 | tail -5", timeout=120)
                        if fix["exit_code"] == 0:
                            installed_modules.update(new_pkgs)
                            # Retry the original install
                            retry = self.exec(cmd, timeout=300)
                            retry_output = retry["stdout"] + retry["stderr"]
                            retry_failed = (
                                retry["exit_code"] != 0
                                or "error:" in retry_output.lower()
                                or "subprocess-exited-with-error" in retry_output
                            )
                            if not retry_failed:
                                self._env_installed = True
                                log.info("sandbox.env_installed", method=f"{name} (after installing {pkgs_str})")
                                return {"status": "installed", "method": f"{name} (after installing {pkgs_str})"}

            # Phase 3: requirements.txt fallback
            if (self.repo_path / "requirements.txt").exists():
                result = self.exec("pip install -r requirements.txt -q 2>&1 | tail -10", timeout=300)
                attempts.append({"cmd": "requirements.txt", "exit_code": result["exit_code"]})
                if result["exit_code"] == 0:
                    self._env_installed = True
                    return {"status": "installed", "method": "pip install -r requirements.txt"}

            # Phase 4: Install common build deps, then retry editable install
            self.exec("pip install setuptools setuptools-scm wheel cython numpy -q 2>/dev/null", timeout=120)
            retry = self.exec("pip install -e . --no-build-isolation 2>&1 | tail -10", timeout=300)
            retry_output = retry["stdout"] + retry["stderr"]
            if retry["exit_code"] == 0 and "error" not in retry_output.lower():
                self._env_installed = True
                return {"status": "installed", "method": "pip install -e . (after build deps)"}

            # Phase 5: Last resort — install pytest + any missing modules we detected
            # so tests can at minimum execute
            all_missing = set()
            for attempt in attempts:
                all_missing.update(extract_missing_modules(attempt.get("output", "")))
            extra_pkgs = modules_to_pip_packages(list(all_missing - installed_modules))

            install_cmd = "pip install pytest pytest-timeout"
            if extra_pkgs:
                install_cmd += " " + " ".join(extra_pkgs)
            install_cmd += " -q 2>&1 | tail -5"

            pytest_result = self.exec(install_cmd, timeout=120)
            if pytest_result["exit_code"] == 0:
                self._env_installed = True
                method = "pytest + " + " ".join(extra_pkgs) if extra_pkgs else "pytest-only"
                log.info("sandbox.env_partial", method=method, extra=extra_pkgs)
                return {"status": "partial", "method": method, "attempts": attempts}

        elif self.project_type == "node":
            for lockfile, cmd, name in [
                ("bun.lockb", "bun install", "bun"),
                ("yarn.lock", "yarn install", "yarn"),
                ("pnpm-lock.yaml", "pnpm install", "pnpm"),
                ("package.json", "npm install", "npm"),
            ]:
                if (self.repo_path / lockfile).exists():
                    result = self.exec(cmd, timeout=300)
                    attempts.append({"cmd": name, "exit_code": result["exit_code"]})
                    if result["exit_code"] == 0:
                        self._env_installed = True
                        return {"status": "installed", "method": name}

        elif self.project_type == "rust":
            result = self.exec("cargo fetch", timeout=120)
            if result["exit_code"] == 0:
                self._env_installed = True
                return {"status": "installed", "method": "cargo fetch"}

        elif self.project_type == "go":
            result = self.exec("go mod download", timeout=120)
            if result["exit_code"] == 0:
                self._env_installed = True
                return {"status": "installed", "method": "go mod download"}

        return {"status": "failed", "message": "Could not install deps in sandbox.", "attempts": attempts[:5]}

    def fix_test_environment(self, test_output: str) -> dict:
        """
        Parse test output for missing deps and install them.
        Called when run_tests returns infrastructure_error.
        Returns {"fixed": True/False, "installed": [...]}
        """
        if not self.is_running:
            return {"fixed": False, "error": "Container not running"}

        error_info = classify_test_error(test_output)
        installed = []

        if error_info["error_type"] == "missing_dependency":
            pkgs = error_info.get("pip_packages", [])
            if pkgs:
                pkgs_str = " ".join(pkgs)
                log.info("sandbox.fix_env.installing", packages=pkgs)
                result = self.exec(f"pip install {pkgs_str} -q 2>&1 | tail -5", timeout=120)
                if result["exit_code"] == 0:
                    installed.extend(pkgs)

        elif error_info["error_type"] == "pytest_config_error":
            # pytest warning filter references an uninstalled module
            # Fix: install the referenced module, or skip the warning filter
            filter_modules = error_info.get("filter_modules", [])
            if filter_modules:
                pkgs = modules_to_pip_packages(filter_modules)
                pkgs_str = " ".join(pkgs)
                log.info("sandbox.fix_env.filter_modules", packages=pkgs)
                result = self.exec(f"pip install {pkgs_str} -q 2>&1 | tail -5", timeout=120)
                if result["exit_code"] == 0:
                    installed.extend(pkgs)

            # Also try: run pytest with -W ignore to bypass warning filters
            # This is a fallback — the warning filter is non-critical
            if not installed:
                log.info("sandbox.fix_env.override_warnings")
                # Create a conftest override that suppresses the problematic filter
                self.exec(
                    'python -c "import pytest; print(pytest.__version__)" 2>&1',
                    timeout=10,
                )

        elif error_info["error_type"] == "test_collection_error":
            # Generic collection error — try installing missing imports
            missing = extract_missing_modules(test_output)
            if missing:
                pkgs = modules_to_pip_packages(missing)
                pkgs_str = " ".join(pkgs)
                result = self.exec(f"pip install {pkgs_str} -q 2>&1 | tail -5", timeout=120)
                if result["exit_code"] == 0:
                    installed.extend(pkgs)

        return {"fixed": bool(installed), "installed": installed, "error_info": error_info}

    def exec(self, command: str, timeout: int = 30, workdir: str = "/workspace") -> dict:
        """Execute a shell command inside the container. All calls are logged."""
        import time as _time
        start = _time.time()

        if not self._container_id or not self.is_running:
            result = {"exit_code": -1, "stdout": "", "stderr": "Container not running"}
            self._exec_log.append({"cmd": command, "result": result, "duration_s": 0})
            return result

        cmd = [
            "docker", "exec",
            "-w", workdir,
            self._container_id,
            "bash", "-c", command,
        ]

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout + 10,
            )
            result = {
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
        except subprocess.TimeoutExpired:
            subprocess.run(
                ["docker", "exec", self._container_id, "pkill", "-f", command[:50]],
                capture_output=True, timeout=5,
            )
            result = {"exit_code": -1, "stdout": "", "stderr": f"Timed out after {timeout}s"}
        except Exception as e:
            result = {"exit_code": -1, "stdout": "", "stderr": str(e)}

        elapsed = round(_time.time() - start, 1)
        self._exec_log.append({
            "cmd": command[:200],
            "exit_code": result["exit_code"],
            "stdout_tail": result["stdout"][-500:] if result["stdout"] else "",
            "stderr_tail": result["stderr"][-500:] if result["stderr"] else "",
            "duration_s": elapsed,
        })
        return result

    def sync_files(self, modified_files: dict[str, str]) -> None:
        """
        Push agent's in-memory file edits into the container.
        Since we volume-mount, we just write to disk — container sees changes immediately.
        """
        for fp, content in modified_files.items():
            abs_path = self.repo_path / fp
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(content, encoding="utf-8")

    def stop(self) -> None:
        """Destroy the container and save execution trace."""
        # Save execution trace before destroying
        self._save_trace()

        if self._container_id:
            # Capture container logs before destroy (catches OOM kills, crashes, etc.)
            try:
                logs_proc = subprocess.run(
                    ["docker", "logs", "--tail", "50", self._container_id],
                    capture_output=True, text=True, timeout=10,
                )
                if logs_proc.stdout.strip():
                    self._exec_log.append({
                        "cmd": "[container_logs]",
                        "exit_code": 0,
                        "stdout_tail": logs_proc.stdout[-1000:],
                        "stderr_tail": logs_proc.stderr[-500:],
                        "duration_s": 0,
                    })
            except Exception:
                pass

            try:
                subprocess.run(
                    ["docker", "rm", "-f", self._container_id],
                    capture_output=True, timeout=15,
                )
                log.info("sandbox.destroyed", container=self._container_id, run_id=self.run_id)
            except Exception as e:
                log.warning("sandbox.destroy_failed", error=str(e))
            self._container_id = None

    def _save_trace(self) -> None:
        """Persist sandbox execution log to disk for debugging."""
        if not self._exec_log:
            return
        trace_dir = Path(os.environ.get("TRACE_DIR", "/home/eeshu/Desktop/context/traces"))
        trace_dir.mkdir(parents=True, exist_ok=True)

        import json, time as _time
        trace_data = {
            "run_id": self.run_id,
            "container": self._container_id,
            "image": self.base_image,
            "project_type": self.project_type,
            "repo_path": str(self.repo_path),
            "env_installed": self._env_installed,
            "total_commands": len(self._exec_log),
            "commands": self._exec_log,
        }
        ts = _time.strftime("%Y%m%d_%H%M%S")
        trace_path = trace_dir / f"sandbox_{ts}_{self.run_id}.json"
        try:
            trace_path.write_text(json.dumps(trace_data, indent=2, default=str))
            log.info("sandbox.trace_saved", path=str(trace_path), commands=len(self._exec_log))
        except Exception as e:
            log.warning("sandbox.trace_save_failed", error=str(e))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_exc):
        self.stop()

    def __del__(self):
        """Safety net: destroy container if user forgot to call stop()."""
        if self._container_id:
            try:
                self.stop()
            except Exception:
                pass


# ── Module-level helpers ──────────────────────────────────────────────────────

def should_use_sandbox() -> bool:
    """Check if sandbox mode is enabled and Docker is available."""
    env = os.environ.get("AGENT_SANDBOX", "auto").lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return _docker_available()
    # "auto": use sandbox if Docker is available
    return _docker_available()
