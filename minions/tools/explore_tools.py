"""Tool subset for the explore/plan phase.

Graph-powered tools + read-only discovery + reasoning.
NO write_file — explorer cannot modify code.
"""
from layer45_agent.tool_defs import (
    get_callers_def,
    get_impact_def,
    get_dependencies_def,
    get_test_coverage_def,
    get_coupled_files_def,
    search_symbols_def,
    get_risk_score_def,
    get_reviewers_def,
    read_file_def,
    search_code_def,
    # New graph tools
    get_top_files_def,
    get_file_info_def,
    get_symbol_details_def,
    get_class_hierarchy_def,
    get_change_context_def,
    # New standard tools
    list_directory_def,
    find_files_def,
    file_outline_def,
    think_def,
    git_log_def,
    git_blame_def,
    batch_read_def,
)

EXPLORE_TOOLS: list[dict] = [
    # Graph-powered (8)
    search_symbols_def,
    get_impact_def,
    get_callers_def,
    get_dependencies_def,
    get_test_coverage_def,
    get_coupled_files_def,
    get_risk_score_def,
    get_reviewers_def,
    # File reading
    read_file_def,
    search_code_def,
    # Discovery & navigation
    list_directory_def,
    find_files_def,
    # File analysis
    file_outline_def,
    # Reasoning
    think_def,
    # Graph (new)
    get_top_files_def,
    get_file_info_def,
    get_symbol_details_def,
    get_class_hierarchy_def,
    get_change_context_def,
    # Git history
    git_log_def,
    git_blame_def,
    # Batch
    batch_read_def,
]
