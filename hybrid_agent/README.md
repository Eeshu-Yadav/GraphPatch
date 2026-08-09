# Hybrid Agent — layer45 infrastructure + lean v2 patterns

Separate experiment folder. Does NOT modify layer45-agent.

## What it takes from layer45-agent:
- Sandbox (Docker isolation)
- Triage system (baseline, fingerprinting, blame analysis, phase tracker)
- History compression
- Trace logging
- Prompt caching

## What it takes from lean v2:
- Phase-based tool restriction (6 explore → 7 write)
- Graph-as-context (auto-inject after find_files, read_file, write_file)
- Single conversation (no context loss)
- 13 graph tools REMOVED from tool list → run automatically as context injection

## What's new:
- Tool list switches based on triage.phase (EXPLORING vs WRITING/VERIFYING)
- Graph expansion hooks into existing tool execution
- Falls back to full toolset if phase detection fails
