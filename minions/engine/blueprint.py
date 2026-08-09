"""Blueprint definitions — the DAG of deterministic + agentic + gate nodes."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from minions.engine.context import PipelineContext


class NodeType(Enum):
    DETERMINISTIC = "D"  # Pure code, zero tokens, same input → same output
    AGENTIC = "A"        # LLM reasoning + tools, fresh conversation per call
    GATE = "G"           # Pass/fail routing decision


@dataclass
class NodeResult:
    """Returned by every node after execution."""
    success: bool
    next_node: str | None = None   # Gates use this to route to a specific node
    error: str = ""
    tokens_used: int = 0
    duration: float = 0.0


@dataclass
class Node:
    """Single step in a blueprint."""
    name: str                                               # Unique ID
    node_type: NodeType
    execute: Callable[['PipelineContext'], NodeResult]
    description: str = ""
    model: str | None = None                                # For [A] nodes
    max_tool_calls: int | None = None                       # Budget for [A] nodes
    timeout_seconds: int = 300                              # Hard timeout per node


@dataclass
class Blueprint:
    """A named sequence of nodes with routing edges."""
    name: str                                # "bug_fix", "feature", etc.
    description: str = ""
    nodes: list[Node] = field(default_factory=list)
    edges: dict[str, dict[str, str]] = field(default_factory=dict)
    # edges example:
    # {
    #   "lint_gate":   {"pass": "run_tests",   "fail": "fix_lint"},
    #   "test_gate":   {"pass": "build_check", "fail": "apply_autofixes",
    #                   "exhausted": "escalate"},
    # }

    def get_node(self, name: str) -> Node | None:
        for n in self.nodes:
            if n.name == name:
                return n
        return None

    def get_node_index(self, name: str) -> int:
        for i, n in enumerate(self.nodes):
            if n.name == name:
                return i
        return -1
