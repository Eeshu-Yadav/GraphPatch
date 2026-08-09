# Lean Agent — Claude Code's Approach

Experimental agent that mimics how Claude Code finds and fixes code:
- **6 tools during exploration** (not 35)
- **Glob + Grep + Read** (not vector search)
- **Graph expansion is automatic** (not agent-driven)
- **Agentless localization seed** → graph expansion → agent writes

## Architecture

```
Ticket
  ↓
Phase 1: SEED (1 LLM call, no tools)
  "Which files are likely involved?" → seed files
  ↓
Phase 2: EXPAND (0 LLM calls, graph only)
  get_dependencies(seed) → importers + imports
  get_coupled_files(seed) → co-change files
  get_callers(symbols) → call graph neighbors
  → full target file set
  ↓
Phase 3: LOAD (0 LLM calls)
  batch_read(all target files)
  file_outline(each)
  → all code loaded into context
  ↓
Phase 4: PLAN (1 LLM call, no tools)
  "Given these files and code, what changes are needed?"
  → structured edit plan
  ↓
Phase 5: EXECUTE (agent loop, 6 tools)
  Tools: read_file, write_file, search_code, run_command, get_diff, finish
  Agent writes code, tests, iterates
  Starts with plan — no exploration needed
```

## Key Differences from layer45-agent

| | layer45-agent | lean-agent |
|--|--------------|------------|
| Tools during exploration | 35 | 0 (no exploration phase) |
| Tools during writing | 35 | 6 |
| Localization method | Agent explores (97 turns) | Seed + Graph expand (0 turns) |
| Vector search | Primary method | Not used |
| Graph tools | Agent calls manually | System calls automatically |
| Expected turns | 100+ | 15-25 |
