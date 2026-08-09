"""
Python-specific symbol extraction using Tree-sitter.
Handles: functions, classes, imports, calls, decorators.
"""
from __future__ import annotations

from pathlib import Path

from tree_sitter import Language, Node, Query, QueryCursor

from src.models.symbol import (
    Call, Decorator, FileSymbols, Import, ImportedName, Symbol, SymbolKind,
)
from src.parsing.extractor import node_text

# Queries are compiled once per call (language object passed in)
_QUERY_CACHE: dict[int, dict[str, Query]] = {}


def _queries(lang: Language) -> dict[str, Query]:
    lid = id(lang)
    if lid not in _QUERY_CACHE:
        q_dir = Path(__file__).parent.parent / "queries"
        _QUERY_CACHE[lid] = {
            "all": lang.query((q_dir / "python.scm").read_text()),
        }
    return _QUERY_CACHE[lid]


def extract_python(root: Node, lang: Language, source: str, result: FileSymbols) -> None:
    queries = _queries(lang)
    raw = QueryCursor(queries["all"]).captures(root)
    # tree-sitter 0.25+ returns dict[str, list[Node]]; flatten to list[(Node, str)]
    captures = sorted(
        [(node, cap) for cap, nodes in raw.items() for node in nodes],
        key=lambda x: x[0].start_byte,
    )

    # ── Track which symbols we've already seen (by name+line) to avoid dups ──
    seen_fns: set[str] = set()
    seen_classes: set[str] = set()

    # ── Functions ──────────────────────────────────────────────────────────────
    fn_nodes = [(n, c) for n, c in captures if c == "fn.def"]
    for fn_node, _ in fn_nodes:
        # find name node among children
        name_node = fn_node.child_by_field_name("name")
        if not name_node:
            continue
        name = node_text(name_node, source)
        key = f"{name}:{fn_node.start_point[0]}"
        if key in seen_fns:
            continue
        seen_fns.add(key)

        is_async = any(
            child.type == "async" for child in fn_node.children
        )

        # docstring: first statement in body if it's a string
        docstring = _extract_docstring(fn_node, source)

        sym = Symbol(
            name=name,
            kind=SymbolKind.FUNCTION,
            file_path=result.path,
            line_start=fn_node.start_point[0] + 1,
            line_end=fn_node.end_point[0] + 1,
            is_async=is_async,
            docstring=docstring,
        )
        result.symbols.append(sym)

    # ── Classes ────────────────────────────────────────────────────────────────
    class_nodes = [(n, c) for n, c in captures if c == "class.def"]
    for cls_node, _ in class_nodes:
        name_node = cls_node.child_by_field_name("name")
        if not name_node:
            continue
        name = node_text(name_node, source)
        key = f"{name}:{cls_node.start_point[0]}"
        if key in seen_classes:
            continue
        seen_classes.add(key)

        docstring = _extract_docstring(cls_node, source)

        # Extract base classes from @class.bases (argument_list node)
        bases = []
        bases_node = _find_child_capture(captures, cls_node, "class.bases")
        if bases_node:
            for child in bases_node.children:
                if child.type == "identifier":
                    bases.append(node_text(child, source))
                elif child.type == "attribute":
                    # e.g. module.ClassName
                    bases.append(node_text(child, source))

        sym = Symbol(
            name=name,
            kind=SymbolKind.CLASS,
            file_path=result.path,
            line_start=cls_node.start_point[0] + 1,
            line_end=cls_node.end_point[0] + 1,
            docstring=docstring,
            bases=bases,
        )
        result.symbols.append(sym)

    # ── Class-level attributes ───────────────────────────────────────────────
    # Captures: class Foo: bar = SomeType(...)
    # These define the class API — descriptors, type hints, configuration
    # Build a lookup: line → class name (for qualifying class attributes)
    class_line_ranges = []
    for cls_sym in result.symbols:
        if cls_sym.kind == SymbolKind.CLASS:
            class_line_ranges.append((cls_sym.line_start, cls_sym.line_end, cls_sym.name))

    classattr_defs = [(n, c) for n, c in captures if c == "classattr.def"]
    for attr_node, _ in classattr_defs:
        name_node = _find_child_capture(captures, attr_node, "classattr.name")
        value_node = _find_child_capture(captures, attr_node, "classattr.value")
        if not name_node:
            continue

        attr_name = node_text(name_node, source)
        # Skip dunder attributes and private — they're internal details
        if attr_name.startswith("_"):
            continue

        # Find which class contains this attribute by line range
        attr_line = attr_node.start_point[0] + 1
        class_name = ""
        for cls_start, cls_end, cls_name in class_line_ranges:
            if cls_start <= attr_line <= cls_end:
                class_name = cls_name
                break

        # Extract the type/value as a short string for context
        value_text = node_text(value_node, source)[:100] if value_node else ""

        sym = Symbol(
            name=attr_name,
            kind=SymbolKind.VARIABLE,
            file_path=result.path,
            line_start=attr_line,
            line_end=attr_node.end_point[0] + 1,
            qualified_name=f"{class_name}.{attr_name}" if class_name else attr_name,
            docstring=value_text,  # store value expression as docstring for discoverability
        )
        result.symbols.append(sym)

    # ── Decorators ─────────────────────────────────────────────────────────────
    decorated_defs = [(n, c) for n, c in captures if c == "decorated.def"]
    for dec_node, _ in decorated_defs:
        dec_name_node = _find_child_capture(captures, dec_node, "decorator.name")
        decorated_name_node = _find_child_capture(captures, dec_node, "decorated.name")
        if not dec_name_node or not decorated_name_node:
            continue
        result.decorators.append(Decorator(
            decorator_name=node_text(dec_name_node, source),
            target_name=node_text(decorated_name_node, source),
        ))

    # ── Imports ────────────────────────────────────────────────────────────────
    _extract_imports(captures, source, result)

    # ── Calls ─────────────────────────────────────────────────────────────────
    _extract_calls(captures, source, result)

    # ── Resolve caller_name: find which symbol contains each call's line ───────
    # Sort symbols by line_start descending so inner functions match before outer
    sorted_syms = sorted(result.symbols, key=lambda s: s.line_start, reverse=True)
    for call in result.calls:
        for sym in sorted_syms:
            if sym.line_start <= call.line <= sym.line_end:
                call.caller_name = sym.name
                break

    # Drop calls with no resolved caller (module-level noise) or calling itself
    result.calls = [c for c in result.calls if c.caller_name and c.caller_name != c.callee_name]

    # ── Mark exports (functions/classes defined at module level with __all__) ──
    _mark_exports(captures, source, result)


def _extract_docstring(node: Node, source: str) -> str:
    """Extract docstring from first body statement if it's a string literal."""
    body = node.child_by_field_name("body")
    if not body:
        return ""
    for child in body.children:
        if child.type == "expression_statement":
            for grandchild in child.children:
                if grandchild.type in ("string", "concatenated_string"):
                    raw = node_text(grandchild, source).strip("'\"").strip('"""').strip("'''")
                    return raw[:500]  # cap docstring length
    return ""


def _find_child_capture(captures: list, parent: Node, cap_name: str) -> Node | None:
    """Find a capture that is a descendant of parent node."""
    for node, name in captures:
        if name == cap_name and _is_descendant(node, parent):
            return node
    return None


def _is_descendant(node: Node, ancestor: Node) -> bool:
    return (
        node.start_byte >= ancestor.start_byte
        and node.end_byte <= ancestor.end_byte
    )


def _extract_imports(captures: list, source: str, result: FileSymbols) -> None:
    # Simple: import os
    for node, cap in captures:
        if cap == "import.stmt":
            mod_node = _find_child_capture(captures, node, "import.module")
            if mod_node:
                result.imports.append(Import(
                    raw_path=node_text(mod_node, source),
                    line=node.start_point[0] + 1,
                ))

    # from x import y / from x import y as z
    seen_from: set[int] = set()
    for node, cap in captures:
        if cap in ("import.from.stmt", "import.from.aliased.stmt") and node.start_byte not in seen_from:
            seen_from.add(node.start_byte)
            mod_node = _find_child_capture(captures, node, "import.from.module")
            name_node = _find_child_capture(captures, node, "import.from.name")
            alias_node = _find_child_capture(captures, node, "import.from.alias")
            if not mod_node:
                continue
            raw = node_text(mod_node, source)
            imp = Import(raw_path=raw, line=node.start_point[0] + 1)
            if name_node:
                orig = node_text(name_node, source)
                alias = node_text(alias_node, source) if alias_node else ""
                imp.names.append(ImportedName(name=alias or orig, original=orig, alias=alias))
            result.imports.append(imp)

    # from . import x (relative)
    for node, cap in captures:
        if cap == "import.relative.stmt":
            mod_node = _find_child_capture(captures, node, "import.relative.module")
            name_node = _find_child_capture(captures, node, "import.relative.name")
            if mod_node:
                raw = node_text(mod_node, source)  # e.g. "."  or ".utils"
                imp = Import(raw_path=raw, line=node.start_point[0] + 1, is_relative=True)
                if name_node:
                    orig = node_text(name_node, source)
                    imp.names.append(ImportedName(name=orig, original=orig))
                result.imports.append(imp)


def _extract_calls(captures: list, source: str, result: FileSymbols) -> None:
    # Simple calls: func(args) — but only at function scope
    for node, cap in captures:
        if cap == "call.expr":
            name_node = _find_child_capture(captures, node, "call.name")
            if name_node:
                result.calls.append(Call(
                    caller_name="",  # resolved later by stitcher
                    callee_name=node_text(name_node, source),
                    line=node.start_point[0] + 1,
                ))

    # Method calls: obj.method(args)
    for node, cap in captures:
        if cap == "call.method.expr":
            method_node = _find_child_capture(captures, node, "call.method")
            if method_node:
                result.calls.append(Call(
                    caller_name="",
                    callee_name=node_text(method_node, source),
                    line=node.start_point[0] + 1,
                    is_method=True,
                ))

    # Dynamic calls via getattr
    for node, cap in captures:
        if cap == "call.dynamic":
            result.calls.append(Call(
                caller_name="",
                callee_name="<dynamic>",
                line=node.start_point[0] + 1,
                is_dynamic=True,
            ))


def _mark_exports(captures: list, source: str, result: FileSymbols) -> None:
    """Mark symbols as exported if they appear in __all__."""
    all_node = next((n for n, c in captures if c == "export.names"), None)
    if not all_node:
        return
    # Extract string names from __all__ list
    exported_names: set[str] = set()
    for child in all_node.children:
        if child.type == "string":
            name = node_text(child, source).strip("'\"")
            exported_names.add(name)
    for sym in result.symbols:
        if sym.name in exported_names:
            sym.is_exported = True
