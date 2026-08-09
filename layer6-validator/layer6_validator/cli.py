"""
Layer 6 CLI — full pipeline including validation.
Usage:
  python -m layer6_validator.cli validate \
    --repo realpython/codetiming \
    --id TICKET-1 \
    --title "Timer.stop() crashes when called twice" \
    --body "..."
"""
import click
import structlog

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(20))


@click.group()
def cli():
    pass


@cli.command()
@click.option("--repo", required=True)
@click.option("--id", "ticket_id", required=True)
@click.option("--title", required=True)
@click.option("--body", default="")
def validate(repo, ticket_id, title, body):
    """Run full pipeline: context → agent → validate."""
    from layer3_context.models.ticket import Ticket
    from layer3_context.assembly.assembler import assemble
    from layer45_agent.agent import run_agent
    from layer45_agent.models import AgentConfig
    from layer6_validator.runner import validate as run_validate

    ticket = Ticket(ticket_id=ticket_id, title=title, body=body, repo_id=repo)

    click.echo("[Layer 3] Assembling context...")
    bundle = assemble(ticket, max_symbols=15, max_files=6)
    click.echo(f"  → {len(bundle.relevant_symbols)} symbols, {len(bundle.relevant_files)} files")

    click.echo("\n[Layer 4.5] Running agent...")
    config = AgentConfig()
    agent_result = run_agent(ticket, bundle, config)
    impl = agent_result.implementation
    click.echo(f"  → {len(impl.file_results)} files modified, {agent_result.iterations} iterations")

    click.echo("\n[Layer 6] Validating...")
    result = run_validate(impl)

    click.echo(f"\n{'='*60}")
    click.echo(result.summary())
    click.echo(f"{'='*60}")


if __name__ == "__main__":
    cli()
