"""
Shell/Bash symbol extraction using Tree-sitter.
Extracts: function definitions.
Shell scripts are common in CI/CD pipelines and build tooling.
"""
from __future__ import annotations

from tree_sitter import Language, Node

from src.models.symbol import FileSymbols, Symbol, SymbolKind
from src.parsing.extractor import node_text


def extract_shell(root: Node, lang: Language, source: str, result: FileSymbols) -> None:
    seen: set[str] = set()
    _walk(root, source, result, seen)


def _walk(node: Node, source: str, result: FileSymbols, seen: set[str]) -> None:
    # function_definition: function foo() { ... }  OR  foo() { ... }
    if node.type == "function_definition":
        name_node = node.child_by_field_name("name")
        if name_node:
            name = node_text(name_node, source)
            key = f"{name}:{node.start_point[0]}"
            if key not in seen:
                seen.add(key)
                result.symbols.append(Symbol(
                    name=name,
                    kind=SymbolKind.FUNCTION,
                    file_path=result.path,
                    line_start=node.start_point[0] + 1,
                    line_end=node.end_point[0] + 1,
                ))

    for child in node.children:
        _walk(child, source, result, seen)
