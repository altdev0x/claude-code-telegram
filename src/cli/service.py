"""Service lifecycle commands wrapping systemctl --user."""

import subprocess
import sys

import click

SERVICE_NAME = "claude-telegram-bot"


def _systemctl(action: str) -> None:
    """Run a systemctl --user command for the bot service."""
    cmd = ["systemctl", "--user", action, SERVICE_NAME]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.stdout:
            click.echo(result.stdout.rstrip())
        if result.stderr:
            click.echo(result.stderr.rstrip(), err=True)
        sys.exit(result.returncode)
    except FileNotFoundError:
        click.echo("Error: systemctl not found.", err=True)
        sys.exit(1)


@click.command()
def start() -> None:
    """Start the bot service."""
    _systemctl("start")


@click.command()
def stop() -> None:
    """Stop the bot service."""
    _systemctl("stop")


@click.command()
def restart() -> None:
    """Restart the bot service."""
    _systemctl("restart")


@click.command()
def status() -> None:
    """Show bot service status."""
    _systemctl("status")


@click.command()
@click.option("--follow", "-f", is_flag=True, help="Follow log output.")
@click.option("--lines", "-n", default=50, help="Number of lines to show.")
def logs(follow: bool, lines: int) -> None:
    """Show bot service logs."""
    cmd = [
        "journalctl",
        "--user",
        "-u",
        SERVICE_NAME,
        "-n",
        str(lines),
    ]
    if follow:
        cmd.append("-f")
    try:
        subprocess.run(cmd)
    except FileNotFoundError:
        click.echo("Error: journalctl not found.", err=True)
        sys.exit(1)
