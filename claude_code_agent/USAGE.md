# Claude Code Agent — Usage Guide

## What This Is

A Claude Code-native approach to fixing GitHub issues using a **code knowledge graph** (Memgraph + Qdrant). Instead of a custom Python ReAct loop calling the Claude API, Claude Code itself is the agent — it uses its native tools (Read, Edit, Bash, Grep, Glob) for file operations and our MCP tools for graph-powered analysis.

## Prerequisites

### 1. Infrastructure (docker-compose)

```bash
cd /home/eeshu/Desktop/context
docker compose up -d   # Starts: Memgraph, Qdrant, Redis, Ollama
```

Verify all services are running:
```bash
# From any Claude Code session, the MCP tool will check:
# Call: codebase-graph__get_pipeline_status
```

### 2. Index the Target Repo (one-time)

The repo must be indexed before graph tools work. If already indexed, skip this.

```bash
# From Claude Code, call:
# codebase-graph__index_repo(repo_url="https://github.com/django/django", repo_id="django/django")
```

Or manually:
```bash
cd /home/eeshu/Desktop/context/layer2-indexer
.venv/bin/python -m src.cli index --repo https://github.com/django/django --id django/django --sync --skip-descriptions
```

### 3. MCP Server Auto-Starts

The `.mcp.json` at `/home/eeshu/Desktop/context/.mcp.json` registers the `codebase-graph` MCP server. Claude Code auto-starts it when you open a session in this directory.

---

## Running /fix-ticket

### Step 1: Open a NEW Claude Code Session

Open Claude Code with the working directory set to the **target repo**:

```bash
cd /home/eeshu/Desktop/context/repos/django_django
claude
```

Or open it in VS Code with the Claude Code extension, with the workspace set to the repo.

**Important:** The `.mcp.json` is at `/home/eeshu/Desktop/context/.mcp.json`. Claude Code needs to find it. Either:
- Open Claude Code from `/home/eeshu/Desktop/context` (it finds `.mcp.json` automatically)
- Or set it as an additional working directory

### Step 2: Invoke the Skill

Type in Claude Code:

```
/fix-ticket django/django django-14053 "HashedFilesMixin's post_process() yields multiple times for the same file" "When using ManifestStaticFilesStorage or CachedStaticFilesStorage with collectstatic, the post_process() method yields the same original filename multiple times instead of just once per file. This happens because the implementation performs multiple passes to handle nested references between files, but yields results after each pass instead of collecting them and yielding once per original file."
```

Or shorter — just paste the issue:

```
/fix-ticket Fix django/django issue django-14053: HashedFilesMixin's post_process() yields multiple times for the same file. The post_process method should yield each file only once, after all passes are complete.
```

### Step 3: What Happens Automatically

The `/fix-ticket` skill guides Claude Code through 6 phases:

1. **Session tracing** — calls `start_session` to record all graph tool calls
2. **Context assembly** — calls `assemble_context` + `build_graph_context` to get pre-computed context (relevant files, call graph, dependencies, tests, coupling, risk)
3. **Reproduce** — runs the failing test to see the actual error
4. **Explore** — uses native Grep/Glob/Read + graph MCP tools if needed
5. **Write** — TDD: write reproduction test → fix → verify
6. **PR** — git branch, commit, push, `gh pr create`
7. **Finish session** — writes trace to `claude_code_agent/traces/`

### Step 4: Monitor from Another Session (optional)

From a SEPARATE Claude Code session:

```
# Check what the fixing session is doing right now:
# Call: codebase-graph__get_session_status

# List past completed traces:
# Call: codebase-graph__list_session_traces

# Read a specific trace in detail:
# Call: codebase-graph__read_session_trace(filename="20260416_193045_django-14053.json")
```

---

## Available Issue Files

Pre-written issue descriptions for testing, in `lean_agent/issues/`:

| File | Issue | Description |
|------|-------|-------------|
| `django-14011.md` | django-14011 | — |
| `django-14034.md` | django-14034 | — |
| `django-14053.md` | django-14053 | HashedFilesMixin post_process duplicate yields |
| `django-14140.md` | django-14140 | — |
| `django-14155.md` | django-14155 | — |

Each file has this format:
```
repo_id: django/django
repo_path: repos/django_django
ticket_id: django-14053

**Title:** ...
Description: ...
```

To use one, just read it and pass the title + body to `/fix-ticket`.

---

## MCP Tools Reference

### Context Assembly (call first)

| Tool | What | When |
|------|------|------|
| `assemble_context(repo_id, title, body)` | Full multi-strategy retrieval with RRF fusion. Returns markdown context map. | Always — replaces 20+ turns of exploration |
| `build_graph_context(repo_id, title, body)` | Pure graph keyword lookup (no LLM). Fast. | Always — supplements assemble_context |

### Graph Deep-Dives (call as needed)

| Tool | What | When |
|------|------|------|
| `get_change_context(file_path, repo_id, symbol_name?)` | Composite: risk + deps + tests + coupling + impact | Before modifying ANY file |
| `search_symbols(query, repo_id)` | Semantic vector search | When context didn't find what you need |
| `get_callers(symbol_name, repo_id)` | Who calls this function | Before renaming or signature change |
| `get_impact(symbol_name, repo_id)` | will_break + may_break | Before signature change |
| `get_dependencies(file_path, repo_id)` | Import graph | Before adding imports |
| `get_test_coverage(file_path, repo_id)` | Which tests cover this file | To know what tests to run |
| `get_coupled_files(file_path, repo_id)` | Git co-change history | Before multi-file changes |
| `get_risk_score(file_path, repo_id)` | Centrality x dependents x test penalty | To prioritize caution |
| `get_class_hierarchy(class_name, repo_id)` | Parents + children + methods | Before modifying a class |

### Session Tracing

| Tool | What | When |
|------|------|------|
| `start_session(repo_id, title, ticket_id)` | Begin tracing | Start of /fix-ticket |
| `finish_session(success, files_changed, pr_url, error)` | Write trace to disk | End of /fix-ticket |
| `get_session_status()` | Live status of in-progress session | From monitoring session |
| `list_session_traces(limit)` | List past traces | From monitoring session |
| `read_session_trace(filename)` | Full trace details | From monitoring session |

### Infrastructure

| Tool | What | When |
|------|------|------|
| `index_repo(repo_url, repo_id)` | Clone + index a new repo | Before first use on a repo |
| `get_pipeline_status()` | Health check all services | Troubleshooting |

---

## Comparing Results

After running `/fix-ticket` on an issue, compare with the lean agent's results:

1. **Trace files** are in `claude_code_agent/traces/*.json`
2. **Lean agent traces** are in `traces/*.json` (the old location)
3. Compare: tool calls, wall time, files changed, success

Key metrics to compare:
- Did it find the right files? (check `files_discovered` in trace)
- How many graph tool calls? (should be 2-5, not 20+)
- Wall time?
- Did it create a working fix? (run the tests manually to verify)

---

## Troubleshooting

### MCP server doesn't start
```
# Check .mcp.json exists:
cat /home/eeshu/Desktop/context/.mcp.json

# Test manually:
cd /home/eeshu/Desktop/context
PYTHONPATH=".:layer2-indexer:layer2-indexer/src:layer3-context:layer4-planner:layer45-agent:lean_agent:mcp-server" \
  .venv/bin/python -m claude_code_agent.server
```

### Graph tools return errors
- Check Memgraph is running: `nc -z localhost 7687`
- Check Qdrant is running: `curl http://localhost:6333`
- Check repo is indexed: look for files in Memgraph

### /fix-ticket not found
Custom commands need `.claude/commands/fix-ticket.md` to exist in the project root.
The file is at `/home/eeshu/Desktop/context/.claude/commands/fix-ticket.md`.

### Python import errors
The `codebase-graph` MCP server needs all layer packages on PYTHONPATH. Check `.mcp.json`
has the correct paths. The layer2-indexer has its own venv with `gqlalchemy`, `mgclient`, etc.
The root `.venv` may be missing some dependencies — install them:
```bash
cd /home/eeshu/Desktop/context
.venv/bin/pip install gqlalchemy tenacity pydantic-settings structlog anthropic
```
