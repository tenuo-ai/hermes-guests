"""Single checked seam for the same Telegram tool selection Hermes uses itself."""

import contextlib

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


def enable_telegram_management_tools():
    """Add only this plugin to an existing Telegram selection after owner confirmation."""
    from hermes_cli.config import read_raw_config, save_config
    from hermes_cli.toolset_validation import parse_platform_toolsets_value

    raw = read_raw_config()
    platforms = raw.get("platform_toolsets") or {}
    selected = parse_platform_toolsets_value(platforms.get("telegram"))
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
