"""Every use of Hermes internals that has no published plugin surface, in one place.

Each is imported only when called. Everything else uses ctx or the published plugin API."""

import contextlib
from pathlib import Path

from .model import GuestsError


def own_profile(*, saved=False):
    """Run the enclosed work on this profile's behalf.

    Once one Hermes process serves several profiles, Hermes refuses any read of a
    credential or setting that is not made on a profile's behalf. Hermes says whose
    behalf for its own turns. Hermes Guests' Telegram handlers, upkeep and guest-call broker
    run outside those turns, so they say it themselves. Nothing is bound on a host
    with a single profile or on a Hermes without this.

    Where a profile is already chosen it is kept. A context kept from earlier (saved=True:
    a background loop, a context saved when the runner or broker was made) also holds
    that profile's settings as they were when it was kept, so they are read again for
    the same profile; otherwise a key changed since would not be seen until a restart."""
    try:
        from agent.secret_scope import (
            current_secret_scope,
            current_secret_scope_home,
            is_multiplex_active,
        )
        from hermes_constants import get_hermes_home, get_hermes_home_override
        from tui_gateway.launch_profile_policy import launch_profile_scope_if_multiplexed
    except ImportError:
        return contextlib.nullcontext()
    if current_secret_scope() is not None:
        if not saved:
            return contextlib.nullcontext()
        # Hermes leaves the profile unnamed for the one the process started as, and keeps
        # that profile's folder in the same context: read from there, because the
        # process's HERMES_HOME may since name another.
        named = current_secret_scope_home()
        return _settings_read(named or get_hermes_home(), named)
    if not is_multiplex_active():
        return contextlib.nullcontext()
    begun_for = get_hermes_home_override()
    if begun_for:  # work already begun for a profile, with none of its settings read yet
        return _settings_read(begun_for, str(begun_for))
    return launch_profile_scope_if_multiplexed()


@contextlib.contextmanager
def _settings_read(home, named):
    """Read the settings of the profile in home; named is how Hermes marks whose they are."""
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from tui_gateway.launch_profile_policy import owner_secret_scope

    token = set_secret_scope(owner_secret_scope(home), profile_home=named)
    try:
        yield
    finally:
        reset_secret_scope(token)


def in_own_profile(function, /, *args, **kwargs):
    """Call a function on this profile's behalf from a context kept from earlier."""
    with own_profile(saved=True):
        return function(*args, **kwargs)


def toolset_selection(value):
    """The toolset list a saved ``platform_toolsets.<platform>`` value encodes, or None, read
    the way Hermes reads it: a list as is, or an older build's ``'["a", "b"]'`` string."""
    import ast

    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip().startswith("["):
        try:
            parsed = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            return None
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return None


def configured_zone():
    """The time zone this profile is set to, as Hermes resolves it: ``HERMES_TIMEZONE`` when
    the process serves one profile, else the profile's ``timezone`` setting; "" when unset."""
    import os

    try:
        from agent.secret_scope import is_multiplex_active

        if not is_multiplex_active():
            zone = os.getenv("HERMES_TIMEZONE", "").strip()
            if zone:
                return zone
        from hermes_cli.config import load_config_readonly

        zone = (load_config_readonly() or {}).get("timezone", "")
        return zone.strip() if isinstance(zone, str) else ""
    except Exception:
        return ""


# Hermes has no published surface for the following; each is kept here, behind one
# function, imported only when called and failing closed if Hermes changes it.


def approved_telegram_users():
    """Telegram users approved through Hermes's own pairing: [{"user_id", ...}]."""
    from gateway.pairing import PairingStore

    return PairingStore().list_approved("telegram")


def auto_provider():
    """The provider Hermes picks when the owner left the choice to Hermes."""
    from hermes_cli.auth import resolve_provider

    return resolve_provider("auto")


def guest_profile(name, **create):
    """The folder of the Hermes profile called name, created with the given settings when
    it doesn't exist yet: (folder, created)."""
    from hermes_cli.profiles import create_profile, get_profile_dir, profile_exists

    if profile_exists(name):
        return Path(get_profile_dir(name)), False
    return Path(create_profile(name, **create)), True


def guest_profile_dir(name):
    from hermes_cli.profiles import get_profile_dir

    return Path(get_profile_dir(name))


def delete_guest_profile(name):
    from hermes_cli.profiles import delete_profile

    return delete_profile(name, yes=True)


def native_agent():
    """Hermes's own agent class, for the guest's turn in its own process."""
    from run_agent import AIAgent

    return AIAgent


def plugin_manager():
    """Hermes's plugin manager, so the guest's process loads this plugin's guest tools."""
    from hermes_cli.plugins import get_plugin_manager

    return get_plugin_manager()


def native_memory_tool():
    """Hermes's memory tool and its schema, for the guest's own private memory."""
    from tools.memory_tool import MEMORY_SCHEMA, memory_tool

    return MEMORY_SCHEMA, memory_tool


def enable_telegram_management_tools():
    """Add only this plugin to an existing Telegram selection after owner confirmation."""
    from hermes_cli.config import read_raw_config, save_config

    raw = read_raw_config()
    platforms = raw.get("platform_toolsets") or {}
    selected = toolset_selection(platforms.get("telegram"))
    if selected is None or "hermes_guests" in selected:
        return
    save_config(
        {"platform_toolsets": {"telegram": [*selected, "hermes_guests"]}},
        merge_existing=True,
    )


def native_definitions(*, collapsed=False):
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.tools_config import _get_platform_tools
        from model_tools import get_tool_definitions
    except ImportError as exc:
        raise GuestsError(
            "This Hermes version lacks the tool-selection APIs Hermes Guests needs. Update Hermes, then retry setup."
        ) from exc
    config = load_config_readonly()
    # Use Hermes's resolver, including plugin defaults, saved disables, and MCP
    # aliases. Reading platform_toolsets directly silently misses native plugins.
    enabled = sorted(_get_platform_tools(config, "telegram"))
    return get_tool_definitions(
        enabled_toolsets=enabled,
        disabled_toolsets=(config.get("agent") or {}).get("disabled_toolsets"),
        quiet_mode=True,
        skip_tool_search_assembly=not collapsed,
    )
