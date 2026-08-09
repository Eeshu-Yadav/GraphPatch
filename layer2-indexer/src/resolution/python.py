"""
Python import resolver.
Converts raw import paths → resolved file paths within the repo.

Handles:
  - Absolute imports: 'os', 'src.utils.helpers'
  - Relative imports: '.utils', '..models.user'
  - __init__.py re-exports: follows re-export chain to actual definition
  - External packages: marked as external (not indexed)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


class PythonResolver:
    def __init__(self, repo_root: str, all_files: set[str]):
        """
        repo_root: absolute path to cloned repo
        all_files: set of relative file paths that exist in this repo
        """
        self.root = Path(repo_root)
        self.all_files = all_files
        self._init_cache: dict[str, dict[str, str]] = {}  # file → {name: source_module}

    def resolve(self, raw_path: str, source_file: str, is_relative: bool = False) -> Optional[str]:
        """
        Return relative file path that raw_path resolves to, or None if external/unresolvable.
        """
        if is_relative or raw_path.startswith("."):
            return self._resolve_relative(raw_path, source_file)
        return self._resolve_absolute(raw_path, source_file)

    def _resolve_relative(self, raw: str, source_file: str) -> Optional[str]:
        """Resolve 'from . import x' or 'from ..models import y'."""
        source_dir = Path(source_file).parent

        # Count leading dots to determine how many levels up
        dots = len(raw) - len(raw.lstrip("."))
        module_part = raw.lstrip(".")  # e.g. "utils" from ".utils"

        # Go up (dots - 1) levels from the source file's directory
        base = source_dir
        for _ in range(dots - 1):
            base = base.parent

        if module_part:
            candidate_base = base / module_part.replace(".", "/")
        else:
            candidate_base = base

        return self._try_resolve_path(candidate_base)

    def _resolve_absolute(self, raw: str, source_file: str) -> Optional[str]:
        """Resolve 'from src.utils.helpers import foo'."""
        # Convert dotted path to filesystem path
        parts = raw.replace(".", "/")
        candidate = Path(parts)
        return self._try_resolve_path(candidate)

    def _try_resolve_path(self, base: Path) -> Optional[str]:
        """
        Try: base.py → base/__init__.py, return relative path if found.
        """
        candidates = [
            f"{base}.py",
            f"{base}/__init__.py",
            f"{base}.pyi",
        ]
        for c in candidates:
            normalized = str(Path(c))  # normalize separators
            if normalized in self.all_files:
                return normalized
        return None

    def resolve_reexport(self, init_file: str, name: str) -> Optional[str]:
        """
        If init_file re-exports `name`, follow the chain and return the actual source file.
        e.g. payments/__init__.py does 'from .stripe import charge_card'
             → returns 'payments/stripe.py'
        """
        exports = self._parse_init_exports(init_file)
        if name in exports:
            source_module = exports[name]
            resolved = self._resolve_relative(source_module, init_file)
            if resolved and resolved != init_file:
                # Recurse in case there's another layer of re-exports
                if resolved.endswith("__init__.py"):
                    deeper = self.resolve_reexport(resolved, name)
                    return deeper or resolved
                return resolved
        return None

    def _parse_init_exports(self, init_file: str) -> dict[str, str]:
        """Parse __init__.py and return {exported_name: source_module_path}."""
        if init_file in self._init_cache:
            return self._init_cache[init_file]

        abs_path = self.root / init_file
        if not abs_path.exists():
            return {}

        exports: dict[str, str] = {}
        try:
            tree = ast.parse(abs_path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    # from .stripe import charge_card, refund
                    source = "." * (node.level or 0) + (node.module or "")
                    for alias in node.names:
                        exports[alias.asname or alias.name] = source
        except SyntaxError:
            pass

        self._init_cache[init_file] = exports
        return exports

    def find_all_python_files(self) -> list[str]:
        return [f for f in self.all_files if f.endswith((".py", ".pyi"))]
