"""
JavaScript symbol extraction using Tree-sitter.
Uses a JS-specific query file (class names are `identifier`, not `type_identifier`).
For everything else (imports, calls, exports) the logic is identical to TypeScript.
"""
from __future__ import annotations

from pathlib import Path

from tree_sitter import Language, Node, Query, QueryCursor

from src.models.symbol import FileSymbols
from src.parsing.extractor import node_text
from src.parsing.extractors.typescript import (
    _extract_calls,
    _extract_exports,
    _extract_functions,
    _extract_imports,
    _find_capture_in,
    _is_descendant,
)
from src.models.symbol import Symbol, SymbolKind

_QUERY_CACHE: dict[int, Query] = {}


def _get_query(lang: Language) -> Query:
    lid = id(lang)
    if lid not in _QUERY_CACHE:
        q_path = Path(__file__).parent.parent / "queries" / "javascript.scm"
        _QUERY_CACHE[lid] = lang.query(q_path.read_text())
    return _QUERY_CACHE[lid]


def extract_javascript(root: Node, lang: Language, source: str, result: FileSymbols) -> None:
    raw = QueryCursor(_get_query(lang)).captures(root)
    captures = sorted(
        [(node, cap) for cap, nodes in raw.items() for node in nodes],
        key=lambda x: x[0].start_byte,
    )

    _extract_functions(captures, source, result)
    _extract_js_classes(captures, source, result)
    _extract_imports(captures, source, result)
    _extract_calls(captures, source, result)
    _extract_exports(captures, source, result)


def _extract_js_classes(captures: list, source: str, result: FileSymbols) -> None:
    """JS class names are `identifier` nodes (TS uses `type_identifier`)."""
    seen: set[str] = set()
    for node, cap in captures:
        if cap != "class.def":
            continue
        name_node = _find_capture_in(captures, node, "class.name")
        if not name_node:
            continue
        name = node_text(name_node, source)
        key = f"{name}:{node.start_point[0]}"
        if key in seen:
            continue
        seen.add(key)
        result.symbols.append(Symbol(
            name=name,
            kind=SymbolKind.CLASS,
            file_path=result.path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        ))
