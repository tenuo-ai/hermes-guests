"""Lenient intake for the owner's access request.

A model drafts the request. Small differences in shape (the time limit one level up, a
bare value where a rule object was expected) are tidied here into the one normalized form
before trusted validation. Tidying never adds permission the request did not state: an
input with no rule still fails validation, and the owner reviews the normalized result.
"""

from __future__ import annotations

import copy

from .model import GuestsError

TIME_FIELDS = ("duration_seconds", "expires_at", "until_ended")
SCOPE_FIELDS = ("tools", "lights", "research", "chat_only") + TIME_FIELDS
RULE_KINDS = ("equals", "one_of", "range", "any")
_EQUALS = ("const", "value", "is", "eq", "exactly")
_ONE_OF = ("enum", "in", "allowed", "values", "any_of", "oneOf")
_NAME_WORDS = ("name", "tool", "tool_name")
_RULE_WORDS = ("constraints", "inputs", "arguments", "rules")
_LOW = ("min", "minimum", "from", "gte")
_HIGH = ("max", "maximum", "to", "lte", "at_most")


def _gather(scope, key, value):
    """Put a field written somewhere else into the scope. The same value twice is one
    statement; two different values are refused, never settled by which came first."""
    if key in scope and scope[key] != value:
        raise GuestsError(
            key + " is given more than once with different values. Give it once, inside scope."
        )
    scope[key] = value


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def tidy_rule(rule, field_schema):
    """One input's rule in canonical form, or the value unchanged if it cannot be read."""
    kind = field_schema.get("type")
    if isinstance(rule, dict):
        keys = set(rule)
        if len(keys) == 1 and keys <= set(RULE_KINDS):
            return rule
        if keys and keys <= set(_LOW + _HIGH):
            lows = [rule[k] for k in _LOW if k in rule]
            highs = [rule[k] for k in _HIGH if k in rule]
            if any(len(side) > 1 and not all(_number(v) for v in side) for side in (lows, highs)):
                return rule  # several bounds that cannot be compared: left for validation
            # Several words for the same bound all hold, so the narrowest one is kept.
            low = max(lows) if lows else field_schema.get("minimum")
            high = min(highs) if highs else field_schema.get("maximum")
            if low is not None and high is not None:
                return {"range": [low, high]}
            return rule
        if len(keys) == 1:
            key, value = next(iter(rule.items()))
            if key in _EQUALS:
                return {"equals": value}
            if key in _ONE_OF and isinstance(value, list):
                return {"one_of": value}
        return rule
    if isinstance(rule, list):
        return {"one_of": rule}
    if isinstance(rule, bool):
        # A bare true on anything but a yes/no input is not a rule. It is left for
        # validation to refuse, which shows how to say "any value" explicitly.
        return {"equals": rule} if kind == "boolean" else rule
    if isinstance(rule, (str, int, float)):
        return {"equals": rule}
    return rule


def tidy(args, resolver):
    """`guests_prepare_access` arguments with the scope gathered and each rule canonical."""
    if not isinstance(args, dict):
        raise GuestsError("Send the person's name and a scope with the tools they may use.")
    args = copy.deepcopy(args)
    scope = args.get("scope")
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise GuestsError("The scope must be an object with tools and a time limit.")
    for key in SCOPE_FIELDS:  # written one level up: same meaning
        if key in args:
            _gather(scope, key, args.pop(key))
    for alias in ("user", "guest", "who", "name"):  # the person, under another word
        if alias in args and isinstance(args[alias], str):
            args.setdefault("person", args[alias])
            args.pop(alias)
    extra = set(args) - {
        "person",
        "share_id",
        "action",
        "scope",
        "invitation_seconds",
        "add_seconds",
        "guest_time_zone",
    }
    if extra:
        raise GuestsError(
            "Unknown fields: "
            + ", ".join(sorted(extra))
            + ". Use person, action, share_id, invitation_seconds, add_seconds, "
            "guest_time_zone and scope; "
            "inside scope use tools and one of duration_seconds, expires_at or until_ended."
        )
    tools = scope.get("tools")
    if isinstance(tools, dict):
        tools = (
            [tools]
            if "name" in tools
            else [{"name": k, "constraints": v} for k, v in tools.items()]
        )
    if isinstance(tools, list):
        tidied = []
        for item in tools:
            if isinstance(item, str):
                item = {"name": item}
            if not isinstance(item, dict):
                tidied.append(item)
                continue
            unknown = set(item) - set(_NAME_WORDS + _RULE_WORDS) - {"presentation"}
            if unknown:
                raise GuestsError(
                    "Unknown fields in a tool entry: "
                    + ", ".join(sorted(map(str, unknown)))
                    + ". Use name, constraints and presentation."
                )
            names = [item[k] for k in _NAME_WORDS if k in item]
            stated = [item[k] for k in _RULE_WORDS if k in item]
            if any(n != names[0] for n in names) or any(s != stated[0] for s in stated):
                raise GuestsError(
                    "A tool entry gives its name or its rules more than once with different "
                    "values. Use name and constraints, once each."
                )
            name = names[0] if names else None
            rules = stated[0] if stated else {}
            spec = resolver(name) if isinstance(name, str) else None
            fields = (
                ((spec or {}).get("parameters") or {}).get("properties", {})
                if isinstance(spec, dict)
                else {}
            )
            if rules == "any" or rules == {"any": True}:
                rules = {
                    field: {"any": True} for field in fields
                }  # the whole tool, stated explicitly
            if isinstance(rules, dict):
                canonical = {}
                for field, rule in rules.items():
                    if field in TIME_FIELDS and field not in fields:
                        value = (
                            rule.get("equals")
                            if isinstance(rule, dict) and set(rule) == {"equals"}
                            else rule
                        )
                        _gather(scope, field, value)  # a time limit filed under a tool
                        continue
                    from .permissions import property_schema

                    params = (spec.get("parameters") or {}) if isinstance(spec, dict) else {}
                    one = property_schema(params, field)
                    canonical[field] = tidy_rule(rule, one if isinstance(one, dict) else {})
                rules = canonical
            entry = {"name": name, "constraints": rules}
            if "presentation" in item:
                entry["presentation"] = item["presentation"]  # checked by trusted validation
            tidied.append(entry)
        scope["tools"] = tidied
    seconds = scope.get("duration_seconds")
    if isinstance(seconds, float) and seconds.is_integer():
        scope["duration_seconds"] = int(seconds)
    elif isinstance(seconds, str) and seconds.strip().isdigit():
        scope["duration_seconds"] = int(seconds.strip())
    if scope.get("until_ended") is False:
        scope.pop("until_ended")
    args["scope"] = scope
    return args
