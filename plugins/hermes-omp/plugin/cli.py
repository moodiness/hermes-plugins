from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _src() -> Path:
    candidates = [Path(__file__).resolve().parents[1] / "src", Path(__file__).resolve().parents[2] / "src"]
    return next((x for x in candidates if (x / "hermes_omp").is_dir()), candidates[0])


def register_cli(parser: argparse.ArgumentParser) -> None:
    source = str(_src())
    if source not in sys.path:
        sys.path.insert(0, source)
    from hermes_omp.cli import build_parser
    template = build_parser()
    # The public Hermes API supplies our top-level parser; build the same
    # subcommands directly by copying parser actions through a parent parser.
    parser.description = template.description
    subs = parser.add_subparsers(dest="command", required=True)
    for name, child in next(a for a in template._actions if isinstance(a, argparse._SubParsersAction)).choices.items():
        target = subs.add_parser(name, help=child.description, parents=[child], add_help=False)
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    source = str(_src())
    if source not in sys.path:
        sys.path.insert(0, source)
    from hermes_omp.cli import main
    argv = []
    # Hermes argparse already parsed the command; serialize public fields back
    # into the standalone CLI deterministically.
    command = getattr(args, "command", None)
    if command is None:
        return 2
    argv.append(command)
    positional = {"name", "message"}
    for key, value in vars(args).items():
        if key in {"func", "command"} or value in (None, False, [], ""):
            continue
        if key in positional:
            argv.append(str(value)); continue
        option = "--" + key.replace("_", "-")
        if value is True: argv.append(option)
        elif isinstance(value, list):
            for item in value: argv += [option, str(item)]
        else: argv += [option, str(value)]
    return main(argv)
