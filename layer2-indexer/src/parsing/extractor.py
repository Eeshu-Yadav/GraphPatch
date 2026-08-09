"""
Tree-sitter based symbol extractor — language-agnostic engine.
Dispatches to language-specific extractors for query execution.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

import tree_sitter_python
import tree_sitter_typescript
import tree_sitter_javascript
import tree_sitter_css
import tree_sitter_bash
from tree_sitter import Language, Node, Parser

from src.models.symbol import FileSymbols, Language as Lang
from src.parsing.filter import is_generated, is_test_file

# Build tree-sitter language objects once at import time
_PY_LANG = Language(tree_sitter_python.language())
_TS_LANG = Language(tree_sitter_typescript.language_typescript())
_TSX_LANG = Language(tree_sitter_typescript.language_tsx())
_JS_LANG = Language(tree_sitter_javascript.language())
_CSS_LANG = Language(tree_sitter_css.language())
_BASH_LANG = Language(tree_sitter_bash.language())

_PARSERS: dict[Lang, Parser] = {
    Lang.PYTHON: Parser(_PY_LANG),
    Lang.TYPESCRIPT: Parser(_TS_LANG),
    Lang.JAVASCRIPT: Parser(_JS_LANG),
    Lang.CSS: Parser(_CSS_LANG),
    Lang.SHELL: Parser(_BASH_LANG),
}


def get_parser(lang: Lang) -> Optional[Parser]:
    return _PARSERS.get(lang)


def get_ts_language(lang: Lang) -> Optional[Language]:
    if lang == Lang.PYTHON:
        return _PY_LANG
    if lang == Lang.TYPESCRIPT:
        return _TS_LANG
    if lang == Lang.JAVASCRIPT:
        return _JS_LANG
    if lang == Lang.CSS:
        return _CSS_LANG
    if lang == Lang.SHELL:
        return _BASH_LANG
    return None


def extract(path: str, content: str, language: Lang) -> FileSymbols:
    """
    Parse a file and return all extracted symbols, imports, calls, decorators.
    Always returns a FileSymbols — never raises. Sets parse_error=True on failure.
    """
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    lines = content.count("\n") + 1

    result = FileSymbols(
        path=path,
        language=language,
        content_hash=content_hash,
        lines=lines,
        is_test=is_test_file(path),
        is_generated=is_generated(content),
    )

    parser = get_parser(language)
    if not parser:
        result.parse_error = True
        return result

    tree = parser.parse(content.encode())

    # Flag partial parse (tree-sitter still gives a tree, but with ERROR nodes)
    if tree.root_node.has_error:
        result.parse_error = True

    ts_lang = get_ts_language(language)
    if not ts_lang:
        return result

    # Dispatch to language-specific extractor
    if language == Lang.PYTHON:
        from src.parsing.extractors.python import extract_python
        extract_python(tree.root_node, ts_lang, content, result)
    elif language == Lang.TYPESCRIPT:
        from src.parsing.extractors.typescript import extract_typescript
        extract_typescript(tree.root_node, ts_lang, content, result)
    elif language == Lang.JAVASCRIPT:
        from src.parsing.extractors.javascript import extract_javascript
        extract_javascript(tree.root_node, ts_lang, content, result)
    elif language == Lang.CSS:
        from src.parsing.extractors.css import extract_css
        extract_css(tree.root_node, ts_lang, content, result)
    elif language == Lang.SHELL:
        from src.parsing.extractors.shell import extract_shell
        extract_shell(tree.root_node, ts_lang, content, result)

    return result


def node_text(node: Node, source: str) -> str:
    """Extract the source text for a tree-sitter node.

    Tree-sitter byte offsets are UTF-8 byte positions, so we must
    slice the source as bytes, not as a Python string (which uses
    character positions). Without this, non-ASCII content before
    the node shifts the offsets and corrupts extracted names.
    """
    return source.encode("utf-8", errors="replace")[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def first_capture(captures: list[tuple[Node, str]], name: str) -> Optional[Node]:
    """Get the first node with the given capture name from a flattened captures list."""
    for node, capture_name in captures:
        if capture_name == name:
            return node
    return None


def all_captures(captures: list[tuple[Node, str]], name: str) -> list[Node]:
    """Get all nodes with the given capture name from a flattened captures list."""
    return [node for node, cap in captures if cap == name]
