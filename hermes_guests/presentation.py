"""Human permission cards bound to the normalized input rules they describe."""

from __future__ import annotations

import copy
import json
import re

from jsonschema import Draft202012Validator

from .model import GuestsError

LABEL_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 80,
    "pattern": r"^[^\x00-\x1f\x7f-\x9f\u2028-\u202e\u2066-\u2069]+$",
}
PRESENTATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "operation": {
            **LABEL_SCHEMA,
            "description": "Short description of the actual tool operation when it has no action input, for example Read state and details or Read calendar events. Interpret the discovered schema accurately.",
        },
        "action": {
            "type": "string",
            "description": "Name of the approved input containing action choices. Omit when the tool itself defines the action.",
        },
        "resource": {
            "type": "string",
            "description": "Name of the approved input identifying resources, such as calendar_id, repository or library.",
        },
        "namespace": {
            "type": "string",
            "description": "Optional input whose exact value is already the literal prefix of every selected resource ID, before a dot.",
        },
        "labels": {
            "type": "object",
            "maxProperties": 100,
            "additionalProperties": LABEL_SCHEMA,
            "description": "Map approved input names to clear labels. All remaining limits are displayed automatically.",
        },
        "resource_labels": {
            "type": "object",
            "maxProperties": 100,
            "additionalProperties": LABEL_SCHEMA,
            "description": "Map exact selected resource IDs to names verified through discovery. The card also displays each canonical ID.",
        },
    },
    "description": "Identify action/resource input fields and readable labels. Hermes Guests renders their actual constraints. A namespace may be omitted from the card only when its exact value is already present in every displayed resource ID.",
}


def choices(rule):
    values = [rule["equals"]] if "equals" in rule else rule.get("one_of")
    if values and all(
        isinstance(v, str)
        and v.strip()
        and not re.search(r"[\x00-\x1f\x7f-\x9f\u2028-\u202e\u2066-\u2069]", v)
        for v in values
    ):
        return values
    return None


def merge_presentation(first, second):
    """Combine labels from repeated tool rules while rejecting contradictory bindings."""
    validator = Draft202012Validator(PRESENTATION_SCHEMA)
    try:
        validator.validate(first)
        validator.validate(second)
    except Exception as exc:
        raise GuestsError(
            "Use discovered fields and short, single-line permission labels."
        ) from exc
    result = copy.deepcopy(first)
    for key, value in second.items():
        if key in {"labels", "resource_labels"}:
            previous = result.get(key, {})
            if any(name in previous and previous[name] != label for name, label in value.items()):
                raise GuestsError("Use one consistent name for each input and resource.")
            result[key] = {**previous, **value}
        elif key in result and result[key] != value:
            raise GuestsError("Use one consistent action and resource field for each tool.")
        else:
            result[key] = value
    return result


def normalize_presentation(value, entry):
    try:
        Draft202012Validator(PRESENTATION_SCHEMA).validate(value)
    except Exception as exc:
        raise GuestsError(
            "Describe permissions with discovered action/resource fields and short, single-line labels."
        ) from exc
    value = copy.deepcopy(value)
    if "operation" in value and not value["operation"].strip():
        raise GuestsError("Give the operation a readable name.")
    if "operation" in value and ("action" in value or "resource" not in value):
        raise GuestsError("Use operation with a resource and no action input.")
    if any(
        not label.strip()
        for group in ("labels", "resource_labels")
        for label in value.get(group, {}).values()
    ):
        raise GuestsError("Permission labels need a readable name.")
    options = entry.get("alternatives") or [entry["constraints"]]
    fields = {k for option in options for k in option}
    roles = [value[k] for k in ("action", "resource", "namespace") if k in value]
    if (
        len(set(roles)) != len(roles)
        or not set(roles) <= fields
        or not set(value.get("labels", {})) <= fields
    ):
        raise GuestsError("Each permission description must refer to a distinct approved input.")
    if any(not set(roles) <= set(option) for option in options):
        raise GuestsError(
            "Keep the described action and resource present in every permission rule."
        )
    resource = value.get("resource")
    if value.get("resource_labels"):
        permitted = {v for option in options for v in choices(option.get(resource, {})) or []}
        if not set(value["resource_labels"]) <= permitted:
            raise GuestsError("Name only the exact resources selected for this access.")
    namespace = value.get("namespace")
    if namespace:
        for option in options:
            prefix = option[namespace].get("equals")
            resources = choices(option.get(resource, {}))
            if (
                not isinstance(prefix, str)
                or not prefix
                or not resources
                or not all(v.startswith(prefix + ".") for v in resources)
            ):
                raise GuestsError(
                    "A resource category must match the exact prefix of every selected resource ID."
                )
    return value


def joined(values, conjunction="or"):
    if len(values) < 2:
        return "".join(values)
    return (
        ", ".join(values[:-1]) + (", " if len(values) > 2 else " ") + conjunction + " " + values[-1]
    )


def literal(value):
    text = json.dumps(value, ensure_ascii=False)
    return re.sub(
        r"[\u202a-\u202e\u2066-\u2069]", lambda match: "\\u" + format(ord(match[0]), "04x"), text
    )


def constraint_text(rule):
    kind, value = next(iter(rule.items()))
    if kind == "range":
        return f"between {value[0]} and {value[1]}"
    if kind == "any":
        return "any value accepted by this service"
    if kind == "one_of":
        return joined([literal(v) for v in value])
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return literal(value)


def action_words(actions):
    verbs = [re.sub(r"[_\s]+", " ", value).strip() for value in actions]
    if len(set(verbs)) != len(set(actions)):
        verbs = actions
    return joined(verbs)


def review_lines(entry):
    schema = entry["schema"]
    title = schema["description"].strip().split("\n")[0].split(". ")[0].strip().rstrip(".")
    lines = [(title or entry["name"].replace("_", " "))[:120]]
    from .permissions import property_schema  # that module imports this one

    fields = schema["parameters"].get("properties", {})
    display = entry.get("presentation", {})
    options = entry.get("alternatives") or [entry["constraints"]]
    for index, option in enumerate(options):
        if index:
            lines.append("")
        covered = set()
        action = display.get("action")
        resource = display.get("resource")
        actions = choices(option.get(action, {}))
        resources = choices(option.get(resource, {}))
        if resources:
            names = display.get("resource_labels", {})
            named = [
                names[v] + " (" + v + ")" if v in names and names[v] != v else v for v in resources
            ]
            sentence = (action_words(actions) + " — " if actions else "") + joined(named, "and")
            lines.append("• " + sentence[0].upper() + sentence[1:])
            covered.add(resource)
            if actions:
                covered.add(action)
            if display.get("namespace"):
                covered.add(display["namespace"])
        for field in fields:
            if field not in option or field in covered:
                continue
            titled = property_schema(schema["parameters"], field)
            own = field.replace("_", " ")
            given = display.get("labels", {}).get(field)
            if given and not set(own.casefold().split()) <= set(
                re.findall(r"\w+", given.casefold())
            ):
                # A label written for this review is the assistant's wording. Unless it
                # already says the input's own name, that name stays beside it, as a
                # resource's own ID stays beside the name given to it.
                given = given + " (" + own + ")"
            label = (
                given
                # An input's schema may be plain true or false, which has no title, or a
                # reference to where its title is kept.
                or (titled.get("title") if isinstance(titled, dict) else None)
                or own.capitalize()
            )
            lines.append("  • " + label + " — " + constraint_text(option[field]))
        if len(option) > 1 and len(covered) != len(option):
            lines.append("  These choices apply together.")
    return lines


TOOL_DESCRIPTION_LIMIT = 3000  # the whole stored description: Details is the complete view


def own_description(schema):
    """A tool's own description on one line. Descriptions are written for models and can
    run to paragraphs, so a long one is cut at a word and the cut is marked."""
    text = " ".join(str(schema.get("description") or "").split()).rstrip(".")
    if len(text) > TOOL_DESCRIPTION_LIMIT:
        text = text[:TOOL_DESCRIPTION_LIMIT].rsplit(" ", 1)[0].rstrip(",;:.") + "…"
    return text


def review_tools(entries):
    """Organize finite, described resource rules without merging their authority."""
    groups, fallback = {}, []
    for entry in entries:
        display = entry.get("presentation", {})
        resource = display.get("resource")
        options = entry.get("alternatives") or [entry["constraints"]]
        if not resource or any(not choices(option.get(resource, {})) for option in options):
            fallback.append(entry)
            continue
        for option in options:
            for value in choices(option[resource]):
                # The ordinary renderer retains every other argument restriction.
                single = copy.deepcopy(entry)
                single.pop("alternatives", None)
                single["constraints"] = {**option, resource: {"equals": value}}
                rendered = review_lines(single)
                action = display.get("action")
                if action and choices(option.get(action, {})):
                    operation = action_words(choices(option[action]))
                    operation = operation[0].upper() + operation[1:]
                else:
                    operation = display.get("operation") or rendered[0]
                label = display.get("resource_labels", {}).get(value, value)
                heading = label + " (" + value + ")" if label != value else value
                key = (resource, value, label)
                group = groups.setdefault(key, [heading])
                group += ["• " + operation]
                # A tool that describes itself in no words is shown by its name.
                own = own_description(single["schema"]) or rendered[0]
                said = operation.casefold().rstrip(".")
                if said != own.casefold():
                    # The opening sentence alone can omit an effect the owner needs to see.
                    group += ["  Tool: " + own]
                group += rendered[2:]
    lines = []
    for group in groups.values():
        lines += group + [""]
    for entry in fallback:
        lines += review_lines(entry) + [""]
    return lines
