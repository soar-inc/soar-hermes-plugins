"""Soar Meet browser participant for Hermes."""

import sys


def register(ctx):
    if sys.platform != "linux":
        return

    from .cli import dispatch, register_cli

    ctx.register_cli_command(
        name="soar-meet",
        help="Soar Meet participant (auth, join, status, transcript, say, stop)",
        setup_fn=register_cli,
        handler_fn=dispatch,
    )
