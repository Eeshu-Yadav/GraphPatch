"""
Graph as Context Provider — NOT as Tools

Instead of exposing get_dependencies, get_callers, get_impact as 13 tools
that the agent ignores, the graph INJECTS context automatically:

1. BEFORE exploration: seed context from ticket keywords → graph lookup
2. DURING exploration: after each find_files → auto-expand with graph
3. Agent never calls graph tools — it RECEIVES enriched context

This is how Claude Code works: git status, environment info, etc. are
injected into the system prompt — the agent doesn't call tools to get them.
"""
from __future__ import annotations

import re
import structlog

log = structlog.get_logger(__name__)


def build_graph_context(
    ticket_title: str,
    ticket_body: str,
    repo_id: str,
) -> str:
    """
    Build a rich context block from the knowledge graph.
    Injected into the system prompt BEFORE the agent starts.
    No LLM calls. Pure graph queries.

    Returns markdown text describing:
    - Files related to ticket keywords
    - Their dependencies (who imports them, what they import)
    - Their callers (who calls functions in them)
    - Test files covering them
    - High-centrality files in relevant directories
    """
    sections = []

    # 1. Rank files against the ticket with BM25 + identifier boost + graph rerank
    ranked = _find_files_for_ticket(ticket_title, ticket_body, repo_id)
    matched_files = [r["path"] for r in ranked]

    if matched_files:
        sections.append("## Files ranked by relevance (BM25 + graph rerank)")
        for r in ranked[:15]:
            details = []
            if r.get("identifier_hits"):
                details.append(f"ids={r['identifier_hits']}")
            if r.get("path_mention"):
                details.append("path-mentioned")
            if r.get("coupling_boost", 0) > 0:
                details.append(f"coupling+{r['coupling_boost']:.2f}")
            detail_str = f" ({', '.join(details)})" if details else ""
            sections.append(f"- `{r['path']}` score={r['score']}{detail_str}")

    # 2. For each matched file, get dependencies
    all_targets = set(matched_files)
    dep_sections = []
    for f in matched_files[:5]:  # Top 5 to avoid noise
        deps = _get_file_dependencies(f, repo_id)
        if deps["imported_by"] or deps["imports_local"]:
            dep_sections.append(f"\n### `{f}`")
            if deps["imported_by"]:
                dep_sections.append("  Imported by: " + ", ".join(f"`{d}`" for d in deps["imported_by"][:5]))
                all_targets.update(deps["imported_by"][:5])
            if deps["imports_local"]:
                dep_sections.append("  Imports: " + ", ".join(f"`{d}`" for d in deps["imports_local"][:5]))

    if dep_sections:
        sections.append("\n## Dependencies (from knowledge graph)")
        sections.extend(dep_sections)

    # 3. Coupled files (git co-change) — surfaces multi-file change patterns
    coupling_sections = []
    strong_coupled: set[str] = set()
    for f in matched_files[:10]:
        couples = _get_coupled_files(f, repo_id)
        if not couples:
            continue
        coupling_sections.append(f"\n### `{f}`")
        for other, jaccard, conf in couples[:3]:
            coupling_sections.append(
                f"  - `{other}` (jaccard={jaccard:.2f}, co-change confidence {conf:.0%})"
            )
            if conf >= 0.3:
                strong_coupled.add(other)
                all_targets.add(other)
    if coupling_sections:
        sections.append("\n## Files historically co-changed (from git history)")
        sections.extend(coupling_sections)
        if strong_coupled:
            sections.append(
                f"\n**Multi-file warning:** {len(strong_coupled)} file(s) co-change with a matched file at >=30% confidence — "
                "non-trivial fixes usually edit these too: "
                + ", ".join(f"`{p}`" for p in sorted(strong_coupled)[:5])
            )

    # 4. Get test files for matched files
    test_files = []
    for f in matched_files[:5]:
        tests = _get_test_files(f, repo_id)
        test_files.extend(tests)
    if test_files:
        sections.append("\n## Test files covering these sources")
        for t in sorted(set(test_files))[:10]:
            sections.append(f"- `{t}`")

    # 4. Get high-centrality files in relevant directories
    dirs = set()
    for f in matched_files[:5]:
        d = "/".join(f.split("/")[:-1])
        if d:
            dirs.add(d)
    if dirs:
        top_files = _get_top_files_in_dirs(list(dirs)[:3], repo_id)
        if top_files:
            sections.append("\n## Most important files in related directories (by PageRank)")
            for f, centrality in top_files[:10]:
                sections.append(f"- `{f}` (centrality: {centrality:.4f})")

    # 5. Get __init__.py / index files in relevant directories
    init_files = _find_init_files(list(dirs)[:5], repo_id)
    if init_files:
        sections.append("\n## Registration/init files (may need updates for new features)")
        for f in init_files:
            all_targets.add(f)
            sections.append(f"- `{f}`")

    context = "\n".join(sections)
    log.info("graph_context.built",
             matched_files=len(matched_files),
             total_targets=len(all_targets),
             context_chars=len(context))

    return context


def expand_from_files(file_paths: list[str], repo_id: str) -> str:
    """
    Called AUTOMATICALLY after find_files. Runs ALL graph queries:
    dependencies, callers, test coverage, coupled files, top files.
    Replaces 13 graph tools with zero-turn context injection.
    """
    sections = []

    for fp in file_paths[:5]:
        file_sections = [f"\n### `{fp}`"]

        # 1. Dependencies (replaces get_dependencies tool)
        deps = _get_file_dependencies(fp, repo_id)
        if deps["imported_by"]:
            file_sections.append("  **Imported by:** " +
                          ", ".join(f"`{d}`" for d in deps["imported_by"][:5]))
        if deps["imports_local"]:
            file_sections.append("  **Imports:** " +
                          ", ".join(f"`{d}`" for d in deps["imports_local"][:5]))

        # 2. Callers (replaces get_callers tool)
        callers = _get_callers_of_file(fp, repo_id)
        if callers:
            file_sections.append("  **Called by:** " +
                          ", ".join(f"`{c[0]}` in `{c[1]}`" for c in callers[:5]))

        # 3. Test coverage (replaces get_test_coverage tool)
        tests = _get_test_files(fp, repo_id)
        if tests:
            file_sections.append("  **Tests:** " + ", ".join(f"`{t}`" for t in tests[:3]))

        # 4. Coupled files (replaces get_coupled_files tool)
        coupled = _get_coupled_files(fp, repo_id)
        if coupled:
            file_sections.append("  **Co-changes with:** " +
                          ", ".join(f"`{c}` (j={j:.2f}, conf={conf:.0%})" for c, j, conf in coupled[:3]))

        # 5. Risk/centrality (replaces get_risk_score tool)
        risk = _get_risk_info(fp, repo_id)
        if risk:
            file_sections.append(f"  **Risk:** centrality={risk['centrality']:.4f}, "
                               f"dependents={risk['dependents']}, "
                               f"tests={risk['test_count']}")

        if len(file_sections) > 1:  # more than just the header
            sections.extend(file_sections)

    if not sections:
        return ""

    return "\n## Graph context (auto-injected)\n" + "\n".join(sections)


def expand_from_read(file_path: str, repo_id: str) -> str:
    """
    Called AUTOMATICALLY after read_file. Injects class attributes
    and function signatures for the file that was just read.
    Replaces get_symbol_details + get_class_hierarchy tools.
    """
    sections = []

    # Class attributes (replaces manual graph tool calls)
    classes = _graph_query(
        """MATCH (c:Class {file_path: $path, repo_id: $rid})
           RETURN c.name AS name""",
        {"path": file_path, "rid": repo_id}
    )

    for cls in classes[:5]:
        cls_name = cls.get("name", "")
        if not cls_name:
            continue

        attrs = _graph_query(
            """MATCH (v:Variable) WHERE v.repo_id = $rid
               AND v.qualified_name STARTS WITH $prefix
               RETURN v.name AS name, v.docstring AS value""",
            {"rid": repo_id, "prefix": f"{cls_name}."}
        )

        methods = _graph_query(
            """MATCH (c:Class {name: $cls, repo_id: $rid})-[:CONTAINS]->(fn:Function)
               RETURN fn.name AS name""",
            {"cls": cls_name, "rid": repo_id}
        )

        bases = _graph_query(
            """MATCH (c:Class {name: $cls, repo_id: $rid})-[:INHERITS]->(parent:Class)
               RETURN parent.name AS name""",
            {"cls": cls_name, "rid": repo_id}
        )

        if attrs or methods or bases:
            sections.append(f"\n**`{cls_name}`**" +
                          (f" (extends {', '.join(b['name'] for b in bases)})" if bases else ""))
            if attrs:
                for a in attrs[:8]:
                    val = a.get("value", "")[:50]
                    sections.append(f"  - `{a['name']}` = {val}")
            if methods:
                sections.append("  - methods: " +
                              ", ".join(f"`{m['name']}`" for m in methods[:8]))

    if not sections:
        return ""

    return "\n## Class details (auto-injected from graph)\n" + "\n".join(sections)


def _get_coupled_files(file_path: str, repo_id: str) -> list[tuple[str, float, float]]:
    """Files historically co-changed with this file. Returns [(path, jaccard, confidence), ...]."""
    results = _graph_query(
        """MATCH (f:File {path: $path, repo_id: $rid})-[c:COUPLED_WITH]->(other:File)
           RETURN other.path AS p,
                  coalesce(c.jaccard, c.score, 0.0) AS j,
                  coalesce(c.confidence, 0.0) AS conf
           ORDER BY j DESC LIMIT 5""",
        {"path": file_path, "rid": repo_id}
    )
    return [(r["p"], r["j"], r["conf"]) for r in results if r.get("p")]


def _get_risk_info(file_path: str, repo_id: str) -> dict | None:
    """Get risk indicators for a file."""
    result = _graph_query(
        """MATCH (f:File {path: $path, repo_id: $rid})
           OPTIONAL MATCH (other:File)-[:IMPORTS]->(:Module)-[:RESOLVES_TO]->(f)
           OPTIONAL MATCH (t:File)-[:TEST_FOR]->(f)
           RETURN f.centrality AS centrality,
                  count(DISTINCT other) AS dependents,
                  count(DISTINCT t) AS test_count""",
        {"path": file_path, "rid": repo_id}
    )
    if result and result[0].get("centrality") is not None:
        return {
            "centrality": result[0]["centrality"] or 0,
            "dependents": result[0]["dependents"] or 0,
            "test_count": result[0]["test_count"] or 0,
        }
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Internal helpers — pure graph queries, no LLM
# ═══════════════════════════════════════════════════════════════════════════

def _extract_keywords(text: str) -> list[str]:
    """Extract code-relevant keywords from ticket text. No LLM needed."""
    # Remove common English words
    stop_words = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "can", "shall", "to", "of", "in", "for",
        "on", "with", "at", "by", "from", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "out", "off", "over",
        "under", "again", "further", "then", "once", "here", "there", "when",
        "where", "why", "how", "all", "each", "every", "both", "few", "more",
        "most", "other", "some", "such", "no", "nor", "not", "only", "own",
        "same", "so", "than", "too", "very", "just", "because", "but", "and",
        "or", "if", "while", "that", "this", "these", "those", "it", "its",
        "we", "they", "them", "their", "our", "your", "my", "me", "him",
        "her", "he", "she", "i", "you", "what", "which", "who", "whom",
        "want", "need", "like", "get", "got", "make", "made", "keep",
        "something", "anything", "everything", "nothing", "also", "still",
        "already", "about", "much", "many", "well", "even", "since",
    }

    # Extract words, preserving CamelCase and snake_case
    words = re.findall(r'[A-Z][a-z]+(?:[A-Z][a-z]+)*|[a-z_]{3,}', text)
    keywords = []
    for w in words:
        w_lower = w.lower()
        if w_lower not in stop_words and len(w_lower) >= 3:
            keywords.append(w_lower)

    # Deduplicate preserving order
    seen = set()
    result = []
    for k in keywords:
        if k not in seen:
            seen.add(k)
            result.append(k)

    return result[:20]


def _graph_query(query: str, params: dict) -> list[dict]:
    """Safe graph query with error handling."""
    try:
        from graph.client import run
        return run(query, params)
    except Exception as e:
        log.debug("graph_context.query_failed", error=str(e)[:100])
        return []


def _find_files_by_keyword(keywords: list[str], repo_id: str) -> list[str]:
    """Kept for backward compatibility. Delegates to the BM25 locator with a reconstructed query."""
    query = " ".join(keywords)
    try:
        from layer3_context.retrieval.bm25 import locate_files
        results = locate_files(query, "", repo_id, top_k=15)
        return [r["path"] for r in results]
    except Exception as e:
        log.warning("bm25.locator_failed", error=str(e)[:200])
        return []


def _find_files_for_ticket(ticket_title: str, ticket_body: str, repo_id: str) -> list[dict]:
    """New entry point: score files against the full ticket using BM25 + graph rerank."""
    try:
        from layer3_context.retrieval.bm25 import locate_files
        return locate_files(ticket_title, ticket_body, repo_id, top_k=15)
    except Exception as e:
        log.warning("bm25.locator_failed", error=str(e)[:200])
        return []


def _get_file_dependencies(file_path: str, repo_id: str) -> dict:
    """Get who imports this file and what it imports (local files only)."""
    # Who imports this file
    imported_by = _graph_query(
        """MATCH (source:File)-[:IMPORTS]->(m:Module)-[:RESOLVES_TO]->(target:File {path: $path, repo_id: $rid})
           RETURN source.path AS p""",
        {"path": file_path, "rid": repo_id}
    )

    # What local files this file imports
    imports = _graph_query(
        """MATCH (f:File {path: $path, repo_id: $rid})-[:IMPORTS]->(m:Module)-[:RESOLVES_TO]->(dep:File)
           RETURN dep.path AS p""",
        {"path": file_path, "rid": repo_id}
    )

    # Filter to same directory or nearby (avoid noise from stdlib)
    file_dir = "/".join(file_path.split("/")[:-2])  # parent of parent
    local_imports = [r["p"] for r in imports if r.get("p") and r["p"].startswith(file_dir)]

    return {
        "imported_by": [r["p"] for r in imported_by if r.get("p")],
        "imports_local": local_imports,
    }


def _get_test_files(file_path: str, repo_id: str) -> list[str]:
    """Get test files covering this source file."""
    results = _graph_query(
        """MATCH (t:File)-[:TEST_FOR]->(s:File {path: $path, repo_id: $rid})
           RETURN t.path AS p""",
        {"path": file_path, "rid": repo_id}
    )
    return [r["p"] for r in results if r.get("p")]


def _get_callers_of_file(file_path: str, repo_id: str) -> list[tuple[str, str]]:
    """Get functions that call functions in this file."""
    results = _graph_query(
        """MATCH (caller:Function)-[:CALLS]->(target:Function {file_path: $path, repo_id: $rid})
           RETURN DISTINCT caller.name AS fn, caller.file_path AS fp
           LIMIT 10""",
        {"path": file_path, "rid": repo_id}
    )
    return [(r["fn"], r["fp"]) for r in results if r.get("fn") and r.get("fp")]


def _get_top_files_in_dirs(dirs: list[str], repo_id: str) -> list[tuple[str, float]]:
    """Get highest-centrality files in given directories."""
    results = []
    for d in dirs:
        rows = _graph_query(
            """MATCH (f:File) WHERE f.repo_id = $rid AND f.path STARTS WITH $dir
               AND f.centrality IS NOT NULL
               RETURN f.path AS p, f.centrality AS c
               ORDER BY f.centrality DESC LIMIT 5""",
            {"rid": repo_id, "dir": d + "/"}
        )
        results.extend([(r["p"], r["c"]) for r in rows if r.get("p")])
    return sorted(results, key=lambda x: x[1], reverse=True)[:10]


def _find_init_files(dirs: list[str], repo_id: str) -> list[str]:
    """
    Find registration files in directories — generalized.
    Instead of checking hardcoded names, finds the file that
    imports the MOST siblings (the structural registration pattern).
    """
    init_files = []
    for d in dirs:
        # Find which file in this directory imports the most other files in the same directory
        rows = _graph_query(
            """MATCH (reg:File)-[:IMPORTS]->(m:Module)-[:RESOLVES_TO]->(sib:File)
               WHERE reg.repo_id = $rid AND reg.path STARTS WITH $dir
               AND sib.path STARTS WITH $dir AND sib.path <> reg.path
               RETURN reg.path AS p, count(sib) AS cnt
               ORDER BY cnt DESC LIMIT 1""",
            {"rid": repo_id, "dir": d + "/"}
        )
        for r in rows:
            if r.get("cnt", 0) >= 3 and r.get("p"):
                init_files.append(r["p"])
    return init_files


# ═══════════════════════════════════════════════════════════════════════════
# FILE COMPLETENESS DETECTION — 3 Patterns (Language-Agnostic)
# Runs AFTER agent creates/modifies files. Returns warnings to inject.
# ═══════════════════════════════════════════════════════════════════════════

def detect_missing_files(
    new_file_path: str,
    new_file_content: str,
    repo_id: str,
    modified_files: dict[str, str],
) -> list[str]:
    """
    After agent creates or modifies a file, detect what ELSE needs changing.
    Returns list of warning strings to inject into the agent's next message.

    Runs 3 patterns:
      1. Sibling Registration — does a registration file need updating?
      2. Attribute Gap — does an upstream class need new attributes?
      3. Functional Overlap — does existing code conflict with new code?
    """
    warnings = []

    w1 = _detect_sibling_registration(new_file_path, repo_id, modified_files)
    if w1:
        warnings.append(w1)

    w2 = _detect_attribute_gap(new_file_path, new_file_content, repo_id)
    if w2:
        warnings.append(w2)

    w3 = _detect_functional_overlap(new_file_path, new_file_content, repo_id)
    if w3:
        warnings.append(w3)

    return warnings


# ── Pattern 1: Sibling Registration ───────────────────────────────────────

def _detect_sibling_registration(
    new_file_path: str,
    repo_id: str,
    modified_files: dict[str, str],
) -> str | None:
    """
    Check if a registration file in the same directory imports most siblings
    but NOT the new file. If so, warn the agent.

    Works for any language: Python __init__.py, TS index.ts, Rust mod.rs, etc.
    Detection is by COUNTING import edges, not by file name.
    """
    directory = "/".join(new_file_path.split("/")[:-1])
    if not directory:
        return None

    new_filename = new_file_path.split("/")[-1]
    new_module = new_filename.rsplit(".", 1)[0]  # strip extension

    # Find all files in the same directory
    siblings = _graph_query(
        """MATCH (f:File) WHERE f.repo_id = $rid
           AND f.path STARTS WITH $dir AND f.path <> $dir
           AND NOT f.path CONTAINS '/'  // direct children only after removing dir prefix
           RETURN f.path AS p""",
        {"rid": repo_id, "dir": directory + "/"}
    )
    # Simpler: get all files in directory
    siblings = _graph_query(
        """MATCH (f:File) WHERE f.repo_id = $rid AND f.path STARTS WITH $dir
           RETURN f.path AS p""",
        {"rid": repo_id, "dir": directory + "/"}
    )
    sibling_paths = [r["p"] for r in siblings if r.get("p")]
    # Filter to direct children (no subdirectory files)
    sibling_paths = [p for p in sibling_paths
                     if p.count("/") == new_file_path.count("/")]

    if len(sibling_paths) < 3:
        return None  # Too few files to detect a pattern

    # For each file in directory, count how many siblings it imports
    best_reg_file = None
    best_import_count = 0
    imported_siblings = []

    for candidate in sibling_paths:
        count = _graph_query(
            """MATCH (reg:File {path: $reg, repo_id: $rid})
                     -[:IMPORTS]->(m:Module)-[:RESOLVES_TO]->(sib:File)
               WHERE sib.path STARTS WITH $dir AND sib.path <> $reg
               RETURN count(sib) AS cnt, collect(sib.path) AS sibs""",
            {"reg": candidate, "rid": repo_id, "dir": directory + "/"}
        )
        if count and count[0].get("cnt", 0) > best_import_count:
            best_import_count = count[0]["cnt"]
            best_reg_file = candidate
            imported_siblings = count[0].get("sibs", [])

    if not best_reg_file or best_import_count < 3:
        return None  # No file imports enough siblings to be a registration file

    # Check: does the registration file already import the new file?
    # (the new file may not be in the graph yet, so check by module name)
    already_imported = any(new_module in sib for sib in imported_siblings)
    if already_imported:
        return None

    # Also check if the agent already modified the registration file
    if best_reg_file in modified_files:
        return None

    # Calculate the ratio
    total_siblings = len([p for p in sibling_paths if p != best_reg_file])
    ratio = best_import_count / max(total_siblings, 1)

    if ratio < 0.4:
        return None  # Less than 40% of siblings imported — not a clear pattern

    reg_filename = best_reg_file.split("/")[-1]
    examples = "\n".join(f"    {s.split('/')[-1]}" for s in imported_siblings[:5])

    return (
        f"REGISTRATION WARNING: `{reg_filename}` imports {best_import_count} of "
        f"{total_siblings} files in `{directory}/`:\n"
        f"{examples}\n"
        f"    ... ({best_import_count} total)\n\n"
        f"Your new file `{new_filename}` is NOT imported. "
        f"You likely need to add an import for `{new_module}` to `{best_reg_file}`."
    )


# ── Pattern 2: Attribute Gap ──────────────────────────────────────────────

def _detect_attribute_gap(
    new_file_path: str,
    new_file_content: str,
    repo_id: str,
) -> str | None:
    """
    Check if the new code accesses attributes that don't exist on the
    classes it imports. Language-agnostic: detects obj.attribute patterns.
    """
    if not new_file_content:
        return None

    # 1. Extract imported class names from the new code
    # Generic pattern: matches "from .foo import Bar", "import Foo", "from foo import Bar"
    import_pattern = re.compile(
        r'(?:from\s+\S+\s+import\s+|import\s+)([A-Z][A-Za-z0-9]*)'
    )
    imported_classes = import_pattern.findall(new_file_content)
    if not imported_classes:
        return None

    # 2. Extract attribute accesses from new code
    # Pattern: identifier.attribute (covers Python, JS, TS, Go, Rust field access)
    attr_pattern = re.compile(r'\.([a-z_][a-z_0-9]*)\b')
    accessed_attrs = set(attr_pattern.findall(new_file_content))
    # Remove common noise
    noise = {"self", "cls", "super", "append", "extend", "items", "keys", "values",
             "get", "set", "pop", "update", "format", "join", "split", "strip",
             "replace", "lower", "upper", "startswith", "endswith", "encode", "decode"}
    accessed_attrs -= noise

    if not accessed_attrs:
        return None

    # 3. For each imported class, get its attributes from the graph
    gaps = []
    for cls_name in imported_classes[:5]:
        graph_attrs = _graph_query(
            """MATCH (v:Variable) WHERE v.repo_id = $rid
               AND v.qualified_name STARTS WITH $prefix
               RETURN v.name AS name, v.docstring AS value""",
            {"rid": repo_id, "prefix": f"{cls_name}."}
        )
        if not graph_attrs:
            continue

        known_attrs = {r["name"] for r in graph_attrs if r.get("name")}

        # Also get methods
        graph_methods = _graph_query(
            """MATCH (c:Class {name: $cls, repo_id: $rid})-[:CONTAINS]->(fn:Function)
               RETURN fn.name AS name""",
            {"cls": cls_name, "rid": repo_id}
        )
        known_attrs.update(r["name"] for r in graph_methods if r.get("name"))

        # Find attributes the new code accesses that the class doesn't have
        missing = accessed_attrs & set()  # reset
        # Check which accessed attrs could belong to this class
        # (conservative: only flag if the class name appears near the attribute access)
        cls_pattern = re.compile(
            rf'{cls_name.lower()}\w*\.([a-z_][a-z_0-9]*)|'
            rf'[a-z_]*{cls_name.lower()}[a-z_]*\.([a-z_][a-z_0-9]*)',
            re.IGNORECASE
        )
        cls_accesses = set()
        for m in cls_pattern.finditer(new_file_content):
            attr = m.group(1) or m.group(2)
            if attr and attr not in noise:
                cls_accesses.add(attr)

        missing = cls_accesses - known_attrs - noise
        if missing:
            known_list = ", ".join(sorted(known_attrs)[:8])
            missing_list = ", ".join(sorted(missing)[:5])
            gaps.append(
                f"  `{cls_name}` has: [{known_list}]\n"
                f"  Your code accesses: [{missing_list}] — NOT in `{cls_name}`"
            )

    if not gaps:
        return None

    return (
        "ATTRIBUTE GAP WARNING: Your new code accesses attributes that may not exist:\n\n"
        + "\n\n".join(gaps)
        + "\n\nYou may need to add these attributes to the upstream class."
    )


# ── Pattern 3: Functional Overlap ─────────────────────────────────────────

def _detect_functional_overlap(
    new_file_path: str,
    new_file_content: str,
    repo_id: str,
) -> str | None:
    """
    Check if the new file's functions overlap with existing functions in
    other files. Detected by shared entity tokens in function names.
    """
    if not new_file_content:
        return None

    # 1. Extract function names from new code
    # Generic pattern: def func_name, function funcName, fn func_name, func funcName
    fn_pattern = re.compile(r'(?:def|function|fn|func)\s+([a-zA-Z_][a-zA-Z0-9_]*)')
    new_functions = fn_pattern.findall(new_file_content)
    if not new_functions:
        return None

    # 2. Extract entity tokens from function names
    # "itrs_to_observed" → {"itrs", "observed"}
    # "convertAltAzToITRS" → {"convert", "alt", "az", "itrs"}
    new_tokens = set()
    for fn in new_functions:
        # Split CamelCase and snake_case
        parts = re.findall(r'[A-Z][a-z]+|[a-z]+', fn)
        for p in parts:
            if len(p) >= 3:
                new_tokens.add(p.lower())

    # Remove generic tokens
    generic = {"get", "set", "init", "new", "create", "make", "build", "run",
               "test", "check", "validate", "process", "handle", "parse", "format",
               "convert", "from", "into", "self", "this", "that", "the", "def",
               "mat", "return", "none", "true", "false"}
    new_tokens -= generic

    if len(new_tokens) < 2:
        return None  # Not enough meaningful tokens

    # 3. Search graph for functions with overlapping tokens
    overlapping_files = {}
    for token in list(new_tokens)[:5]:
        results = _graph_query(
            """MATCH (fn:Function) WHERE fn.repo_id = $rid
               AND toLower(fn.name) CONTAINS $token
               AND fn.file_path <> $path
               RETURN fn.name AS name, fn.file_path AS file
               LIMIT 10""",
            {"rid": repo_id, "token": token, "path": new_file_path}
        )
        for r in results:
            fp = r.get("file", "")
            fn_name = r.get("name", "")
            if fp and fn_name:
                overlapping_files.setdefault(fp, []).append(fn_name)

    if not overlapping_files:
        return None

    # 4. Rank by overlap count — files with most matching functions are most relevant
    ranked = sorted(overlapping_files.items(), key=lambda x: len(x[1]), reverse=True)

    # Only warn about files with 2+ overlapping functions (avoid noise)
    significant = [(fp, fns) for fp, fns in ranked if len(fns) >= 2]
    if not significant:
        return None

    # 5. Build warning
    sections = []
    for fp, fns in significant[:3]:
        fn_list = ", ".join(f"`{fn}`" for fn in sorted(set(fns))[:5])
        sections.append(f"  `{fp}` has: {fn_list}")

    new_fn_list = ", ".join(f"`{fn}`" for fn in new_functions[:5])

    return (
        f"OVERLAP WARNING: Your new functions ({new_fn_list}) share entity names "
        f"with existing code:\n\n"
        + "\n".join(sections)
        + "\n\nThese files may need modification to avoid conflicts, "
        "or your new code may duplicate existing functionality. Check if they need updating."
    )
