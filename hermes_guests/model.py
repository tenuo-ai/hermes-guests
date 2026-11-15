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
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        amount, seconds = divmod(seconds, size)
        if amount:
            parts.append(f"{amount} {unit}{'s' if amount != 1 else ''}")
    # Every unit, so an approved length is never shown shorter than it is.
    return " ".join(parts) or "0 seconds"


def parse_length(text):
    """A length a person typed ("6 hours", "an hour", "1h30m", "2 days", "1 hour and 30
    minutes") in seconds, or None. The whole message must be a length: alternatives,
    negations, signs or other words are asked again rather than read as some length."""
    import re

    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    text = text.casefold().strip().rstrip(".!")
    text = re.sub(r"\b(an?|one)\s+(?=(min|hour|day|week))", "1 ", text)
    component = r"(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?|w|weeks?)(?![a-z])"
    found = re.findall(component, text)
    rest = re.sub(component, " ", text)
    filler = {"for", "please", "another", "more", "and", "just", "about", "of", "time"}
    leftover = [w for w in re.split(r"[\s,]+", rest) if w and w not in filler]
    if not found or leftover:
        return None
    total = sum(float(amount) * units[unit[0]] for amount, unit in found)
    import math

    if not math.isfinite(total) or total > 10 * 365 * 86400:
        return None  # an absurd number is asked again, not turned into a length
    return int(total) if total >= 60 else None


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


def simple_access(access, catalog, zone="UTC", *, for_owner=True):
    """Access in plain words, the same way for every tool: what it does, on which
    resources, within which limits. Wording written for the review never stands in for
    what a tool is: for the owner, the tool's whole own description follows whenever the
    wording differs. The exact rules are under Details."""
    from .presentation import choices, constraint_text, own_description

    lines = []
    if access.get("chat_only"):
        lines.append("• Chat with the assistant.")
    for entity in access.get("lights", []):  # the lights preset
        lines.append(
            f"• Ask whether {catalog.get(entity, {}).get('name', entity)} is on, or turn it on or off."
        )
    if access.get("research"):
        lines.append("• Search the web or read a public page.")
    for entry in access.get("tools", []):
        display = entry.get("presentation", {})
        resource = display.get("resource")
        names = display.get("resource_labels", {})
        # The whole description, never cut: long cards page in Telegram.
        whole = " ".join(str(entry["schema"].get("description") or "").split()).rstrip(".")
        whole = whole or own_description(entry["schema"]) or str(entry["name"]).replace("_", " ")
        if len(str(entry["schema"].get("description") or "")) >= 3000:
            # Stored descriptions stop at 3000 characters; say so rather than look complete.
            whole += " … [description cut at 3000 characters; check the tool before sharing it]"
        written = (display.get("operation") or "").strip().rstrip(".")
        for option in entry.get("alternatives") or [entry["constraints"]]:
            targets = choices(option.get(resource, {})) if resource else None

            def shown(value):
                # Discovered names are trusted; a written one keeps the real value beside it.
                known = catalog.get(value, {}).get("name")
                label = names.get(value)
                if label and label.casefold() not in {str(value).casefold(), str(known).casefold()}:
                    return f"{label} ({value})"
                return known or str(value)

            text = written or whole
            if targets:
                text += " — " + ", ".join(shown(value) for value in targets)
            limits = [
                (
                    f"{display['labels'][key]} ({key})"
                    if display.get("labels", {}).get(key)
                    and display["labels"][key] != key.replace("_", " ")
                    else key.replace("_", " ")
                )
                + ": "
                + constraint_text(rule)
                for key, rule in option.items()
                if not (targets and key == resource) and "any" not in rule
            ]
            # Inputs left open are named too, briefly and by their real names.
            open_inputs = [
                key.replace("_", " ")
                for key, rule in option.items()
                if "any" in rule and not (targets and key == resource)
            ]
            if open_inputs:
                limits.append("any " + ", ".join(open_inputs))
            if limits:
                text += " (" + "; ".join(limits) + ")"
            line = "• " + text[:1].upper() + text[1:] + "."
            if for_owner and written and written.casefold() != whole.casefold():
                line += "\n  Tool: " + whole
            lines.append(line)
    if access.get("guest_memory"):
        # Signed into the grant, so the plain card says so too.
        lines.append("• Private memory: preferences are remembered between chats.")
    if for_owner and not access.get("chat_only"):
        # What the full review's access details add, briefly, before Approve.
        lines.append("\nAlso: subtasks may use narrower parts of this for up to five minutes.")
        if access.get("tools"):
            lines.append("Tools may return more data, or do more, than their inputs show.")
    return "\n".join(dict.fromkeys(lines)) + "\n\n" + end_text(access, zone)


def guest_view(label, access, catalog, zone="UTC", inviter=None, *, detailed=False):
    """The first message a guest gets. The owner's preview uses the same text."""
    welcome = ("Welcome! " + inviter + " invited you to use this assistant.\n\n") if inviter else ""
    body = (
        review(label, access, catalog, zone)
        if detailed
        else "Here’s what you can do:\n" + simple_access(access, catalog, zone, for_owner=False)
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
