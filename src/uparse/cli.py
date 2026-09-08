"""Placeholder CLI"""

from __future__ import annotations

import click

from uparse import __version__


@click.command()
@click.version_option(__version__, prog_name="uparse")
def main() -> None:
    """UniParse CLI."""
    click.echo("uparse")
