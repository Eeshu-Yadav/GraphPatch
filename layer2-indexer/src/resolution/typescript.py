"""
TypeScript import resolver.
Handles:
  - Relative imports: './utils', '../models/user'
  - tsconfig.json path aliases: '@/components/*', '@payments/*'
  - Barrel files: 'import X from "../utils"' → utils/index.ts
  - node_modules (marked external)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

_TS_EXTENSIONS = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs")


class TypeScriptResolver:
    def __init__(self, repo_root: str, all_files: set[str]):
        self.root = Path(repo_root)
        self.all_files = all_files
        self._alias_map: dict[str, list[str]] = {}  # alias pattern → target paths
        self._tsconfig_loaded = False

    def _load_tsconfig(self) -> None:
        if self._tsconfig_loaded:
            return
        self._tsconfig_loaded = True

        # Find tsconfig.json (could be in root or subdirectories)
        tsconfig_path = self._find_tsconfig()
        if not tsconfig_path:
            return

        try:
            raw = json.loads(tsconfig_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        paths = raw.get("compilerOptions", {}).get("paths", {})
        base_url = raw.get("compilerOptions", {}).get("baseUrl", ".")
        base = (tsconfig_path.parent / base_url).resolve().relative_to(self.root.resolve())

        for alias_pattern, targets in paths.items():
            resolved_targets = [str(base / t.rstrip("*").rstrip("/")) for t in targets]
            self._alias_map[alias_pattern] = resolved_targets

    def _find_tsconfig(self) -> Optional[Path]:
        """Find the root tsconfig.json (not tsconfig.node.json etc.)."""
        for name in ("tsconfig.json", "tsconfig.base.json"):
            p = self.root / name
            if p.exists():
                return p
        return None

    def resolve(self, raw_path: str, source_file: str) -> Optional[str]:
        """
        Return relative file path that raw_path resolves to, or None if external.
        """
        # node_modules / bare specifiers → external
        if not raw_path.startswith(".") and not raw_path.startswith("@"):
            # Could be a path alias or external package
            self._load_tsconfig()
            resolved = self._resolve_alias(raw_path)
            if resolved:
                return resolved
            return None  # external package

        if raw_path.startswith("."):
            return self._resolve_relative(raw_path, source_file)

        # @ prefix — check alias map first
        self._load_tsconfig()
        resolved = self._resolve_alias(raw_path)
        return resolved

    def _resolve_alias(self, raw_path: str) -> Optional[str]:
        """Match raw_path against tsconfig path alias patterns."""
        for pattern, targets in self._alias_map.items():
            regex = re.escape(pattern).replace(r"\*", "(.*)")
            m = re.fullmatch(regex, raw_path)
            if m:
                suffix = m.group(1) if "*" in pattern else ""
                for target in targets:
                    candidate = Path(target.rstrip("/")) / suffix if suffix else Path(target)
                    resolved = self._try_resolve(candidate)
                    if resolved:
                        return resolved
        return None

    def _resolve_relative(self, raw_path: str, source_file: str) -> Optional[str]:
        source_dir = Path(source_file).parent
        candidate = (source_dir / raw_path).resolve()
        try:
            rel = candidate.relative_to(self.root.resolve())
        except ValueError:
            return None
        return self._try_resolve(rel)

    def _try_resolve(self, base: Path) -> Optional[str]:
        """
        Try extensions + index files:
          base.ts, base.tsx, base/index.ts, base/index.tsx, etc.
        """
        base_str = str(base)

        # Direct match (path already has extension)
        if base_str in self.all_files:
            return base_str

        # Try adding extensions
        for ext in _TS_EXTENSIONS:
            candidate = f"{base_str}{ext}"
            if candidate in self.all_files:
                return candidate

        # Barrel file: base/index.ts
        for ext in _TS_EXTENSIONS:
            candidate = str(base / f"index{ext}")
            if candidate in self.all_files:
                return candidate

        return None
