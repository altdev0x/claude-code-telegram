"""CLI entrypoint for claude-telegram-bot.

Provides subcommands for service management and job scheduling.
Running without a subcommand starts the bot (backward compatible).
"""

import click

from .schedule import schedule
from .service import logs, restart, start, status, stop


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Claude Code Telegram Bot — CLI management tool."""
    if ctx.invoked_subcommand is None:
        # Default: run the bot (backward compatible with old entry point)
        run()


@cli.command(name="run")
def run_cmd() -> None:
    """Start the bot in the foreground (same as running without a subcommand)."""
    run()


def run() -> None:
    """Start the bot — delegates to src.main.run."""
    from src.main import run as bot_run

    bot_run()


# Register service commands
cli.add_command(start)
cli.add_command(stop)
cli.add_command(restart)
cli.add_command(status)
cli.add_command(logs)

# Register schedule group
cli.add_command(schedule)
