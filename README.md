# GraphPatch

An autonomous ticket-to-PR pipeline that turns an issue ticket into a ready-to-review pull request. It indexes a codebase into a knowledge graph, assembles the right context for the ticket, plans and applies the change with an LLM agent, validates the result, and publishes a PR.

Evaluated on SWE-bench (Django/Astropy issues) with the lean agent reaching a 37.5% resolve rate on the sampled set.

## Architecture

```
ticket ──> layer2-indexer ──> layer3-context ──> layer4-planner ──> layer45-agent ──> layer6-validator ──> layer7-pr-publisher ──> PR
                │                                                        │
          Memgraph + Qdrant                                        code changes
          (code knowledge graph                                    in sandboxed
           + embeddings)                                           repo checkout
```

| Component | What it does |
|---|---|
| `layer2-indexer/` | Parses a repository (AST + embeddings) into a Memgraph knowledge graph and Qdrant vector store |
| `layer3-context/` | Assembles ticket-relevant context from the graph (callers, dependencies, coupled files, test coverage) |
| `layer4-planner/` | Produces a change plan from the assembled context |
| `layer45-agent/` | Executing agent that applies the planned change |
| `layer6-validator/` | Runs tests / checks against the produced diff |
| `layer7-pr-publisher/` | Opens the pull request on GitHub |
| `lean_agent/` | Lean single-conversation agent (6 tools + graph-as-context) — the best-performing variant |
| `hybrid_agent/` | Experiment combining the lean agent with the layered pipeline |
| `claude_code_agent/` | MCP server exposing the graph tools to Claude Code |
| `mcp-server/` | MCP server exposing the full pipeline as tools |
| `minions/` | Task queue, worker nodes, and a Next.js dashboard for monitoring runs |

## Quickstart

Requirements: Python 3.11+, Docker, and API keys for the LLM provider you use.

```bash
# 1. Infrastructure (Memgraph, Qdrant, Redis, Ollama)
docker compose up -d

# 2. Environment
cp .env.example .env   # fill in your API keys

# 3. Install the indexer (each layer has its own venv/pyproject)
cd layer2-indexer && python -m venv .venv && .venv/bin/pip install -e . && cd ..

# 4. Index a repository
.venv/bin/python -m layer2 index /path/to/repo   # see layer2-indexer/scripts

# 5. Run the lean agent on a ticket
python lean_agent/run_v5_single.py
```

## Evaluation

`swe_bench_runner.py` runs the pipeline against SWE-bench issues; `swe_bench_analyze.py` summarizes results. Sample issue fixtures live in `lean_agent/issues/`.

## MCP integration

`.mcp.json` registers two MCP servers for Claude Code:

- **ticket-to-pr** — the full pipeline as tools
- **codebase-graph** — graph queries (impact analysis, coupled files, test coverage, risk scores)

Adjust the absolute paths in `.mcp.json` to your checkout location.
