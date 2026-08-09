"""
CLI for local development and testing.

Usage:
  indexer index    --repo https://github.com/org/repo --id org/repo
  indexer query    --repo org/repo --impact "function_name"
  indexer search   --repo org/repo --q "payment processing logic"
  indexer health
"""
import click
import structlog

log = structlog.get_logger(__name__)


@click.group()
def cli():
    """Codebase Indexer — Layer 2"""
    pass


@cli.command()
@click.option("--repo", required=True, help="Git clone URL")
@click.option("--id", "repo_id", required=True, help="Repo ID (e.g. org/repo-name)")
@click.option("--branch", default="main", help="Branch to index")
@click.option("--sync", is_flag=True, default=False, help="Run synchronously (wait for completion)")
@click.option("--skip-descriptions", is_flag=True, default=False, help="Skip LLM descriptions (uses docstring/name fallback, much faster)")
def index(repo: str, repo_id: str, branch: str, sync: bool, skip_descriptions: bool):
    """Trigger a full index of a repository."""
    from src.workers.index_worker import full_index
    from src.graph.client import setup_indexes
    from src.semantic.vector_store import setup_collection

    if skip_descriptions:
        from src.config import settings
        settings.skip_descriptions = True

    click.echo(f"Setting up indexes...")
    setup_indexes()
    setup_collection()

    if sync:
        # Run all Celery tasks in-process (no worker needed for local dev)
        from src.workers.celery_app import app as celery_app
        celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)
        click.echo(f"Indexing {repo_id} synchronously (this may take a while)...")
        result = full_index(repo_id, repo, branch)
        click.echo(f"Done: {result}")
    else:
        task = full_index.apply_async(args=[repo_id, repo, branch], queue="indexing")
        click.echo(f"Queued: task_id={task.id}")


@cli.command()
@click.option("--repo", "repo_id", required=True, help="Repo ID")
@click.option("--symbol", required=True, help="Symbol name to analyse")
@click.option("--depth", default=3, type=int)
def impact(repo_id: str, symbol: str, depth: int):
    """Show impact analysis for a symbol."""
    from src.graph.queries import get_impact
    result = get_impact(repo_id, symbol, depth)
    click.echo(f"\nImpact of changing '{symbol}':")
    click.echo(f"  Will break ({len(result['will_break'])} callers):")
    for r in result["will_break"][:10]:
        click.echo(f"    [{r['depth']}] {r['symbol']} in {r['file']}")
    click.echo(f"  May break ({len(result['may_break'])} dynamic callers):")
    for r in result["may_break"][:5]:
        click.echo(f"    [?] {r['symbol']} in {r['file']}")


@cli.command()
@click.option("--repo", "repo_id", required=True, help="Repo ID")
@click.option("--q", "query", required=True, help="Natural language query")
@click.option("--limit", default=5, type=int)
def search(repo_id: str, query: str, limit: int):
    """Semantic search over indexed code."""
    from src.semantic.embeddings import embed_single
    from src.semantic.vector_store import search as vs_search

    click.echo(f"Searching: '{query}'")
    vector = embed_single(query)
    results = vs_search(vector, repo_id, limit=limit)
    for i, r in enumerate(results, 1):
        click.echo(f"\n{i}. [{r['entity_type']}] {r['qualified_name']} (score={r['score']:.3f})")
        click.echo(f"   {r['file_path']}:{r.get('line_start', '?')}")
        if r.get("summary"):
            click.echo(f"   {r['summary'][:120]}")


@cli.command()
@click.option("--repo", "repo_id", required=True, help="Repo ID")
@click.option("--repo-root", required=True, help="Path to local repo clone")
def reindex_classes(repo_id: str, repo_root: str):
    """Targeted re-index: only files containing classes (for INHERITS edges).

    ~5-15% of initial index time. No LLM descriptions needed (cached).
    """
    from pathlib import Path
    from src.graph import client as g
    from src.graph.client import setup_indexes

    setup_indexes()

    # Find files with classes in the graph
    rows = g.run(
        """
        MATCH (f:File)-[:CONTAINS]->(c:Class)
        WHERE f.repo_id = $repo_id
        RETURN DISTINCT f.path AS path
        """,
        {"repo_id": repo_id},
    )
    class_files = [r["path"] for r in rows]
    click.echo(f"Found {len(class_files)} files with classes to re-index")

    if not class_files:
        click.echo("No class files found. Run a full index first.")
        return

    # Clear hashes for these files so they re-parse
    for fp in class_files:
        g.run_void(
            """
            MATCH (f:File {path: $path, repo_id: $repo_id})
            SET f.content_hash = ''
            """,
            {"path": fp, "repo_id": repo_id},
        )

    click.echo(f"Cleared hashes. Re-indexing {len(class_files)} files...")

    # Re-index each file synchronously
    from src.workers.index_worker import index_file
    from src.workers.celery_app import app as celery_app
    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)

    repo_root_path = Path(repo_root)
    success = 0
    for i, fp in enumerate(class_files):
        result = index_file(repo_id, fp, str(repo_root_path))
        status = result.get("status", "unknown")
        if status in ("indexed", "success"):
            success += 1
        if (i + 1) % 20 == 0:
            click.echo(f"  {i + 1}/{len(class_files)} processed...")

    click.echo(f"Done: {success}/{len(class_files)} files re-indexed with INHERITS edges.")


@cli.command()
def health():
    """Check status of all services."""
    from src.api.health import health_check
    result = health_check()
    status = result["status"].upper()
    click.echo(f"Overall: {status}")
    for svc, check in result["checks"].items():
        icon = "✓" if check["status"] == "ok" else "✗"
        click.echo(f"  {icon} {svc}: {check['status']}")
        if "detail" in check:
            click.echo(f"      {check['detail']}")


if __name__ == "__main__":
    cli()
