"""
CSS symbol extraction using Tree-sitter.
Extracts: class selectors (.foo), ID selectors (#foo), and @keyframes names as symbols.
These become searchable units in the graph (useful for tracing style dependencies).
"""
from __future__ import annotations

from tree_sitter import Language, Node

from src.models.symbol import FileSymbols, Symbol, SymbolKind
from src.parsing.extractor import node_text


def extract_css(root: Node, lang: Language, source: str, result: FileSymbols) -> None:
    seen: set[str] = set()
    _walk(root, source, result, seen)


def _walk(node: Node, source: str, result: FileSymbols, seen: set[str]) -> None:
    # class_selector: .foo
    if node.type == "class_selector":
        for child in node.children:
            if child.type == "class_name":
                name = "." + node_text(child, source)
                _add_symbol(name, SymbolKind.VARIABLE, node, result, seen)

    # id_selector: #foo
    elif node.type == "id_selector":
        for child in node.children:
            if child.type == "id_name":
                name = "#" + node_text(child, source)
                _add_symbol(name, SymbolKind.VARIABLE, node, result, seen)

    # @keyframes animation-name { ... }
    elif node.type == "keyframes_statement":
        for child in node.children:
            if child.type == "keyframes_name":
                name = "@keyframes/" + node_text(child, source)
                _add_symbol(name, SymbolKind.FUNCTION, node, result, seen)

    for child in node.children:
        _walk(child, source, result, seen)


def _add_symbol(
    name: str,
    kind: SymbolKind,
    node: Node,
    result: FileSymbols,
    seen: set[str],
) -> None:
    key = f"{name}:{node.start_point[0]}"
    if key in seen:
        return
    seen.add(key)
    result.symbols.append(Symbol(
        name=name,
        kind=kind,
        file_path=result.path,
        line_start=node.start_point[0] + 1,
        line_end=node.end_point[0] + 1,
    ))
