"""Hermes public plugin entry point."""
from .cli import dispatch, register_cli


def register(ctx) -> None:
    ctx.register_cli_command(
        name="omp",
        help="Durable Oh My Pi session supervision",
        setup_fn=register_cli,
        handler_fn=dispatch,
        description="Create and supervise durable OMP RPC sessions.",
    )
