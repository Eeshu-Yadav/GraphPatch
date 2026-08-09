"""Claude tool schemas for all agent tools."""
from __future__ import annotations


# ── Graph-Powered Tools (8) ──────────────────────────────────────────────────

get_callers_def = {
    "name": "get_callers",
    "description": "Find all functions that call a given symbol. Returns callers with file paths and centrality (importance) scores from the codebase knowledge graph.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol_name": {"type": "string", "description": "Name of the function/class to find callers for"},
            "depth": {"type": "integer", "description": "Hop depth for caller search (default 1 = direct callers only)"},
        },
        "required": ["symbol_name"],
    },
}

get_impact_def = {
    "name": "get_impact",
    "description": "Analyze what breaks if a symbol is changed. Returns static callers (will_break) and dynamic dispatch callers (may_break) up to N hops deep. ALWAYS call this before changing a function signature.",
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol_name": {"type": "string", "description": "Name of the function/class to analyze impact for"},
            "depth": {"type": "integer", "description": "How many call-chain hops to trace (default 2)"},
        },
        "required": ["symbol_name"],
    },
}

get_dependencies_def = {
    "name": "get_dependencies",
    "description": "Get the import dependency graph for a file. Returns which files this file imports (dependencies) and which files import this file (dependents).",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path within the repo"},
        },
        "required": ["file_path"],
    },
}

get_test_coverage_def = {
    "name": "get_test_coverage",
    "description": "Find which test files cover a given source file. Uses TEST_FOR edges in the knowledge graph.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative path of the source file"},
        },
        "required": ["file_path"],
    },
}

get_coupled_files_def = {
    "name": "get_coupled_files",
    "description": "Find files that historically change together with the given file (git co-change analysis). High coupling score means these files almost always change together.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path"},
            "min_score": {"type": "number", "description": "Minimum coupling score 0-1 (default 0.1)"},
        },
        "required": ["file_path"],
    },
}

search_symbols_def = {
    "name": "search_symbols",
    "description": "Semantic search over all indexed code symbols (functions, classes, files) using natural language. Powered by vector embeddings — finds code by description, not just name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Natural language description of what you're looking for (e.g. 'authentication middleware', 'database connection pool')"},
            "limit": {"type": "integer", "description": "Max results to return (default 10)"},
        },
        "required": ["query"],
    },
}

get_risk_score_def = {
    "name": "get_risk_score",
    "description": "Get a composite risk score for changing a file. Combines centrality, number of dependents, and test coverage. Higher score = more careful changes needed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path to assess risk for"},
        },
        "required": ["file_path"],
    },
}

get_reviewers_def = {
    "name": "get_reviewers",
    "description": "Find who should review changes to the given files, based on CODEOWNERS configuration.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of changed file paths",
            },
        },
        "required": ["file_paths"],
    },
}

get_top_files_def = {
    "name": "get_top_files",
    "description": (
        "Find the most important files in a directory by PageRank centrality. "
        "High centrality = many files depend on it (architectural hub). "
        "Use to understand which files are load-bearing before touching a module."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path_prefix": {"type": "string", "description": "Directory prefix (e.g. 'django/core/', 'src/components/'). Empty = whole repo."},
            "limit": {"type": "integer", "description": "Max files to return (default 10)"},
        },
    },
}

get_file_info_def = {
    "name": "get_file_info",
    "description": (
        "Get metadata about a file from the knowledge graph: language, line count, centrality score, "
        "AI-generated summary, and whether it's a test file. "
        "Use BEFORE reading or editing a file to assess its importance and role."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path"},
        },
        "required": ["file_path"],
    },
}

get_symbol_details_def = {
    "name": "get_symbol_details",
    "description": (
        "Look up one or more symbols by exact name. Returns file path, type (Function/Class), "
        "line number, centrality score, docstring, and AI summary for each. "
        "Faster than search_symbols when you already know the exact names."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Symbol names to look up (max 10)",
            },
        },
        "required": ["names"],
    },
}

get_class_hierarchy_def = {
    "name": "get_class_hierarchy",
    "description": (
        "Get the inheritance hierarchy for a class: parent classes it extends "
        "and child classes that inherit from it. Use to understand polymorphism, "
        "find overridable methods, and avoid breaking subclasses when modifying a class."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_name": {"type": "string", "description": "Name of the class"},
        },
        "required": ["class_name"],
    },
}

get_change_context_def = {
    "name": "get_change_context",
    "description": (
        "Get a comprehensive pre-change analysis for a file/symbol in ONE call. Returns: "
        "risk score, callers that will break, dependents, test coverage, coupled files, and file info. "
        "This is the SINGLE MOST IMPORTANT tool to call BEFORE writing code — it replaces calling "
        "get_risk_score + get_impact + get_dependencies + get_test_coverage + get_coupled_files individually."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File you plan to modify"},
            "symbol_name": {"type": "string", "description": "Symbol you plan to change (optional — adds caller/impact analysis)"},
        },
        "required": ["file_path"],
    },
}


# ── Standard Tools (7) ──────────────────────────────────────────────────────

read_file_def = {
    "name": "read_file",
    "description": "Read a file from the repository. Optionally specify a line range to read only a section of a large file.",
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path within the repo"},
            "start_line": {"type": "integer", "description": "First line to read (1-based, default: 1)"},
            "end_line": {"type": "integer", "description": "Last line to read (0 = entire file, default: 0)"},
        },
        "required": ["file_path"],
    },
}

write_file_def = {
    "name": "write_file",
    "description": (
        "Apply edits to a file. Two modes: "
        "(1) Search-replace: each edit has 'search' (exact text to find) and 'replace' (replacement). "
        "(2) Line-range: each edit has 'start_line', 'end_line' (1-based), and 'replace'. "
        "Line-range mode is more reliable for large files — use it when you know the exact line numbers from read_file. "
        "Use search='' with replace=<content> to create a new file. "
        "Plan ALL edits for a file before calling — ONE call per file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "search": {"type": "string", "description": "Exact text to find (for search-replace mode)"},
                        "replace": {"type": "string", "description": "Replacement text"},
                        "start_line": {"type": "integer", "description": "First line to replace (1-based, for line-range mode)"},
                        "end_line": {"type": "integer", "description": "Last line to replace (1-based, for line-range mode)"},
                    },
                    "required": ["replace"],
                },
                "description": "List of edits. Each edit uses EITHER search+replace OR start_line+end_line+replace.",
            },
        },
        "required": ["file_path", "edits"],
    },
}

search_code_def = {
    "name": "search_code",
    "description": "Search the repository for code matching a regex pattern (like grep). Returns matching lines with file paths and line numbers.",
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "file_glob": {"type": "string", "description": "File pattern to filter (e.g. '*.py', '*.ts', default: all files)"},
            "max_results": {"type": "integer", "description": "Max matches to return (default 20)"},
        },
        "required": ["pattern"],
    },
}

run_tests_def = {
    "name": "run_tests",
    "description": "Run the project's test suite and optionally the linter. Flushes all pending file edits to disk before running. Auto-detects the test framework (pytest, Jest, go test, cargo test, etc.). Returns pass/fail counts and output.",
    "input_schema": {
        "type": "object",
        "properties": {
            "test_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific test files or directories to run (empty = auto-detect)",
            },
            "include_lint": {"type": "boolean", "description": "Also run ruff linter (default true)"},
        },
    },
}

run_command_def = {
    "name": "run_command",
    "description": "Execute a shell command in the repository root. Use for running type checkers, formatters, build scripts, or any CLI tool. Output is capped at 5000 chars. Timeout: 30 seconds.",
    "input_schema": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute (e.g. 'python -m py_compile file.py', 'npm run build')"},
        },
        "required": ["command"],
    },
}

get_diff_def = {
    "name": "get_diff",
    "description": "Get a unified diff of all changes you have made so far. Use this to review your work before calling finish(). Shows what was added/removed in each file.",
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

build_check_def = {
    "name": "build_check",
    "description": "Check if code compiles/builds successfully. Auto-detects build system (npm, cargo, tsc, etc.) and runs it. Call this after making changes to catch compilation errors early.",
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

finish_def = {
    "name": "finish",
    "description": "Signal that all changes are complete. You MUST call get_diff to review changes before calling this. Call this when you are done implementing and have verified your changes.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Summary of all changes made"},
            "files_changed": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths that were modified or created",
            },
        },
        "required": ["summary", "files_changed"],
    },
}


# ── Discovery & Navigation Tools ─────────────────────────────────────────────

list_directory_def = {
    "name": "list_directory",
    "description": (
        "List files and subdirectories at a path. Shows file sizes and types in a compact tree format. "
        "Use to understand project structure BEFORE searching. Much faster than search_code for orientation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative directory path (default: repo root)"},
            "depth": {"type": "integer", "description": "Max depth to recurse (default 1, max 3)"},
            "pattern": {"type": "string", "description": "Glob filter for files (e.g. '*.py', '*.ts')"},
        },
    },
}

find_files_def = {
    "name": "find_files",
    "description": (
        "Find files by name pattern (glob). Use when you know part of the filename but not the full path. "
        "Much faster than search_code for file discovery. Supports ** for recursive matching."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. '*validator*', 'test_*.py', '**/*.tsx')"},
            "path": {"type": "string", "description": "Directory to search in (default: repo root)"},
            "max_results": {"type": "integer", "description": "Max files to return (default 20)"},
        },
        "required": ["pattern"],
    },
}


# ── Reasoning Tool ───────────────────────────────────────────────────────────

think_def = {
    "name": "think",
    "description": (
        "Use this tool to think through complex problems step-by-step. Write your analysis, hypotheses, "
        "and plan. This does NOT modify files or execute anything — it is a scratchpad for reasoning. "
        "Use BEFORE making complex changes to reduce wasted tool calls."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "thought": {"type": "string", "description": "Your analysis, reasoning, or plan"},
        },
        "required": ["thought"],
    },
}


# ── Edit Safety Tools ────────────────────────────────────────────────────────

undo_edit_def = {
    "name": "undo_edit",
    "description": (
        "Revert the last write_file edit to a specific file. Restores the file to its state before "
        "the most recent write_file call. Use when your edit made things worse or tests fail after your change. "
        "Supports up to 5 undo levels per file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative path of the file to revert"},
        },
        "required": ["file_path"],
    },
}


# ── File Analysis Tools ──────────────────────────────────────────────────────

file_outline_def = {
    "name": "file_outline",
    "description": (
        "Get the structure of a file: imports, class/struct definitions, function/method signatures, "
        "and decorators — WITHOUT function bodies. Shows line numbers for each symbol. "
        "Works with Python (AST-based), JS/TS, Rust, Go, Java, C#, Ruby, PHP, C/C++, Swift, Kotlin. "
        "Use this BEFORE read_file to understand a file's layout, then read_file with start_line/end_line "
        "for specific functions. Saves 10-30x tokens vs reading the full file."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path"},
        },
        "required": ["file_path"],
    },
}


# ── Lint & Validation Tools ──────────────────────────────────────────────────

lint_check_def = {
    "name": "lint_check",
    "description": (
        "Run the appropriate linter on a single file and return structured errors. "
        "Auto-detects linter by file type (ruff/flake8 for Python, ESLint for JS/TS, clippy for Rust, go vet for Go). "
        "Much faster than run_tests(include_lint=True) which lints the whole project. "
        "Call this right after write_file to catch issues early."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative path of the file to lint"},
        },
        "required": ["file_path"],
    },
}

classify_test_result_def = {
    "name": "classify_test_result",
    "description": (
        "Classify test output into structured categories: assertion_failure, import_error, runtime_error, "
        "compile_error, infra_error, timeout, no_tests. Works with all test frameworks "
        "(pytest, Jest, Mocha, Go test, cargo test, JUnit, etc.). "
        "Returns category, error context, and suggested_action. "
        "Call this INSTEAD of parsing raw test output yourself — saves tokens and improves fix accuracy."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "test_output": {"type": "string", "description": "Raw test output from run_tests"},
            "exit_code": {"type": "integer", "description": "Exit code from test run (0=pass, 1=fail, 5=no tests)"},
        },
        "required": ["test_output"],
    },
}


# ── Git History Tools ────────────────────────────────────────────────────────

git_log_def = {
    "name": "git_log",
    "description": (
        "Get recent git commits for a file or the whole repo. Shows commit hashes, messages, authors, and dates. "
        "Use to understand WHY code is written a certain way — the commit message often explains the intent."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "File to get history for (omit for whole repo)"},
            "max_commits": {"type": "integer", "description": "Max commits to return (default 10)"},
        },
    },
}

git_blame_def = {
    "name": "git_blame",
    "description": (
        "Show who last modified each line in a file range. Includes commit hash, author, and commit message. "
        "Use to understand the intent behind specific code when the current logic seems unusual."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Relative file path to blame"},
            "start_line": {"type": "integer", "description": "Start line (1-based)"},
            "end_line": {"type": "integer", "description": "End line (1-based)"},
        },
        "required": ["file_path"],
    },
}


# ── Batch & Review Tools ─────────────────────────────────────────────────────

batch_read_def = {
    "name": "batch_read",
    "description": (
        "Read multiple files in a single call. More efficient than multiple read_file calls when you need "
        "to understand related files together (e.g. source + test + config). Max 5 files, 30K chars total."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "start_line": {"type": "integer", "description": "First line (1-based, optional)"},
                        "end_line": {"type": "integer", "description": "Last line (1-based, optional)"},
                    },
                    "required": ["file_path"],
                },
                "description": "Files to read (max 5)",
            },
        },
        "required": ["files"],
    },
}

self_review_def = {
    "name": "self_review",
    "description": (
        "Review your own changes before finishing. Returns the diff with a checklist: "
        "(1) Does every edit address the ticket? (2) Are there unintended side effects? "
        "(3) Did you leave debug code? (4) Are imports correct? (5) Did you modify test files? "
        "You SHOULD call this before finish()."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
    },
}

checkpoint_def = {
    "name": "checkpoint",
    "description": (
        "Save a snapshot of all current file changes. Use before trying a risky approach. "
        "If the approach fails, call restore() to revert ALL files to this snapshot. Max 3 checkpoints."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Short label (e.g. 'before_hypothesis_2')"},
        },
        "required": ["label"],
    },
}

restore_def = {
    "name": "restore",
    "description": (
        "Restore all files to a previous checkpoint. Reverts ALL changes made since that checkpoint. "
        "Use when your approach failed and you want a clean slate to try a different fix."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "Checkpoint label to restore to"},
        },
        "required": ["label"],
    },
}


# ── Setup Environment ────────────────────────────────────────────────────────

setup_environment_def = {
    "name": "setup_environment",
    "description": (
        "Auto-detect and install project dependencies so tests can run. "
        "Detects project type from config files (setup.py, pyproject.toml, package.json, "
        "Cargo.toml, go.mod, requirements.txt) and runs the appropriate install command. "
        "Call this when tests fail with import errors or missing modules."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# ── All tools list ───────────────────────────────────────────────────────────

ALL_TOOL_DEFS = [
    # Graph tools
    get_callers_def,
    get_impact_def,
    get_dependencies_def,
    get_test_coverage_def,
    get_coupled_files_def,
    search_symbols_def,
    get_risk_score_def,
    get_reviewers_def,
    # Standard tools
    read_file_def,
    write_file_def,
    search_code_def,
    run_tests_def,
    run_command_def,
    build_check_def,
    get_diff_def,
    finish_def,
    setup_environment_def,
    # Graph tools (new)
    get_top_files_def,
    get_file_info_def,
    get_symbol_details_def,
    get_class_hierarchy_def,
    get_change_context_def,
    # Discovery & navigation
    list_directory_def,
    find_files_def,
    # Reasoning
    think_def,
    # Edit safety
    undo_edit_def,
    checkpoint_def,
    restore_def,
    # File analysis
    file_outline_def,
    # Lint & validation
    lint_check_def,
    classify_test_result_def,
    # Git history
    git_log_def,
    git_blame_def,
    # Batch & review
    batch_read_def,
    self_review_def,
]
