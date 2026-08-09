"""
Core data structures for the indexing pipeline.
All parsers produce these — language-agnostic normalized form.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Language(str, Enum):
    PYTHON = "python"
    TYPESCRIPT = "typescript"
    JAVASCRIPT = "javascript"
    CSS = "css"
    SHELL = "shell"
    UNKNOWN = "unknown"


class SymbolKind(str, Enum):
    FUNCTION = "Function"
    CLASS = "Class"
    VARIABLE = "Variable"
    MODULE = "Module"


class EdgeKind(str, Enum):
    CONTAINS = "CONTAINS"
    IMPORTS = "IMPORTS"
    CALLS = "CALLS"
    INSTANTIATES = "INSTANTIATES"
    INHERITS = "INHERITS"
    HAS_METHOD = "HAS_METHOD"
    EXPORTS = "EXPORTS"
    DECORATED_BY = "DECORATED_BY"
    TEST_FOR = "TEST_FOR"
    COUPLED_WITH = "COUPLED_WITH"
    OWNED_BY = "OWNED_BY"


@dataclass
class Symbol:
    """A single extracted code symbol (function, class, variable)."""
    name: str
    kind: SymbolKind
    file_path: str          # relative to repo root
    line_start: int
    line_end: int
    is_exported: bool = False
    is_async: bool = False
    qualified_name: str = ""   # e.g. "ClassName.method_name"
    docstring: str = ""
    bases: list[str] = field(default_factory=list)  # parent class names (for Classes)
    # Set during semantic enrichment stage
    summary: str = ""
    # Set during graph stage
    id: str = ""            # "repo_id:file_path:line_start:name"

    def __post_init__(self):
        if not self.qualified_name:
            self.qualified_name = self.name

    def make_id(self, repo_id: str) -> str:
        self.id = f"{repo_id}:{self.file_path}:{self.line_start}:{self.name}"
        return self.id


@dataclass
class ImportedName:
    """A single name imported from a module."""
    name: str           # the name as used in this file (may be alias)
    original: str       # the actual exported name
    alias: str = ""     # set if 'import X as Y' or '{ X as Y }'


@dataclass
class Import:
    """An import statement extracted from a file."""
    raw_path: str           # exactly as written in source: './utils', 'os', '@/components/...'
    names: list[ImportedName] = field(default_factory=list)  # empty = wildcard / namespace import
    line: int = 0
    is_relative: bool = False
    # Resolved during stitching — None means external/unresolvable
    resolved_file: Optional[str] = None


@dataclass
class Call:
    """A function call extracted from a file."""
    caller_name: str        # symbol that contains this call
    callee_name: str        # function/method being called
    line: int = 0
    is_dynamic: bool = False    # getattr, computed property, etc.
    is_method: bool = False


@dataclass
class Decorator:
    """A decorator applied to a function or class."""
    decorator_name: str
    target_name: str        # function/class being decorated
    order: int = 0          # 0 = outermost decorator


@dataclass
class FileSymbols:
    """Everything extracted from a single file by the parser."""
    path: str               # relative to repo root
    language: Language
    content_hash: str       # sha256 of raw content
    lines: int
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    decorators: list[Decorator] = field(default_factory=list)
    is_test: bool = False
    is_generated: bool = False
    parse_error: bool = False   # True if tree-sitter returned errors (partial parse)
