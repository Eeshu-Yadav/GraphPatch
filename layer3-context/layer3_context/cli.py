"""
Layer 3 CLI -- test context assembly without Layer 4.

Usage:
  python -m layer3_context.cli assemble --repo realpython/codetiming --id TICKET-1 \\
    --title "Timer.stop() crashes when called twice" \\
    --body "Calling stop() twice raises TimerError but message is unclear"
"""
from __future__ import annotations

import json
import sys

import click
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(20))

from layer3_context.models.ticket import Ticket

log = structlog.get_logger()


@click.group()
def cli():
    pass


@cli.command()
@click.option("--repo", required=True, help="repo_id, e.g. realpython/codetiming")
@click.option("--id", "ticket_id", required=True, help="Ticket ID, e.g. TICKET-1")
@click.option("--title", required=True)
@click.option("--body", default="", help="Ticket body/description")
@click.option("--max-symbols", default=20)
@click.option("--max-files", default=10)
@click.option("--json-output", is_flag=True, help="Print JSON instead of markdown")
def assemble(repo, ticket_id, title, body, max_symbols, max_files, json_output):
    """Assemble context bundle for a ticket."""
    from layer3_context.assembly.assembler import assemble as run_assemble

    ticket = Ticket(ticket_id=ticket_id, title=title, body=body, repo_id=repo)
    click.echo(f"Assembling context for: [{ticket_id}] {title}")

    bundle = run_assemble(ticket, max_symbols=max_symbols, max_files=max_files)

    if json_output:
        import dataclasses
        click.echo(json.dumps(dataclasses.asdict(bundle), indent=2, default=str))
    else:
        click.echo("\n" + bundle.to_prompt_text())
        click.echo(f"\n---\nToken estimate: ~{bundle.token_estimate:,}")
        click.echo(f"Strategies: {', '.join(bundle.strategies_used)}")


if __name__ == "__main__":
    cli()
