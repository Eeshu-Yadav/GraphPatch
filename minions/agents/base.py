"""BaseAgent — focused LLM caller with prompt caching, tool memoization, and hard budget.

Token reduction techniques (from existing layer45_agent):
1. Prompt caching — system prompt cached via cache_control: ephemeral
2. Tool result memoization — read_file, search_* cached across calls
3. File content cache — shared across nodes via PipelineContext
4. Model-appropriate sizing — Haiku for explore, Sonnet for write, Opus only for plan (capped)
5. Truncated tool results — cap large outputs to prevent context bloat
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic
import structlog

log = structlog.get_logger(__name__)

# Max chars for tool result content sent back to LLM
_MAX_TOOL_RESULT_CHARS = 8000


@dataclass
class AgentRunResult:
    """Output of a single focused agent run."""
    final_output: str = ""
    reasoning: str = ""
    modified_files: dict[str, str] = field(default_factory=dict)
    original_files: dict[str, str] = field(default_factory=dict)
    tokens_input: int = 0
    tokens_output: int = 0
    tool_calls_made: int = 0
    success: bool = True
    error: str = ""
    files_read: dict[str, str] = field(default_factory=dict)  # path → content summary (for cross-node cache)

    @property
    def total_tokens(self) -> int:
        return self.tokens_input + self.tokens_output


class BaseAgent:
    """Focused agent: one conversation, limited tools, hard budget.

    Each agentic node creates a BaseAgent with:
    - A specific model (Haiku for explore, Sonnet for write, etc.)
    - A specific tool subset (graph tools, write tools, etc.)
    - A hard max_tool_calls budget
    - Prompt caching for system prompt (reduces repeat input cost by 90%)
    - Tool result memoization (same as existing layer45 _result_cache)
    """

    def __init__(
        self,
        model: str,
        tools: list[dict],
        max_tool_calls: int,
        repo_path: Path,
        repo_id: str,
        temperature: float = 0.1,
        max_tokens_per_turn: int = 8192,
    ):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.tools = tools
        self.max_tool_calls = max_tool_calls
        self.repo_path = repo_path
        self.repo_id = repo_id
        self.temperature = temperature
        self.max_tokens_per_turn = max_tokens_per_turn

    def run(
        self,
        prompt: str,
        existing_modifications: dict[str, str] | None = None,
        existing_originals: dict[str, str] | None = None,
    ) -> AgentRunResult:
        """Execute a focused agent conversation with prompt caching."""
        from layer45_agent.tools import execute_tool, clear_tool_cache

        clear_tool_cache()

        # Split prompt into system (cacheable) and user message
        # System prompt is cached across turns — 90% cheaper on repeat reads
        system_blocks = [{
            "type": "text",
            "text": prompt,
            "cache_control": {"type": "ephemeral"},
        }]

        messages: list[dict] = [
            {"role": "user", "content": "Implement the task described in the system prompt. Start now."}
        ]
        modified_files = dict(existing_modifications or {})
        original_files = dict(existing_originals or {})
        files_read: dict[str, str] = {}
        tool_calls_made = 0
        total_input = 0
        total_output = 0
        all_reasoning = []

        log.info("agent.start", model=self.model, max_tools=self.max_tool_calls)

        while tool_calls_made < self.max_tool_calls:
            response = self._call_with_retry(system_blocks, messages)
            if response is None:
                return AgentRunResult(
                    success=False,
                    error="Rate limit exhausted after retries",
                    modified_files=modified_files,
                    original_files=original_files,
                    tokens_input=total_input,
                    tokens_output=total_output,
                    tool_calls_made=tool_calls_made,
                    files_read=files_read,
                )

            if response.usage:
                total_input += response.usage.input_tokens or 0
                total_output += response.usage.output_tokens or 0

            for block in response.content:
                if hasattr(block, "text") and block.text:
                    all_reasoning.append(block.text)

            messages.append({"role": "assistant", "content": response.content})

            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_blocks:
                break

            results = []
            for block in tool_blocks:
                tool_name = block.name
                tool_args = block.input if isinstance(block.input, dict) else {}

                log.debug("agent.tool", tool=tool_name,
                          args_keys=list(tool_args.keys()))

                result = execute_tool(
                    name=tool_name,
                    args=tool_args,
                    repo_path=self.repo_path,
                    repo_id=self.repo_id,
                    modified_files=modified_files,
                    original_files=original_files,
                )

                # Track files read for cross-node cache
                if tool_name == "read_file":
                    fp = tool_args.get("file_path", "")
                    lines = result.get("total_lines", 0) if isinstance(result, dict) else 0
                    if fp and "error" not in (result if isinstance(result, dict) else {}):
                        files_read[fp] = f"{lines} lines"

                # Truncate large tool results to prevent context bloat
                result_str = json.dumps(result, default=str)
                if len(result_str) > _MAX_TOOL_RESULT_CHARS:
                    result_str = result_str[:_MAX_TOOL_RESULT_CHARS] + '..."}'

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })
                tool_calls_made += 1

            messages.append({"role": "user", "content": results})

            if tool_calls_made >= self.max_tool_calls:
                log.info("agent.budget_exhausted", calls=tool_calls_made)
                break

        # Extract final text
        final_text = ""
        if messages and messages[-1]["role"] == "assistant":
            content = messages[-1]["content"]
            if isinstance(content, list):
                for block in content:
                    if hasattr(block, "text") and block.text:
                        final_text += block.text
            elif isinstance(content, str):
                final_text = content

        log.info("agent.done", model=self.model, tools_used=tool_calls_made,
                 input_tokens=total_input, output_tokens=total_output,
                 cache_read=getattr(response.usage, 'cache_read_input_tokens', 0) if response and response.usage else 0)

        return AgentRunResult(
            final_output=final_text,
            reasoning="\n".join(all_reasoning),
            modified_files=modified_files,
            original_files=original_files,
            tokens_input=total_input,
            tokens_output=total_output,
            tool_calls_made=tool_calls_made,
            files_read=files_read,
        )

    def _call_with_retry(self, system_blocks: list[dict], messages: list[dict], max_retries: int = 3):
        """Call Claude API with prompt caching and exponential backoff."""
        for attempt in range(max_retries + 1):
            try:
                return self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens_per_turn,
                    system=system_blocks,
                    tools=self.tools,
                    messages=messages,
                    temperature=self.temperature,
                )
            except (anthropic.RateLimitError, anthropic.InternalServerError) as e:
                if attempt == max_retries:
                    log.error("agent.rate_limit_exhausted", attempts=max_retries)
                    return None
                wait = 30.0 * (attempt + 1)
                log.warning("agent.rate_limited", attempt=attempt + 1, wait=f"{wait:.0f}s",
                            error=str(e)[:100])
                time.sleep(wait)
            except anthropic.APIError as e:
                if e.status_code in (429, 529, 503) and attempt < max_retries:
                    wait = 30.0 * (attempt + 1)
                    log.warning("agent.api_retry", status=e.status_code, wait=f"{wait:.0f}s")
                    time.sleep(wait)
                else:
                    raise
        return None
