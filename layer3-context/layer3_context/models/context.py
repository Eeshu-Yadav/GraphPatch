from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SymbolContext:
    name: str
    qualified_name: str
    file_path: str
    entity_type: str   # "Function" | "Class"
    summary: str
    centrality: float
    score: float       # retrieval relevance (0-1)
    line_start: int = 0
    docstring: str = ""
    code_snippet: str = ""  # First few lines (signature + docstring), populated during assembly


@dataclass
class FileContext:
    path: str
    language: str
    summary: str
    centrality: float
    score: float
    is_test: bool = False
    lines: int = 0


@dataclass
class CoupledFile:
    path: str
    score: float
    commit_count: int


@dataclass
class ContextBundle:
    ticket_id: str
    repo_id: str
    relevant_symbols: list[SymbolContext]   # top symbols, ranked by RRF
    relevant_files: list[FileContext]        # derived from symbols + file-level search
    call_graph: dict                          # {symbol_name: [{"name": "caller", "file": "path"}]}
    dependencies: dict[str, dict]            # {file_path: {deps: [], dependents: []}}
    test_files: list[str]
    coupled_files: list[CoupledFile]
    token_estimate: int
    strategies_used: list[str]
    test_code_snippets: dict[str, str] = field(default_factory=dict)  # {test_path: first 80 lines}
    impact_summary: dict[str, dict] = field(default_factory=dict)     # {symbol: {total_callers, risk, ...}}

    def to_prompt_text(self, max_symbols: int = 20, max_files: int = 10) -> str:
        """Render bundle as structured markdown text for LLM consumption (Layer 4)."""
        lines = [
            f"# Codebase Context for Ticket: {self.ticket_id}",
            f"**Repository:** `{self.repo_id}`",
            f"**Strategies used:** {', '.join(self.strategies_used)}",
            "",
            "## Relevant Symbols",
        ]
        for idx, s in enumerate(self.relevant_symbols[:max_symbols]):
            lines.append(f"### {s.entity_type}: `{s.name}` — `{s.file_path}`:{s.line_start}")
            if s.code_snippet and idx < 5:
                lines.append(f"```\n{s.code_snippet}\n```")
            elif s.docstring:
                lines.append(f"> {s.docstring[:200]}")
            if s.summary:
                lines.append(f"*{s.summary[:300]}*")

            # Enriched call graph: callers with file paths
            if s.name in self.call_graph and self.call_graph[s.name]:
                callers = self.call_graph[s.name]
                if isinstance(callers[0], dict):
                    caller_strs = [f"`{c['name']}` ({c.get('file', '?')})" for c in callers[:5]]
                else:
                    caller_strs = [f"`{c}`" for c in callers[:5]]
                lines.append(f"**Called by:** {', '.join(caller_strs)}")

            # Impact assessment
            if s.name in self.impact_summary:
                imp = self.impact_summary[s.name]
                risk = imp.get("risk", "unknown")
                total = imp.get("total_callers", 0)
                affected = imp.get("affected_files", 0)
                lines.append(f"**Impact:** {risk} risk — {total} callers across {affected} files")
                will_break = imp.get("will_break", [])
                if will_break:
                    lines.append(f"  Will break: {', '.join(will_break[:3])}")

            lines.append("")

        lines.append("## Relevant Files")
        for f in self.relevant_files[:max_files]:
            test_marker = " [TEST]" if f.is_test else ""
            lines.append(f"- `{f.path}`{test_marker} ({f.language}, centrality={f.centrality:.4f})")
            if f.summary:
                lines.append(f"  {f.summary[:200]}")

        if self.test_files:
            lines.append("\n## Test Coverage")
            for t in self.test_files:
                lines.append(f"- `{t}`")

        if self.test_code_snippets:
            lines.append("\n## Test Expectations (these define what 'correct' means)")
            for tpath, code in list(self.test_code_snippets.items())[:3]:
                lines.append(f"\n### `{tpath}`")
                lines.append(f"```\n{code}\n```")

        if self.coupled_files:
            lines.append("\n## Historically Co-Changed Files")
            for c in self.coupled_files[:5]:
                lines.append(f"- `{c.path}` (coupling={c.score:.2f}, {c.commit_count} commits)")

        return "\n".join(lines)
