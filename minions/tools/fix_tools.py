"""Tool subset for fix phases (lint, tests, review).

Core read/write/search + edit safety, reasoning, and test classification.
The fixer already knows what's wrong (from ctx.test_output / ctx.lint_errors).
"""
from layer45_agent.tool_defs import (
    read_file_def,
    write_file_def,
    search_code_def,
    # Graph tools (enables post-failure diagnosis)
    get_change_context_def,
    get_callers_def,
    get_impact_def,
    # New tools
    find_files_def,
    think_def,
    undo_edit_def,
    lint_check_def,
    classify_test_result_def,
    checkpoint_def,
    restore_def,
)

FIX_TOOLS: list[dict] = [
    # Core
    read_file_def,
    write_file_def,
    search_code_def,
    # Graph tools (diagnose what broke)
    get_change_context_def,
    get_callers_def,
    get_impact_def,
    # Discovery
    find_files_def,
    # Reasoning
    think_def,
    # Edit safety
    undo_edit_def,
    checkpoint_def,
    restore_def,
    # Lint & validation
    lint_check_def,
    classify_test_result_def,
]
