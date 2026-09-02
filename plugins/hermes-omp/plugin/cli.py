from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _src() -> Path:
    candidates = [
        Path(__file__).resolve().parent / "src",
        Path(__file__).resolve().parents[1] / "src",
        Path(__file__).resolve().parents[2] / "src",
    ]
    return next(
        (path for path in candidates if (path / "hermes_omp").is_dir()),
        candidates[0],
    )


try:
    from hermes_omp.cli import configure_parser, dispatch_namespace
except ModuleNotFoundError as exc:
    if exc.name != "hermes_omp":
        raise
    source = str(_src())
    if source not in sys.path:
        sys.path.insert(0, source)
    from hermes_omp.cli import configure_parser, dispatch_namespace


def register_cli(parser: argparse.ArgumentParser) -> None:
    configure_parser(parser)
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    command = getattr(args, "command", None)
    if command is None:
        return 2
    command_name = str(command)
    logger.info("hermes-omp command requested: %s", command_name)
    try:
        result = dispatch_namespace(args)
    except Exception:
        logger.exception("hermes-omp command failed: %s", command_name)
        raise
    logger.info("hermes-omp command finished: %s rc=%s", command_name, result)
    return result
