"""
Hybrid Agent — layer45 infrastructure + lean v2 patterns.

This is a WRAPPER around layer45-agent's run_agent. It:
1. Builds graph context BEFORE the agent starts (Phase 0)
2. Patches tool filtering to restrict by phase (6 explore → 7 write)
3. Patches tool execution to auto-inject graph context
4. Everything else (sandbox, triage, traces, compression) stays from layer45

Does NOT modify layer45-agent code. Uses monkey-patching to intercept
tool selection and execution.
"""
from __future__ import annotations

import os
import structlog

from layer3_context.models.ticket import Ticket
from layer3_context.models.context import ContextBundle
from layer45_agent.models import AgentConfig, AgentResult
from layer45_agent import agent as l45_agent
from layer45_agent.tool_defs import ALL_TOOL_DEFS

from hybrid_agent.tool_filter import filter_tools_for_phase, GRAPH_TOOL_NAMES
from hybrid_agent.graph_injection import inject_graph_context

log = structlog.get_logger(__name__)


def run_hybrid_agent(
    ticket: Ticket,
    bundle: ContextBundle,
    config: AgentConfig,
    feedback: str | None = None,
    feedback_images: list[dict] | None = None,
) -> AgentResult:
    """
    Run the hybrid agent: layer45 core + lean v2 tool restriction + auto graph injection.

    Steps:
    1. Build graph context and prepend to system prompt
    2. Monkey-patch ALL_TOOL_DEFS to be phase-filtered
    3. Monkey-patch execute_tool to auto-inject graph context
    4. Call layer45's run_agent (all infrastructure intact)
    5. Restore patches
    """
    # ── Phase 0: Build graph context ──────────────────────────────────
    from lean_agent.graph_context import build_graph_context
    graph_ctx = build_graph_context(ticket.title, ticket.body, ticket.repo_id)
    log.info("hybrid.graph_context", chars=len(graph_ctx))

    # ── Patch 1: Inject graph context into system prompt builder ──────
    original_build_prompt = l45_agent.build_system_prompt.__wrapped__ if hasattr(l45_agent.build_system_prompt, '__wrapped__') else None

    # We'll inject graph context by modifying the bundle's prompt text
    # The cleanest way: append to the ticket body (it goes into the system prompt)
    original_body = ticket.body
    if graph_ctx:
        ticket.body = f"{original_body}\n\n## Pre-computed Graph Context\n{graph_ctx}"

    # ── Patch 2: Filter tools by phase ────────────────────────────────
    # Store original ALL_TOOL_DEFS and replace with a dynamic version
    import layer45_agent.tool_defs as td_module
    original_tool_defs = td_module.ALL_TOOL_DEFS

    # Remove graph tools from the tool list entirely
    filtered_base = [t for t in original_tool_defs if t.get("name", "") not in GRAPH_TOOL_NAMES]
    td_module.ALL_TOOL_DEFS = filtered_base
    log.info("hybrid.tools_filtered",
             original=len(original_tool_defs),
             filtered=len(filtered_base),
             removed=len(original_tool_defs) - len(filtered_base))

    # ── Patch 3: Auto-inject graph context after tool execution ───────
    from layer45_agent import tools as tools_module
    original_execute = tools_module.execute_tool

    def patched_execute_tool(name, args, repo_path, repo_id, modified_files, original_files, sandbox=None):
        result = original_execute(name, args, repo_path, repo_id, modified_files, original_files, sandbox)
        # Auto-inject graph context
        result = inject_graph_context(name, args, result, repo_id)
        return result

    tools_module.execute_tool = patched_execute_tool

    # ── Run the agent ─────────────────────────────────────────────────
    try:
        result = l45_agent.run_agent(
            ticket=ticket,
            bundle=bundle,
            config=config,
            feedback=feedback,
            feedback_images=feedback_images,
        )
    finally:
        # ── Restore everything ────────────────────────────────────────
        ticket.body = original_body
        td_module.ALL_TOOL_DEFS = original_tool_defs
        tools_module.execute_tool = original_execute
        log.info("hybrid.patches_restored")

    return result
