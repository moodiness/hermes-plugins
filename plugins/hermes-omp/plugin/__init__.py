"""Hermes public plugin entry point."""
from __future__ import annotations

import logging
from typing import Any

from .cli import dispatch, register_cli

logger = logging.getLogger(__name__)


def register(ctx: Any) -> None:
    """Plugin entry point — wires the documented public CLI surface."""
    ctx.register_cli_command(
        name="omp",
        help="Durable Oh My Pi session supervision",
        setup_fn=register_cli,
        handler_fn=dispatch,
        description="Create and supervise durable OMP RPC sessions.",
    )
    logger.debug("hermes-omp: registered `hermes omp`")
