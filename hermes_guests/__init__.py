"""Hermes Guests: local per-person access with native confirmations."""

import os
from pathlib import Path


def _register_skill(ctx):
    ctx.register_skill(
        "guest-access",
        Path(__file__).parent / "skills" / "guest-access" / "SKILL.md",
        description="Share, inspect, expand, reduce, renew, revoke and recover per-person access through native owner confirmation.",
    )


def register(ctx):
    required = ("register_tool", "register_hook")
    if os.environ.get("HERMES_GUESTS_WORKER") != "1":
        required += (
            "register_skill",
            "register_telegram_handler",
            "spawn_task",
            "register_command",
        )
    if any(not callable(getattr(ctx, name, None)) for name in required):
        raise RuntimeError(
            "Update Hermes: Hermes Guests needs native tools, hooks, skills, Telegram handlers, and supervised plugin tasks."
        )
    if os.environ.get("HERMES_GUESTS_WORKER") == "1":
        from .worker import register_worker

        register_worker(ctx)
    else:
        from .plugin import register as register_host

        register_host(ctx)
