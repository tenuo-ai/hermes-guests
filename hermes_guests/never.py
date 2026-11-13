"""The owner's never-share list: tools and values no guest gets, whatever is asked.

It is a backstop under everything else. A model's draft, a button, a preset, a request
for more, and a grant made before the list changed all pass through it, and it can only
remove. There are three kinds of entry:

- a tool, which is left out of every grant;
- a value, which is refused in any input of any tool;
- a value for one tool, or for one input of one tool, which is refused only there.

A listed value is taken out of the choices a grant allows where the tool names its
choices, and any guest call that carries it is refused when the call is admitted, which
also covers free-text inputs and grants made earlier.
"""

from __future__ import annotations

import unicodedata

from .model import GuestsError

MIN_VALUE = 3  # shorter values would match almost anything
MAX_VALUE = 80
MAX_ITEMS = 200
_DROPPED = {"Cc", "Cf", "Co", "Cs", "Cn"}
# Characters that are shown as nothing although they are not format characters: fillers,
# variation selectors and the like (Unicode's Default_Ignorable_Code_Point). A word with
# one of these inside reads the same, so it has to compare the same.
_HIDDEN = dict.fromkeys(
    cp
    for first, last in (
        (0x00AD, 0x00AD),
        (0x034F, 0x034F),
        (0x061C, 0x061C),
        (0x115F, 0x1160),
        (0x17B4, 0x17B5),
        (0x180B, 0x180F),
        (0x200B, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x206F),
        (0x3164, 0x3164),
        (0xFE00, 0xFE0F),
        (0xFEFF, 0xFEFF),
        (0xFFA0, 0xFFA0),
        (0xFFF0, 0xFFF8),
        (0x1BCA0, 0x1BCA3),
        (0x1D173, 0x1D17A),
        (0xE0000, 0xE0FFF),
    )  # fmt: skip
    for cp in range(first, last + 1)
)
ONLY_OWNER = (
    "Only the owner changes that list, in /guests_setup or by confirming a change in this chat."
)


def fold(text):
    """Text as it is compared: one spelling for look-alike forms, no invisible characters,
    no case, single spaces."""
    text = unicodedata.normalize("NFKC", str(text).translate(_HIDDEN)).translate(_HIDDEN)
    kept = "".join(
        " " if c.isspace() else c
        for c in text
        if c.isspace() or unicodedata.category(c) not in _DROPPED
    )
    return " ".join(kept.casefold().split())


def _carried(value, depth=0):
    """Every piece of text or number inside a value, folded, with the form a web address
    would carry it in."""
    if depth > 12:
        raise TooDeep
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _carried(key, depth + 1)
            yield from _carried(inner, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for inner in value:
            yield from _carried(inner, depth + 1)
    else:
        text = fold(value)
        if "%" in text or "+" in text:
            from urllib.parse import unquote_plus

            text += " " + fold(unquote_plus(str(value)))
        yield text


MAX_CHOICES = 100  # as many values as one rule may list


class TooDeep(Exception):
    """An input nested further than it is inspected."""


UNCHECKED = "(an input nested too deeply to check)"


class NeverShare:
    def __init__(self, tools=(), values=(), scoped=()):
        self.tools = [str(t) for t in tools]
        self.values = [str(v) for v in values]
        self.scoped = [
            {"tool": str(r["tool"]), "field": str(r.get("field") or ""), "value": str(r["value"])}
            for r in scoped
            if isinstance(r, dict) and r.get("tool") and r.get("value")
        ]
        self._tools = {fold(t) for t in self.tools}
        self._values = [(fold(v), v) for v in self.values if len(fold(v)) >= MIN_VALUE]
        self._scoped = [
            (fold(r["tool"]), r["field"], fold(r["value"]), r["value"])
            for r in self.scoped
            if len(fold(r["value"])) >= MIN_VALUE
        ]

    def __bool__(self):
        return bool(self._tools or self._values or self._scoped)

    def blocks_tool(self, name):
        return fold(name) in self._tools

    def word_locations(self, value, tool="", field=""):
        """The exact locations a removal would affect, with an empty pair for the global rule."""
        word = fold(value)
        locations = {
            (rule["tool"], rule["field"])
            for rule in self.scoped
            if fold(rule["value"]) == word
            and (not tool or (fold(rule["tool"]) == fold(tool) and rule["field"] == field))
        }
        if not tool and any(fold(entry) == word for entry in self.values):
            locations.add(("", ""))
        return sorted(locations)

    def _find(self, listed, value):
        try:
            texts = list(_carried(value))
        except TooDeep:
            return UNCHECKED  # what cannot be inspected is not let through

        return next((shown for folded, shown in listed if any(folded in t for t in texts)), None)

    def blocked_in(self, value):
        """The value listed for every tool that is found anywhere inside `value`, or None."""
        return self._find(self._values, value) if self._values else None

    def _for(self, tool, field=None):
        """Values listed for this tool: for all of its inputs, and for `field` if given."""
        name = fold(tool)
        return [
            (folded, shown)
            for rule_tool, rule_field, folded, shown in self._scoped
            if rule_tool == name and (not rule_field or field is None or rule_field == field)
        ]

    def blocked_for(self, tool, args):
        """The listed value a call to `tool` carries, or None: a value listed for every tool,
        for this tool, or for the input of this tool it arrives in."""
        found = self.blocked_in(args)
        if found or not self._scoped:
            return found
        whole = [(f, s) for rt, rf, f, s in self._scoped if rt == fold(tool) and not rf]
        found = self._find(whole, args) if whole else None
        if found or not isinstance(args, dict):
            return found
        for rule_tool, rule_field, folded, shown in self._scoped:
            if rule_tool == fold(tool) and rule_field and rule_field in args:
                if self._find([(folded, shown)], args[rule_field]):
                    return shown
        return None

    def _blocked_choice(self, tool, field, value):
        return self.blocked_in(value) or self._find(self._for(tool, field), value)

    def lock(self, entry, schema):
        """One requested tool with listed values taken out of what it may allow.
        Returns (entry, what was left out). Raises when nothing allowed would remain."""
        name = entry.get("name")
        rules = entry.get("constraints")
        if not (self._values or self._scoped) or not isinstance(rules, dict):
            return entry, []
        locked, left_out = {}, []
        for field, rule in rules.items():
            choices = None
            if isinstance(rule, dict) and "equals" in rule:
                found = self._blocked_choice(name, field, rule["equals"])
                if found:
                    raise GuestsError(
                        f"“{found}” is on your never-share list, so {name} cannot be shared with {field} "
                        f"set to it. {ONLY_OWNER}"
                    )
            elif isinstance(rule, dict) and isinstance(rule.get("one_of"), list):
                choices = rule["one_of"]
            elif isinstance(rule, dict) and rule.get("any") is True:
                from .permissions import property_schema

                spec = property_schema((schema or {}).get("parameters") or {}, field)
                spec = spec if isinstance(spec, dict) else {}
                # Only choices for the whole input. A list input's choices are per element,
                # which a rule here cannot express; those are checked on each call.
                # Choices that are objects cannot be written as a rule either; those too
                # stay as they are and are checked on each call.
                listed = spec.get("enum")
                if (
                    isinstance(listed, list)
                    and not any(isinstance(v, dict) for v in listed)
                    and any(self._blocked_choice(name, field, v) for v in listed)
                ):
                    choices = listed
            if choices is not None:
                kept = [v for v in choices if not self._blocked_choice(name, field, v)]
                gone = [v for v in choices if self._blocked_choice(name, field, v)]
                if gone:
                    if not kept:
                        raise GuestsError(
                            f"Every choice for {field} of {name} is on your never-share list."
                        )
                    left_out += [f"{name} · {field}: {v}" for v in gone]
                    if len(kept) <= MAX_CHOICES:
                        rule = {"one_of": kept}
                    # More choices than a rule can list: the rule stays as reviewed, the
                    # review still says what is left out, and each call is checked.
            locked[field] = rule
        entry = dict(entry, constraints=locked)
        shown = entry.get("presentation")
        if isinstance(shown, dict) and isinstance(shown.get("resource_labels"), dict):
            # A name for a resource that was just taken out would describe something the
            # access no longer has; the same labels are given on each rule set of a tool.
            field = shown.get("resource")
            kept = {
                resource: label
                for resource, label in shown["resource_labels"].items()
                if not self._blocked_choice(name, field, resource)
            }
            if kept != shown["resource_labels"]:
                shown = {k: v for k, v in shown.items() if k != "resource_labels"}
                if kept:
                    shown["resource_labels"] = kept
                entry["presentation"] = shown
        return entry, left_out


def checked_item(kind, item):
    """A tool name or value as it is stored, or a GuestsError saying why not."""
    if kind not in {"tools", "values", "scoped"}:
        raise GuestsError("Choose a tool or a value.")
    shown = " ".join(
        "".join(
            c
            for c in str(item).translate(_HIDDEN)
            if c.isspace() or unicodedata.category(c) not in _DROPPED
        ).split()
    )
    if kind != "tools" and not MIN_VALUE <= len(fold(shown)) <= MAX_VALUE:
        raise GuestsError(
            f"A value to never share is between {MIN_VALUE} and {MAX_VALUE} characters."
        )
    if kind == "tools" and not 1 <= len(shown) <= 64:
        raise GuestsError("Choose one of your tools.")
    return shown
