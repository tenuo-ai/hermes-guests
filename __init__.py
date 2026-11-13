"""Git-installed Hermes plugin entrypoint."""

import os


def register(ctx):
    if os.environ.get("HERMES_GUESTS_WORKER") == "1":
        # The native worker owns the canonical module's in-memory payload.
        from hermes_guests import register as register_plugin
    else:
        from .hermes_guests import register as register_plugin

    register_plugin(ctx)


__all__ = ["register"]
