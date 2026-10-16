"""Command line: `run` plays scenarios, `list` shows the built-in ones, `init` writes a first config."""

import argparse
import sys

import billing_e2e


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="billing-e2e", description=billing_e2e.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="play one or more scenarios")
    commands.add_parser("list", help="show the built-in scenarios")
    commands.add_parser("init", help="write a first config and scenario")
    args = parser.parse_args(argv)
    sys.exit(f"`{args.command}` is not implemented yet")
