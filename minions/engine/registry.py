"""Blueprint catalog — register and retrieve blueprints by task type."""
from __future__ import annotations

import re

from minions.engine.blueprint import Blueprint, Node, NodeType
from minions.engine.model_router import get_model

# ── Node imports ──────────────────────────────────────────────────────────────
from minions.nodes.deterministic import (
    setup_env,
    assemble_context,
    reproduce_bug,
    run_lint,
    run_tests,
    build_check,
    apply_autofixes,
    code_review,
    create_pr,
    escalate,
    notify,
)
from minions.nodes.agentic import (
    explore,
    write_code,
    fix_lint,
    fix_tests,
    fix_review,
)
from minions.nodes.gates import (
    lint_gate,
    test_gate,
    review_gate,
)


def _make_node(name, node_type, module, description="", **kwargs):
    return Node(
        name=name,
        node_type=node_type,
        execute=module.execute,
        description=description,
        **kwargs,
    )


# ── Blueprint definitions ─────────────────────────────────────────────────────

def build_bug_fix_blueprint(complexity: str = "moderate") -> Blueprint:
    """Bug fix: reproduce → explore → write → lint → test → review → PR.

    Models selected via model_router based on complexity + env-var overrides.
    """
    return Blueprint(
        name="bug_fix",
        description="Fix a bug: reproduce, explore codebase, write fix, validate, open PR",
        nodes=[
            _make_node("setup_env",          NodeType.DETERMINISTIC, setup_env),
            _make_node("assemble_context",   NodeType.DETERMINISTIC, assemble_context),
            _make_node("reproduce_bug",      NodeType.DETERMINISTIC, reproduce_bug),
            _make_node("explore",            NodeType.AGENTIC,       explore,
                       model=get_model("explore", complexity), max_tool_calls=8),
            _make_node("write_code",         NodeType.AGENTIC,       write_code,
                       model=get_model("write_code", complexity), max_tool_calls=16),
            _make_node("run_lint",           NodeType.DETERMINISTIC, run_lint),
            _make_node("lint_gate",          NodeType.GATE,          lint_gate),
            _make_node("fix_lint",           NodeType.AGENTIC,       fix_lint,
                       model=get_model("fix_lint", complexity), max_tool_calls=6),
            _make_node("run_lint_retry",     NodeType.DETERMINISTIC, run_lint),
            _make_node("run_tests",          NodeType.DETERMINISTIC, run_tests),
            _make_node("test_gate",          NodeType.GATE,          test_gate),
            _make_node("apply_autofixes",    NodeType.DETERMINISTIC, apply_autofixes),
            _make_node("fix_tests",          NodeType.AGENTIC,       fix_tests,
                       model=get_model("fix_tests", complexity), max_tool_calls=8),
            _make_node("run_tests_retry",    NodeType.DETERMINISTIC, run_tests),
            _make_node("test_gate_retry",    NodeType.GATE,          test_gate),
            _make_node("build_check",        NodeType.DETERMINISTIC, build_check),
            _make_node("code_review",        NodeType.DETERMINISTIC, code_review),
            _make_node("review_gate",        NodeType.GATE,          review_gate),
            _make_node("fix_review",         NodeType.AGENTIC,       fix_review,
                       model=get_model("fix_review", complexity), max_tool_calls=8),
            _make_node("run_lint_post_review", NodeType.DETERMINISTIC, run_lint),
            _make_node("run_tests_post_review", NodeType.DETERMINISTIC, run_tests),
            _make_node("create_pr",          NodeType.DETERMINISTIC, create_pr),
            _make_node("notify",             NodeType.DETERMINISTIC, notify),
            _make_node("escalate",           NodeType.DETERMINISTIC, escalate),
        ],
        edges={
            # lint_gate routes
            "lint_gate":          {"pass": "run_tests",       "fail": "fix_lint"},
            "fix_lint":           {"pass": "run_lint_retry",  "fail": "run_lint_retry"},
            "run_lint_retry":     {"pass": "run_tests",       "fail": "run_tests"},

            # test_gate routes (first attempt)
            "test_gate":          {"pass": "build_check",     "fail": "apply_autofixes",
                                   "exhausted": "escalate"},
            "apply_autofixes":    {"pass": "fix_tests",       "fail": "fix_tests"},
            "fix_tests":          {"pass": "run_tests_retry", "fail": "run_tests_retry"},
            "run_tests_retry":    {"pass": "test_gate_retry", "fail": "test_gate_retry"},

            # test_gate_retry routes (second attempt)
            "test_gate_retry":    {"pass": "build_check",     "fail": "escalate",
                                   "exhausted": "escalate"},

            # review_gate routes
            "review_gate":        {"pass": "create_pr",       "fail": "fix_review"},
            "fix_review":         {"pass": "run_lint_post_review", "fail": "run_lint_post_review"},
            "run_lint_post_review": {"pass": "run_tests_post_review", "fail": "run_tests_post_review"},
            "run_tests_post_review": {"pass": "create_pr",    "fail": "create_pr"},

            # Terminal nodes
            "create_pr":          {"pass": "notify"},
            "escalate":           {},  # stops here
            "notify":             {},  # stops here
        },
    )


def build_feature_blueprint() -> Blueprint:
    """Feature: explore (more budget) → write (more budget) → validate → PR."""
    bp = build_bug_fix_blueprint()
    bp.name = "feature"
    bp.description = "Implement a feature: explore, write, validate, open PR"

    # Remove reproduce_bug — features don't have failing tests
    bp.nodes = [n for n in bp.nodes if n.name != "reproduce_bug"]

    # Give explore and write_code more budget
    for node in bp.nodes:
        if node.name == "explore":
            node.max_tool_calls = 12
        elif node.name == "write_code":
            node.max_tool_calls = 15

    return bp


def build_migration_blueprint() -> Blueprint:
    """Migration: explore → write → lint → test → PR (no review)."""
    bp = build_bug_fix_blueprint()
    bp.name = "migration"
    bp.description = "Apply migration/refactor across codebase"

    # Remove review gate + fix_review — migrations are formulaic
    remove_nodes = {"code_review", "review_gate", "fix_review",
                    "run_lint_post_review", "run_tests_post_review"}
    bp.nodes = [n for n in bp.nodes if n.name not in remove_nodes]

    # After build_check, go straight to create_pr
    bp.edges["build_check"] = {"pass": "create_pr", "fail": "create_pr"}
    bp.edges["test_gate_retry"] = {"pass": "create_pr", "fail": "escalate",
                                    "exhausted": "escalate"}

    return bp


def build_test_fix_blueprint() -> Blueprint:
    """Test fix: reproduce (3x) → explore → write → test (3 rounds) → PR."""
    bp = build_bug_fix_blueprint()
    bp.name = "test_fix"
    bp.description = "Fix flaky or broken tests"

    # Remove review — test fixes are straightforward
    remove_nodes = {"code_review", "review_gate", "fix_review",
                    "run_lint_post_review", "run_tests_post_review"}
    bp.nodes = [n for n in bp.nodes if n.name not in remove_nodes]
    bp.edges["build_check"] = {"pass": "create_pr", "fail": "create_pr"}

    return bp


# ── Registry ──────────────────────────────────────────────────────────────────

_BLUEPRINTS = {
    "bug_fix": build_bug_fix_blueprint,
    "feature": build_feature_blueprint,
    "migration": build_migration_blueprint,
    "test_fix": build_test_fix_blueprint,
}


def get_blueprint(task_type: str) -> Blueprint:
    """Get a blueprint by task type. Defaults to bug_fix."""
    builder = _BLUEPRINTS.get(task_type, build_bug_fix_blueprint)
    return builder()


def classify_task(title: str, body: str) -> str:
    """Auto-detect task type from title and body text."""
    text = f"{title}\n{body}".lower()

    if any(w in text for w in ["flaky", "test fail", "fix test", "broken test", "intermittent"]):
        return "test_fix"
    if any(w in text for w in ["migrate", "migration", "upgrade", "deprecat", "rename across"]):
        return "migration"
    if any(w in text for w in ["add feature", "implement", "create endpoint", "new api",
                                "add support", "build"]):
        return "feature"
    if any(w in text for w in ["bug", "fix", "error", "crash", "broken", "wrong", "fails",
                                "doesn't work", "issue", "regression"]):
        return "bug_fix"

    return "bug_fix"  # Default
