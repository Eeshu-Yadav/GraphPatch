"""
TypeScript-specific symbol extraction using Tree-sitter.
Handles: functions, classes, methods, imports, calls, decorators, exports.
"""
from __future__ import annotations

from pathlib import Path

from tree_sitter import Language, Node, Query, QueryCursor

from src.models.symbol import (
    Call, Decorator, FileSymbols, Import, ImportedName, Symbol, SymbolKind,
)
from src.parsing.extractor import node_text

_QUERY_CACHE: dict[int, dict[str, Query]] = {}


def _queries(lang: Language) -> dict[str, Query]:
    lid = id(lang)
    if lid not in _QUERY_CACHE:
        q_dir = Path(__file__).parent.parent / "queries"
        _QUERY_CACHE[lid] = {
            "all": lang.query((q_dir / "typescript.scm").read_text()),
        }
    return _QUERY_CACHE[lid]


def extract_typescript(root: Node, lang: Language, source: str, result: FileSymbols) -> None:
    queries = _queries(lang)
    raw = QueryCursor(queries["all"]).captures(root)
    # tree-sitter 0.25+ returns dict[str, list[Node]]; flatten to list[(Node, str)]
    captures = sorted(
        [(node, cap) for cap, nodes in raw.items() for node in nodes],
        key=lambda x: x[0].start_byte,
    )

    _extract_functions(captures, source, result)
    _extract_classes(captures, source, result)
    _extract_imports(captures, source, result)
    _extract_calls(captures, source, result)
    _extract_exports(captures, source, result)
    _extract_decorators(captures, source, result)


def _extract_functions(captures: list, source: str, result: FileSymbols) -> None:
    seen: set[str] = set()

    for node, cap in captures:
        if cap not in ("fn.def", "fn.expr.def", "fn.arrow.def", "fn.async.arrow.def"):
            continue
        name_node = _find_capture_in(captures, node, "fn.name")
        if not name_node:
            continue
        name = node_text(name_node, source)
        key = f"{name}:{node.start_point[0]}"
        if key in seen:
            continue
        seen.add(key)

        is_async = any(
            node_text(n, source) == "async"
            for n, c in captures
            if c == "fn.async" and _is_descendant(n, node)
        )

        sym = Symbol(
            name=name,
            kind=SymbolKind.FUNCTION,
            file_path=result.path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            is_async=is_async,
        )
        result.symbols.append(sym)

    # Methods inside classes
    for node, cap in captures:
        if cap != "method.def":
            continue
        name_node = _find_capture_in(captures, node, "method.name")
        if not name_node:
            continue
        name = node_text(name_node, source)
        key = f"{name}:{node.start_point[0]}"
        if key in seen:
            continue
        seen.add(key)

        # Try to find enclosing class name for qualified_name
        enclosing_class = _find_enclosing_class(captures, source, node)

        sym = Symbol(
            name=name,
            kind=SymbolKind.FUNCTION,
            file_path=result.path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            qualified_name=f"{enclosing_class}.{name}" if enclosing_class else name,
        )
        result.symbols.append(sym)


def _extract_classes(captures: list, source: str, result: FileSymbols) -> None:
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

        # Extract extends clause from @class.extends capture
        bases = []
        extends_node = _find_capture_in(captures, node, "class.extends")
        if extends_node:
            bases.append(node_text(extends_node, source))

        sym = Symbol(
            name=name,
            kind=SymbolKind.CLASS,
            file_path=result.path,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            bases=bases,
        )
        result.symbols.append(sym)


def _extract_imports(captures: list, source: str, result: FileSymbols) -> None:
    seen: set[int] = set()

    # Named imports: import { X, Y } from './module'
    for node, cap in captures:
        if cap != "import.named.stmt" or node.start_byte in seen:
            continue
        seen.add(node.start_byte)
        source_node = _find_capture_in(captures, node, "import.source")
        if not source_node:
            continue
        raw_path = node_text(source_node, source).strip("'\"`")
        imp = Import(raw_path=raw_path, line=node.start_point[0] + 1,
                     is_relative=raw_path.startswith("."))
        for n, c in captures:
            if c == "import.name" and _is_descendant(n, node):
                orig = node_text(n, source)
                # Check for alias
                alias_nodes = [an for an, ac in captures if ac == "import.alias" and _is_descendant(an, node)]
                alias = node_text(alias_nodes[0], source) if alias_nodes else ""
                imp.names.append(ImportedName(name=alias or orig, original=orig, alias=alias))
        result.imports.append(imp)

    # Default import: import Foo from './module'
    for node, cap in captures:
        if cap != "import.default.stmt" or node.start_byte in seen:
            continue
        seen.add(node.start_byte)
        source_node = _find_capture_in(captures, node, "import.source")
        default_node = _find_capture_in(captures, node, "import.default")
        if source_node and default_node:
            raw_path = node_text(source_node, source).strip("'\"`")
            imp = Import(raw_path=raw_path, line=node.start_point[0] + 1,
                         is_relative=raw_path.startswith("."))
            orig = node_text(default_node, source)
            imp.names.append(ImportedName(name=orig, original="default"))
            result.imports.append(imp)

    # Namespace import: import * as Ns from './module'
    for node, cap in captures:
        if cap != "import.namespace.stmt" or node.start_byte in seen:
            continue
        seen.add(node.start_byte)
        source_node = _find_capture_in(captures, node, "import.source")
        if source_node:
            raw_path = node_text(source_node, source).strip("'\"`")
            result.imports.append(Import(
                raw_path=raw_path,
                line=node.start_point[0] + 1,
                is_relative=raw_path.startswith("."),
            ))

    # require() calls
    for node, cap in captures:
        if cap != "require.call" or node.start_byte in seen:
            continue
        seen.add(node.start_byte)
        path_node = _find_capture_in(captures, node, "require.path")
        if path_node:
            raw_path = node_text(path_node, source).strip("'\"`")
            result.imports.append(Import(
                raw_path=raw_path,
                line=node.start_point[0] + 1,
                is_relative=raw_path.startswith("."),
            ))


def _extract_calls(captures: list, source: str, result: FileSymbols) -> None:
    for node, cap in captures:
        if cap == "call.expr":
            name_node = _find_capture_in(captures, node, "call.name")
            if name_node:
                result.calls.append(Call(
                    caller_name="",
                    callee_name=node_text(name_node, source),
                    line=node.start_point[0] + 1,
                ))
        elif cap == "call.method.expr":
            method_node = _find_capture_in(captures, node, "call.method")
            if method_node:
                result.calls.append(Call(
                    caller_name="",
                    callee_name=node_text(method_node, source),
                    line=node.start_point[0] + 1,
                    is_method=True,
                ))
        elif cap == "new.expr":
            cls_node = _find_capture_in(captures, node, "new.class")
            if cls_node:
                result.calls.append(Call(
                    caller_name="",
                    callee_name=node_text(cls_node, source),
                    line=node.start_point[0] + 1,
                ))


def _extract_exports(captures: list, source: str, result: FileSymbols) -> None:
    """Mark symbols as is_exported based on export statements."""
    exported_names: set[str] = set()

    for node, cap in captures:
        if cap == "export.fn.name":
            exported_names.add(node_text(node, source))
        elif cap == "export.class.name":
            exported_names.add(node_text(node, source))
        elif cap == "export.var.name":
            exported_names.add(node_text(node, source))
        elif cap == "export.name":
            exported_names.add(node_text(node, source))

    for sym in result.symbols:
        if sym.name in exported_names:
            sym.is_exported = True


def _extract_decorators(captures: list, source: str, result: FileSymbols) -> None:
    for node, cap in captures:
        if cap in ("decorator", "decorator.simple"):
            name_node = _find_capture_in(captures, node, "decorator.name")
            if name_node:
                result.decorators.append(Decorator(
                    decorator_name=node_text(name_node, source),
                    target_name="",  # TS decorators attach to next sibling — resolved post-parse
                ))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _find_capture_in(captures: list, parent: Node, cap_name: str) -> Node | None:
    for node, name in captures:
        if name == cap_name and _is_descendant(node, parent):
            return node
    return None


def _is_descendant(node: Node, ancestor: Node) -> bool:
    return (
        node.start_byte >= ancestor.start_byte
        and node.end_byte <= ancestor.end_byte
    )


def _find_enclosing_class(captures: list, source: str, fn_node: Node) -> str:
    """Find the name of the class that contains fn_node, if any."""
    for node, cap in captures:
        if cap == "class.def" and _is_descendant(fn_node, node):
            name_node = _find_capture_in(captures, node, "class.name")
            if name_node:
                return node_text(name_node, source)
    return ""
