"""
Keyword and entity extraction from ticket text.
Uses regex patterns to find symbol names, file paths, and keywords.
"""
from __future__ import annotations

import re

BACKTICK_RE = re.compile(r'`([^`\n]+)`')
CAMEL_RE = re.compile(r'\b([A-Z][a-zA-Z][a-zA-Z0-9]{1,})\b')
SNAKE_RE = re.compile(r'\b([a-z][a-z0-9]{1,}_[a-z0-9_]+)\b')  # must have underscore
FILEPATH_RE = re.compile(r'\b([\w./][\w/.-]*\.(?:py|ts|tsx|js|jsx|css|sh))\b')
# Extract meaningful domain words (3+ chars, not common English)
DOMAIN_WORD_RE = re.compile(r'\b([a-z]{3,})\b', re.IGNORECASE)

# Comprehensive stopword list — common English + common tech noise
STOP_WORDS = {
    # English
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'when',
    'then', 'also', 'have', 'been', 'will', 'should', 'could', 'would',
    'there', 'their', 'what', 'which', 'about', 'after', 'before', 'between',
    'some', 'other', 'more', 'most', 'much', 'many', 'such', 'only',
    'very', 'just', 'than', 'them', 'they', 'these', 'those', 'each',
    'every', 'both', 'either', 'neither', 'but', 'not', 'nor', 'yet',
    'here', 'where', 'how', 'why', 'who', 'whom', 'whose',
    'can', 'may', 'might', 'must', 'shall', 'need', 'dare',
    'does', 'did', 'done', 'doing', 'was', 'were', 'are', 'has', 'had',
    'being', 'get', 'got', 'getting', 'gets',
    'like', 'make', 'makes', 'made', 'take', 'takes', 'took',
    'use', 'used', 'using', 'uses', 'seem', 'seems', 'seemed',
    'report', 'reports', 'users', 'user', 'team', 'related',
    'instead', 'currently', 'already', 'still', 'actually',
    'try', 'tries', 'tried', 'want', 'wants', 'wanted',
    'add', 'added', 'adding', 'create', 'created', 'creating',
    'update', 'updated', 'updating', 'delete', 'deleted', 'deleting',
    'fix', 'fixed', 'fixing', 'change', 'changed', 'changing',
    'new', 'old', 'first', 'last', 'next', 'previous',
    'all', 'any', 'few', 'own', 'same', 'too',
    # Bug report noise
    'error', 'bug', 'issue', 'problem', 'broken', 'fails', 'failing',
    'expected', 'actual', 'steps', 'reproduce', 'priority', 'high',
    'low', 'medium', 'critical', 'blocking', 'blocker',
    'internal', 'server', 'return', 'returns', 'response',
    # Python keywords
    'True', 'False', 'None', 'return', 'import', 'class', 'def', 'async',
    'await', 'raise', 'except', 'finally', 'yield', 'lambda', 'pass',
    'break', 'continue', 'global', 'nonlocal', 'assert', 'type',
    # Tech acronyms (not useful for symbol lookup)
    'GET', 'POST', 'PUT', 'DELETE', 'HTTP', 'URL', 'API', 'JSON', 'HTML',
    'CSS', 'SQL', 'CLI', 'MCP', 'OR', 'AND', 'NOT', 'IN',
    'The', 'Add', 'Fix', 'Bug', 'Error', 'Internal', 'Server',
    'Users', 'User', 'Requirements', 'Return', 'Priority', 'High',
    # PR / pipeline template noise
    'Summary', 'Files', 'Changed', 'Modified', 'Model', 'Generated',
    'Validation', 'SKIPPED', 'Syntax', 'Tests', 'Lint', 'Auto',
    'Maintains', 'Applied', 'Created', 'When', 'DROP', 'RETRY',
    'passed', 'failed', 'skipped', 'issues', 'errors', 'warnings',
    'ticket', 'pipeline', 'generated', 'auto', 'branch', 'commit',
    'merged', 'opened', 'closed', 'draft', 'review', 'reviewer',
}


def extract_entities(full_text: str) -> dict:
    """
    Extract symbols, file paths, and keywords from ticket text.
    Returns dict with keys: symbols (list), files (list), keywords (list).
    Symbols are ordered: backtick-quoted first (highest confidence), then CamelCase, then snake_case.
    """
    backtick = BACKTICK_RE.findall(full_text)
    camel = CAMEL_RE.findall(full_text)
    snake = SNAKE_RE.findall(full_text)
    files = FILEPATH_RE.findall(full_text)

    # Extract domain-specific words (lowercase 3+ chars not in stopwords)
    domain_words = DOMAIN_WORD_RE.findall(full_text)
    domain_filtered = [w for w in domain_words
                       if w.lower() not in {s.lower() for s in STOP_WORDS}
                       and len(w) >= 4]  # 4+ chars for domain words

    def clean(tokens: list[str]) -> list[str]:
        return [t for t in tokens if t not in STOP_WORDS and len(t) > 2]

    bt_clean = clean(backtick)
    camel_clean = clean(camel)
    snake_clean = clean(snake)

    # Deduplicate preserving CamelCase when there's a case conflict
    # e.g. Timer (class) and timer (variable) → keep Timer
    seen: dict[str, str] = {}  # lower → preferred form
    symbols: list[str] = []
    for tok in bt_clean + camel_clean + snake_clean + domain_filtered:
        lower = tok.lower()
        if lower not in seen:
            seen[lower] = tok
            symbols.append(tok)
        elif tok != seen[lower] and tok[0].isupper() and not seen[lower][0].isupper():
            # New token is CamelCase, existing is lowercase — prefer CamelCase
            old = seen[lower]
            symbols = [tok if s == old else s for s in symbols]
            seen[lower] = tok

    return {
        "symbols": symbols[:25],
        "files": list(dict.fromkeys(files))[:10],
        "keywords": (bt_clean + domain_filtered[:10])[:10],
    }
