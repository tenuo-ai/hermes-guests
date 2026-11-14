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


def when(value, zone="UTC"):
    """A moment as people read it, in their own clock: "Fri 13 Nov, 11:52 PM EST"."""
    from zoneinfo import ZoneInfo

    at = datetime.fromtimestamp(timestamp(value), ZoneInfo(zone))
    return at.strftime("%a %d %b, %I:%M %p %Z").replace(" 0", " ")


def length_text(seconds):
    """A length of time in words: "15 minutes", "2 hours 30 minutes", "7 days"."""
    seconds = int(seconds)
    parts = []
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {unit}{'s' if amount != 1 else ''}")
    return " ".join(parts[:2]) or f"{seconds} seconds"


def parse_length(text):
    """A length a person typed ("6 hours", "an hour", "1h30m", "2 days") in seconds, or
    None. Only plain amounts and units; anything else is asked again."""
    import re

    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = re.sub(r"\b(an?|one)\s+(?=(min|hour|day|week))", "1 ", text.casefold())
    total, found = 0, False
    # A number must stand on its own: "-6 hours" or ".5 hours" is asked again rather
    # than read as 6 or 5 hours.
    if re.search(r"(^|[^\d])[-+.]\d", text):
        return None
    for amount, unit in re.findall(
        r"(?<![\d.])(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?)(?![a-z])",
        text,
    ):
        total += float(amount) * units[unit[0]]
        found = True
    return int(total) if found and total >= 60 else None


def end_text(access, zone="UTC"):
    """When access ends. A reviewed length starts when the owner approves."""
    if access.get("until_ended"):
        return "No end date. Access continues until the owner ends it."
    if access.get("duration_seconds"):
        import time

        # Worked out when the card is shown, not when the review was prepared: a card
        # shown again hours later still says where access would end if approved now.
        return (
            "Lasts "
            + length_text(access["duration_seconds"])
            + " from approval (until "
            + when(time.time() + access["duration_seconds"], zone)
            + " if approved now)"
        )
    return "Ends " + when(access["expires_at"], zone)


HA_ACTION_WORDS = {
    "media_play": "play",
    "media_pause": "pause",
    "media_stop": "stop",
    "media_play_pause": "play or pause",
    "media_next_track": "skip to the next track",
    "media_previous_track": "go back a track",
    "volume_up": "turn the volume up",
    "volume_down": "turn the volume down",
    "volume_set": "set the volume",
    "volume_mute": "mute",
    "play_media": "choose what plays",
    "select_source": "choose the source",
    "lock": "lock",
    "unlock": "unlock",
    "open_cover": "open",
    "close_cover": "close",
}


def device_name(entity_id):
    """A readable name for a Home Assistant device id: "media_player.bedroom" -> "the
    Bedroom media player"."""
    domain, _, name = str(entity_id).partition(".")
    if not name:
        return str(entity_id)
    kind = {
        "media_player": "speaker",
        "light": "light",
        "lock": "lock",
        "switch": "switch",
        "cover": "cover",
        "climate": "thermostat",
        "fan": "fan",
    }.get(domain, domain.replace("_", " "))
    return "the " + name.replace("_", " ").title() + " " + kind


def simple_access(access, catalog, zone="UTC"):

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
            # Wording written for the review never stands in for what the tool is. Outside
            # the Home Assistant tools, whose words this code supplies, the tool's whole own
            # description is shown whenever anything else is: a written label, its action
            # choices, or an opening sentence that leaves out what else it does.
            from .presentation import own_description

            # The whole description, not cut: long ones page in Telegram.
            whole = " ".join(str(entry["schema"].get("description") or "").split()).rstrip(".") or (
                own_description(entry["schema"]) or str(entry["name"]).replace("_", " ")
            )
            # For the Home Assistant tools this code supplies every word, from the rules
            # themselves; wording written for the review is not used for them.
            written = None if ha_tool else display.get("operation")
            description = written or whole
            if entry["name"] == "ha_get_state":
                description = "Check the state"
            elif entry["name"] == "ha_call_service" and not actions:
                description = "Use any Home Assistant service"
            if actions:
                aliases = HA_ACTION_WORDS if ha_tool else {}
                spoken = [aliases.get(value, value) for value in actions]
                if ha_tool and set(actions) <= {"turn_on", "turn_off"}:
                    spoken = [
                        "turn " + " or ".join(w for w in ("on", "off") if "turn_" + w in actions)
                    ]
                description = action_words(spoken)
            tool_note = (
                " (tool: " + whole + ")"
                if not ha_tool and description.strip().rstrip(".").casefold() != whole.casefold()
                else ""
            )
            if targets:
                names = display.get("resource_labels", {})

                def shown(value):
                    # Names from discovery are trusted. A name the model wrote that says
                    # something else keeps the real value beside it.
                    known = catalog.get(value, {}).get("name") or (
                        device_name(value) if ha_tool else value
                    )
                    written = names.get(value)
                    same = {str(value), str(known)}
                    if ha_tool:  # "Porch light" for light.porch is the id read out
                        same.add(device_name(value))
                    if not written or written.casefold() in {
                        name.removeprefix("the ").casefold() for name in same
                    } | {name.casefold() for name in same}:
                        return known
                    return written + " (" + str(value) + ")"

                devices = ", ".join(shown(value) for value in targets)
                if ha_tool and actions:  # "Bedroom speaker: play, pause or stop"
                    devices = devices.removeprefix("the ")
                    description = devices[:1].upper() + devices[1:] + ": " + description
                else:
                    connector = " of " if entry["name"] == "ha_get_state" else " on "
                    description += connector + devices
            elif entry["name"] == "ha_get_state" and option.get(resource) == {"any": True}:
                description += " of any Home Assistant device or sensor"
            limits = [
                (
                    display["labels"][key] + " (" + key + ")"
                    if display.get("labels", {}).get(key)
                    and display["labels"][key] != key.replace("_", " ")
                    else key.replace("_", " ")
                )
                + ": "
                + constraint_text(rule)
                for key, rule in option.items()
                if key
                not in {
                    resource if targets else None,
                    action if actions else None,
                    display.get("namespace") if targets else None,
                    "domain" if ha_tool and targets else None,  # implied by the device
                }
                and "any" not in rule
            ]
            if limits:
                description += " (" + "; ".join(limits) + ")"
            description += tool_note
            lines.append("• " + description[:1].upper() + description[1:].rstrip(".") + ".")
    if access.get("guest_memory"):
        # Signed into the grant, so the plain card says so too.
        lines.append("• Private memory: preferences are remembered between chats.")
    return "\n".join(dict.fromkeys(lines)) + "\n\n" + end_text(access, zone)


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
        end_text(access, zone),
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
