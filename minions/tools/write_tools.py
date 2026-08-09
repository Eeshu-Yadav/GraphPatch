"""Tool subset for the write_code phase.

Core read/write/search + discovery, analysis, edit safety, and review tools.
NO graph tools — writer already has a plan from the explorer.
"""
from layer45_agent.tool_defs import (
    read_file_def,
    write_file_def,
    search_code_def,
    # Graph tools (enables mid-write blast radius checks)
    get_change_context_def,
    get_impact_def,
    get_test_coverage_def,
    # New tools
    find_files_def,
    file_outline_def,
    think_def,
    undo_edit_def,
    lint_check_def,
    self_review_def,
    checkpoint_def,
    restore_def,
    batch_read_def,
)

WRITE_TOOLS: list[dict] = [
    # Core
    read_file_def,
    write_file_def,
    search_code_def,
    # Graph tools (critical for blast radius verification)
    get_change_context_def,
    get_impact_def,
    get_test_coverage_def,
    # Discovery
    find_files_def,
    # File analysis
    file_outline_def,
    # Reasoning
    think_def,
    # Edit safety
    undo_edit_def,
    checkpoint_def,
    restore_def,
    # Lint
    lint_check_def,
    # Review
    self_review_def,
    # Batch
    batch_read_def,
]
