"""
Language detection and file routing.
Determines what language a file is written in before parsing.
"""
from pathlib import Path

from src.models.symbol import Language

# Extension → language map (covers 99% of cases)
_EXT_MAP: dict[str, Language] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".css": Language.CSS,
    ".scss": Language.CSS,
    ".sass": Language.CSS,
    ".sh": Language.SHELL,
    ".bash": Language.SHELL,
}

# Shebang patterns for extensionless scripts
_SHEBANG_MAP: dict[str, Language] = {
    "python": Language.PYTHON,
    "python3": Language.PYTHON,
    "ts-node": Language.TYPESCRIPT,
    "node": Language.JAVASCRIPT,
    "bash": Language.SHELL,
    "sh": Language.SHELL,
}


def detect_language(path: str | Path, content: str = "") -> Language:
    """Return the language for a file, or UNKNOWN if unsupported."""
    p = Path(path)
    lang = _EXT_MAP.get(p.suffix.lower())
    if lang:
        return lang

    # Shebang fallback for extensionless files
    if content:
        first_line = content.split("\n", 1)[0]
        if first_line.startswith("#!"):
            for keyword, lang in _SHEBANG_MAP.items():
                if keyword in first_line:
                    return lang

    return Language.UNKNOWN
