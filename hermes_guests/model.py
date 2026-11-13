"""Typed scope shared by confirmation rendering and warrant compilation."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone

CHAT_ONLY_CAPABILITY = "__hermes_guests_chat_only__"
GUEST_MEMORY_CAPABILITY = "__hermes_guests_guest_memory__"
MAX_ACCESS_SECONDS = 30 * 86400


class GuestsError(ValueError):
    pass


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise GuestsError("Choose a finite end time.")
        return float(value)
    if not isinstance(value, str):
        raise GuestsError("Give an exact end time including its timezone.")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuestsError("Give an exact ISO end time including its timezone.") from exc
    if dt.tzinfo is None:
        raise GuestsError("The end time needs a timezone.")
    return dt.timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


STANDING_SECONDS = 60 * 86400  # how far one signed grant reaches for access with no end date
STANDING_RENEW = 30 * 86400  # issue the next one when less than this is left


def scope(value, *, now, catalog, parent=None):
    if not isinstance(value, dict) or set(value) - {
        "lights",
        "chat_only",
        "guest_memory",
        "research",
        "tools",
        "expires_at",
        "duration_seconds",
        "until_ended",
    }:
        raise GuestsError(
            "Choose discovered tools, optional resource presets, and a time limit: duration_seconds, "
            "expires_at, or until_ended for access with no end date."
        )
    guest_memory = value.get("guest_memory", False)
    if type(guest_memory) is not bool or (parent is not None and guest_memory):
        raise GuestsError("Private guest memory is available only to the guest, not subtasks.")
    chat_only = value.get("chat_only", False)
    if type(chat_only) is not bool:
        raise GuestsError("Choose whether this is chat-only access.")
    if chat_only and (value.get("lights") or value.get("research") or value.get("tools")):
        raise GuestsError("Chat-only access cannot include tools.")
    lights = value.get("lights", [])
    research = value.get("research", False)
    if (
        not isinstance(lights, list)
        or len(lights) > 20
        or not all(isinstance(x, str) for x in lights)
    ):
        raise GuestsError("Choose individual lights from discovery.")
    if type(research) is not bool:
        raise GuestsError("Research must be enabled or disabled.")
    lights = sorted(set(lights))
    for entity in lights:
        if entity not in catalog or not entity.startswith("light.") or catalog[entity].get("group"):
            raise GuestsError(
                "Choose a discovered individual light; groups and scenes aren't supported."
            )
    standing = value.get("until_ended", False)
    if type(standing) is not bool:
        raise GuestsError("until_ended must be true or false.")
    if standing and parent:
        raise GuestsError("A delegated task always has an end time.")
    if standing:
        if "duration_seconds" in value:
            raise GuestsError("Choose either a time limit or access with no end date, not both.")
        # No end date for the person. The signed grant itself is short-lived and is issued
        # again while access stays active; this is how far the current one reaches.
        end = now + STANDING_SECONDS
    elif "duration_seconds" in value:
        seconds = value["duration_seconds"]
        if (
            "expires_at" in value
            or type(seconds) is not int
            or not 6 <= seconds <= MAX_ACCESS_SECONDS
        ):
            raise GuestsError(
                "Choose either a duration of 6 seconds to 30 days or an exact end time."
            )
        end = now + seconds
    else:
        end = timestamp(value.get("expires_at"))
    if not standing and not now + 5 < end <= now + MAX_ACCESS_SECONDS:
        raise GuestsError(
            "Choose an end time between a minute and 30 days from now, or until_ended for access with no end date."
        )
    if not chat_only and not lights and not research and not value.get("tools"):
        raise GuestsError("Choose at least one capability, or revoke access.")
    if parent:
        if not set(lights) <= set(parent["lights"]) or (research and not parent["research"]):
            raise GuestsError("A delegated task cannot have more access than its parent.")
        if end > timestamp(parent["expires_at"]):
            raise GuestsError("A delegated task cannot outlive its parent.")
    result = {
        **({"chat_only": True} if chat_only else {}),
        "lights": lights,
        "research": research,
        "tools": value.get("tools", []),
        "expires_at": iso(end),
    }
    if guest_memory:
        result["guest_memory"] = True
    if standing:
        result["until_ended"] = True
    return result


def simple_access(access, catalog, zone="UTC"):
    from zoneinfo import ZoneInfo

    from .permissions import short_label
    from .presentation import action_words, choices, constraint_text

    lines = []
    if access.get("chat_only"):
        lines.append("• Chat with the assistant.")
    for entity in access.get("lights", []):
        lines.append(
            f"• Ask whether {catalog.get(entity, {}).get('name', entity)} is on, or turn it on or off."
        )
    if access.get("research"):
        lines.append("• Search the web or read a public page.")
    for entry in access.get("tools", []):
        display = entry.get("presentation", {})
        for option in entry.get("alternatives") or [entry["constraints"]]:
            ha_tool = entry["name"] in {"ha_get_state", "ha_call_service"}
            resource = display.get("resource") or ("entity_id" if ha_tool else None)
            action = display.get("action") or (
                "service" if entry["name"] == "ha_call_service" else None
            )
            targets = choices(option.get(resource, {}))
            actions = choices(option.get(action, {}))
            description = display.get("operation") or short_label(
                entry["schema"].get("description"), entry["name"]
            )
            if entry["name"] == "ha_get_state":
                description = "Check the state"
            if actions:
                aliases = (
                    {
                        "media_play": "play",
                        "media_pause": "pause",
                        "media_stop": "stop",
                    }
                    if ha_tool
                    else {}
                )
                description = action_words([aliases.get(value, value) for value in actions])
            if targets:
                names = display.get("resource_labels", {})
                connector = " of " if entry["name"] == "ha_get_state" else " on "
                description += connector + ", ".join(names.get(value, value) for value in targets)
            elif entry["name"] == "ha_get_state" and option.get(resource) == {"any": True}:
                description += " of any Home Assistant device or sensor"
            limits = [
                (display.get("labels", {}).get(key) or key.replace("_", " "))
                + ": "
                + constraint_text(rule)
                for key, rule in option.items()
                if key
                not in {
                    resource if targets else None,
                    action if actions else None,
                    display.get("namespace") if targets else None,
                }
                and "any" not in rule
            ]
            if limits:
                description += " (" + "; ".join(limits) + ")"
            lines.append("• " + description[:1].upper() + description[1:].rstrip(".") + ".")
    if access.get("until_ended"):
        end = "Access continues until the owner ends it."
    else:
        at = datetime.fromtimestamp(timestamp(access["expires_at"]), ZoneInfo(zone))
        end = "Ends " + at.strftime("%a %d %b, %I:%M %p %Z").replace(", 0", ", ")
    return "\n".join(dict.fromkeys(lines)) + "\n\n" + end


def guest_view(label, access, catalog, zone="UTC", inviter=None, *, detailed=False):
    """The first message a guest gets. The owner's preview uses the same text."""
    welcome = ("Welcome! " + inviter + " invited you to use this assistant.\n\n") if inviter else ""
    body = (
        review(label, access, catalog, zone)
        if detailed
        else "Here’s what you can do:\n" + simple_access(access, catalog, zone)
    )
    return welcome + body + "\n\nSend a message to start."


def review(label, access, catalog, zone="UTC"):
    from zoneinfo import ZoneInfo

    end = datetime.fromtimestamp(timestamp(access["expires_at"]), ZoneInfo(zone))
    lines = [f"Access for {label}", ""]
    if access.get("chat_only"):
        lines += ["Chat only. No enabled actions or subagents."]
    if access["lights"]:
        lines.append("Home Assistant")
        for entity in access["lights"]:
            lines.append(
                f"• Read {catalog.get(entity, {}).get('name', entity)}'s state and details; turn it on and off."
            )
    if access["research"]:
        lines += ["Web research", "• Search the public web and read public pages."]
    if access.get("tools"):
        from .presentation import review_tools

        lines += ["Allowed actions", ""]
        lines += review_tools(access["tools"])
    if lines[-1] != "":
        lines.append("")
    lines += [
        "No end date. Access continues until the owner ends it."
        if access.get("until_ended")
        else "Ends " + end.strftime("%a %d %b %Y, %H:%M:%S %Z (UTC%z)"),
        "",
        "Access details",
        "• Scope is limited to the capabilities and inputs above.",
        *(
            []
            if access.get("chat_only")
            else ["• Subtasks may use narrower access for up to five minutes."]
        ),
        *(
            ["• Private guest memory remembers preferences across conversations."]
            if access.get("guest_memory")
            else []
        ),
        "• The owner can inspect tool attempts and results.",
        *(
            ["• Tools may return broader data or have effects their input schemas do not describe."]
            if access.get("tools")
            else []
        ),
    ]
    return "\n".join(lines)


def narrow(parent, requested, now, catalog):
    value = dict(requested)
    value.setdefault("expires_at", iso(min(timestamp(parent["expires_at"]), now + 300)))
    return scope(value, now=now, catalog=catalog, parent=parent)
