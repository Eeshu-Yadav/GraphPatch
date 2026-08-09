"""BlueprintRunner — walks the DAG, executes nodes, handles fallback."""
from __future__ import annotations

import os
import time
import structlog

from minions.engine.blueprint import Blueprint, Node, NodeResult, NodeType
from minions.engine.context import PipelineContext

log = structlog.get_logger(__name__)


class BlueprintRunner:
    """Executes a blueprint by walking its node DAG."""

    def __init__(self, blueprint: Blueprint):
        self.blueprint = blueprint

    def run(self, ctx: PipelineContext) -> PipelineContext:
        """Execute the blueprint. Returns the final PipelineContext."""
        start = time.time()
        log.info("blueprint.start", name=self.blueprint.name, task_type=ctx.task_type,
                 ticket_id=ctx.ticket_id)

        # Apply cost budget from intent complexity
        complexity = ctx.intent.get("complexity", "moderate") if ctx.intent else "moderate"
        from minions.engine.model_router import get_cost_budget
        budget = get_cost_budget(complexity)
        ctx.max_total_tokens = budget["max_tokens"]
        log.info("blueprint.budget", complexity=complexity, max_tokens=ctx.max_total_tokens)

        try:
            ctx = self._execute(ctx)
        except Exception as e:
            log.error("blueprint.crashed", error=str(e), nodes_done=ctx.nodes_executed)
            if ctx.fallback_to_legacy:
                log.info("blueprint.fallback_to_legacy")
                try:
                    ctx = self._run_legacy(ctx)
                except Exception as legacy_err:
                    ctx.error = f"Blueprint failed: {e}; Legacy fallback also failed: {legacy_err}"
                    ctx.success = False
            else:
                ctx.error = str(e)
                ctx.success = False
        finally:
            # Always destroy sandbox — disposable, one per run
            self._cleanup_sandbox(ctx)

        ctx.total_duration = time.time() - start
        log.info("blueprint.done",
                 success=ctx.success,
                 nodes=len(ctx.nodes_executed),
                 tokens=ctx.total_tokens,
                 duration=f"{ctx.total_duration:.1f}s",
                 pr_url=ctx.pr_url or "(none)")
        return ctx

    def _execute(self, ctx: PipelineContext) -> PipelineContext:
        """Walk the DAG node by node."""
        if not self.blueprint.nodes:
            ctx.error = "Blueprint has no nodes"
            ctx.success = False
            return ctx

        current_name = self.blueprint.nodes[0].name
        _budget_applied = False

        while current_name:
            # Apply cost budget from intent complexity (once, after assemble_context fills ctx.intent)
            if not _budget_applied and ctx.intent:
                from minions.engine.model_router import get_cost_budget
                complexity = ctx.intent.get("complexity", "moderate")
                budget = get_cost_budget(complexity)
                ctx.max_total_tokens = budget["max_tokens"]
                log.info("blueprint.budget_applied", complexity=complexity,
                         max_tokens=budget["max_tokens"])
                _budget_applied = True
            node = self.blueprint.get_node(current_name)
            if node is None:
                ctx.error = f"Node '{current_name}' not found in blueprint"
                ctx.success = False
                return ctx

            # Execute the node
            result = self._run_node(node, ctx)

            # Track metrics
            ctx.nodes_executed.append(node.name)
            ctx.tokens_by_node[node.name] = (
                ctx.tokens_by_node.get(node.name, 0) + result.tokens_used
            )
            ctx.time_by_node[node.name] = (
                ctx.time_by_node.get(node.name, 0) + result.duration
            )
            ctx.total_tokens += result.tokens_used

            # Token budget check — hard cap
            if ctx.total_tokens > ctx.max_total_tokens:
                log.warning("blueprint.token_budget_exceeded",
                            used=ctx.total_tokens, limit=ctx.max_total_tokens,
                            node=node.name)
                # If we have changes, try to finish (skip remaining fix loops)
                if ctx.modified_files and ctx.implementation:
                    current_name = self._find_terminal_node(ctx)
                else:
                    ctx.error = f"Token budget exceeded ({ctx.total_tokens:,} > {ctx.max_total_tokens:,})"
                    return ctx
                continue

            # Determine next node
            current_name = self._resolve_next(node, result, ctx)

        if not ctx.error:
            ctx.success = True
        return ctx

    def _run_node(self, node: Node, ctx: PipelineContext) -> NodeResult:
        """Execute a single node with timeout and error handling."""
        node_type_label = node.node_type.value
        log.info("node.start", node=node.name, type=node_type_label,
                 model=node.model or "-")

        start = time.time()
        try:
            result = node.execute(ctx)
        except Exception as e:
            log.error("node.error", node=node.name, error=str(e))
            result = NodeResult(success=False, error=str(e))

        result.duration = time.time() - start

        log.info("node.done", node=node.name, success=result.success,
                 tokens=result.tokens_used, duration=f"{result.duration:.1f}s",
                 next=result.next_node or "default")
        return result

    def _resolve_next(self, node: Node, result: NodeResult, ctx: PipelineContext) -> str | None:
        """Determine which node runs next based on result and edges."""

        # 1. If the node explicitly says where to go (gates do this)
        if result.next_node:
            # Check if target node exists in blueprint (may have been stripped in no-PR mode)
            if self.blueprint.get_node(result.next_node):
                return result.next_node
            else:
                log.info("node.target_missing", target=result.next_node, node=node.name)
                return None  # End pipeline gracefully

        # 2. Check blueprint edges for this node
        edges = self.blueprint.edges.get(node.name, {})

        if result.success and "pass" in edges:
            return edges["pass"]

        if not result.success and "fail" in edges:
            return edges["fail"]

        if not result.success and not edges:
            # No failure route defined — pipeline stops
            ctx.error = result.error or f"Node '{node.name}' failed with no failure route"
            return None

        # 3. Default: next node in sequence
        idx = self.blueprint.get_node_index(node.name)
        if idx >= 0 and idx + 1 < len(self.blueprint.nodes):
            return self.blueprint.nodes[idx + 1].name

        return None  # End of blueprint

    def _find_terminal_node(self, ctx: PipelineContext) -> str | None:
        """Find the create_pr or notify node to skip to when budget exceeded."""
        for name in ("create_pr", "notify"):
            if self.blueprint.get_node(name):
                ctx.draft_pr = True  # Force draft since we skipped validation
                return name
        return None  # No terminal node — just end

    @staticmethod
    def _cleanup_sandbox(ctx: PipelineContext):
        """Destroy sandbox container if one was started. Disposable — one per run."""
        if ctx.sandbox:
            try:
                ctx.sandbox.stop()
                log.info("blueprint.sandbox.destroyed")
            except Exception as e:
                log.warning("blueprint.sandbox.cleanup_failed", error=str(e))
            ctx.sandbox = None

    def _run_legacy(self, ctx: PipelineContext) -> PipelineContext:
        """Fall back to the existing run_pipeline_pr logic."""
        from layer3_context.models.ticket import Ticket
        from layer3_context.assembly.assembler import assemble
        from layer45_agent.agent import run_agent
        from layer45_agent.models import AgentConfig
        from layer6_validator.runner import validate
        from layer7_publisher.publisher import publish

        # Assemble context if not already done
        if ctx.ticket is None:
            ctx.ticket = Ticket(
                ticket_id=ctx.ticket_id, title=ctx.title,
                body=ctx.body, repo_id=ctx.repo_id,
            )
        if ctx.bundle is None:
            ctx.bundle = assemble(ctx.ticket)

        config = AgentConfig(
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
            explore_model=os.environ.get("CLAUDE_EXPLORE_MODEL", "claude-haiku-4-5-20251001"),
            plan_model=os.environ.get("CLAUDE_PLAN_MODEL", "claude-opus-4-6"),
            write_model=os.environ.get("CLAUDE_WRITE_MODEL", "claude-sonnet-4-20250514"),
        )

        agent_result = run_agent(ctx.ticket, ctx.bundle, config)
        if not agent_result.implementation.file_results:
            ctx.error = "Legacy fallback: agent produced no changes"
            ctx.success = False
            return ctx

        impl = agent_result.implementation
        validation = validate(impl)

        token = ctx.github_token or os.environ.get("GITHUB_TOKEN", "")
        pr_result = publish(
            impl=impl,
            ticket_title=ctx.title,
            github_token=token,
            validation_summary=validation.summary(),
            draft=not validation.passed(),
        )

        ctx.pr_url = pr_result.pr_url or ""
        ctx.pr_number = pr_result.pr_number or 0
        ctx.success = pr_result.success
        ctx.total_tokens = agent_result.total_prompt_tokens + agent_result.total_completion_tokens
        return ctx
