"""Schema-bound tool permissions for arbitrary installed native tools.

Descriptions help humans; exact normalized input constraints provide authority.
There is no per-service allowlist. Host escape tools are excluded until sandboxed.
"""

from __future__ import annotations

import copy
import json
import math
import re

from jsonschema import Draft202012Validator
from tenuo import CEL, Exact, OneOf, Range, Wildcard

from .model import GuestsError, canonical, digest
from .presentation import (
    PRESENTATION_SCHEMA,
    merge_presentation,
    normalize_presentation,
)
from .presentation import (
    review_lines as review_lines,
)

HOST_TOOLSETS = frozenset(
    {
        "terminal",
        "file",
        "files",
        "file_tools",
        "memory",
        "skills",
        "cron",
        "delegation",
        "code_execution",
        "execute_code",
        "clarify",
        "session_search",
        "browser",
        "computer_use",
        "desktop_ui",
        "kanban",
        "process",
        "vision",
        "a2a",
        "project",
        "catalog",
        "config",
    }
)
HOST_TOOLS = frozenset(
    {
        "terminal",
        "execute_code",
        "delegate_task",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "memory",
        "session_search",
        "cronjob",
        "skill_manage",
        "manage_connections",
        "tool_call",
        "tool_search",
        "tool_describe",
        "process_manage",
        "computer_use",
        "vision_analyze",
        "browser_navigate",
        "browser_cdp",
        "cronjob_manage",
        "skill_manage",
    }
)
# Hermes adds and renames toolsets (its browser tools are in "browser", "browser-use", ...),
# so a tool is also kept from sharing by what its toolset or name starts with.
HOST_FAMILIES = ("browser", "computer", "desktop", "terminal", "shell", "file", "code_exec")
HOST_NAME_STARTS = ("browser_", "computer_", "desktop_", "terminal", "shell_")


def acts_on_host(name, toolset=None):
    """True for a tool that acts on the owner's machine, browser or Hermes itself."""
    name, family = str(name or "").lower(), str(toolset or "").lower().replace("-", "_")
    return (
        name in HOST_TOOLS
        or family in HOST_TOOLSETS
        or family.startswith(HOST_FAMILIES)
        or name.startswith(HOST_NAME_STARTS)
    )


SECRET_FIELDS = re.compile(
    r"(^|_)(password|secret|api_key|access_token|private_key|credential)(_|$)", re.I
)


def function_schema(schema):
    return schema.get("function", schema)


def validate_schema(schema):
    schema = copy.deepcopy(function_schema(schema))
    name = schema.get("name", "")
    params = schema.get("parameters")
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
        or name.startswith(("guests_", "shared_"))
        or acts_on_host(name)
    ):
        raise GuestsError(
            "This tool manages the host or sharing itself and cannot be shared safely."
        )
    if (
        not isinstance(params, dict)
        or params.get("type") != "object"
        or not isinstance(params.get("properties", {}), dict)
    ):
        raise GuestsError("This tool needs a structured input schema before it can be shared.")

    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value and not str(value["$ref"]).startswith("#/"):
                raise GuestsError("External schema references are not supported for sharing.")
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(params)
    try:
        Draft202012Validator.check_schema(params)
    except Exception as exc:
        raise GuestsError("The tool input schema needs repair before sharing.") from exc
    return {
        "name": name,
        "description": str(schema.get("description", name))[:3000],
        "parameters": params,
    }


def property_schema(params, field):
    """One input's schema with a local reference followed, so that choices and limits kept
    under `$defs` are seen wherever an input's schema is read."""
    spec = (params.get("properties") or {}).get(field) if isinstance(params, dict) else None
    for _ in range(8):  # references to references, but not for ever
        if not isinstance(spec, dict) or not isinstance(spec.get("$ref"), str):
            break
        target = params
        for part in spec["$ref"].lstrip("#").strip("/").split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            target = target.get(part) if isinstance(target, dict) else None
        rest = {k: v for k, v in spec.items() if k != "$ref"}
        if isinstance(target, dict):
            spec = {**target, **rest}
        elif isinstance(target, bool) and not rest:
            spec = target
        else:
            break
    return spec


def describe_inputs(schema):
    """A tool's inputs in a sentence: what each is, its choices or limits, and which are optional."""
    params = schema.get("parameters", {})
    required = set(params.get("required", []))
    parts = []
    for field in params.get("properties", {}):
        spec = property_schema(params, field)
        if not isinstance(spec, dict):  # a schema of plain true or false
            parts.append(
                f"{field} ({'any value' if spec is True else 'not accepted'}"
                f"{'' if field in required else ', optional'})"
            )
            continue
        if isinstance(spec.get("enum"), list):
            what = "one of " + ", ".join(
                json.dumps(v, ensure_ascii=False) for v in spec["enum"][:12]
            )
        elif spec.get("type") in ("integer", "number"):
            what = "whole number" if spec["type"] == "integer" else "number"
            if "minimum" in spec and "maximum" in spec:
                what += f" {spec['minimum']} to {spec['maximum']}"
        elif spec.get("type") == "boolean":
            what = "true or false"
        elif spec.get("type") == "array":
            what = "list"
        else:
            what = "text"
        parts.append(f"{field} ({what}{'' if field in required else ', optional'})")
    return ", ".join(parts) or "none"


def example_entry(schema, given=None, only=None):
    """A complete, valid selection for one tool, as wide as its schema allows. `given` keeps
    rules already chosen; `only` limits it to those inputs, so that nothing is added which
    was not asked for. For showing a model the exact shape; the owner still reviews it."""
    params = schema.get("parameters", {})
    constraints = {}
    for field in params.get("properties", {}):
        spec = property_schema(params, field)
        if only is not None and field not in only:
            continue
        if given and field in given:
            constraints[field] = given[field]
        elif not isinstance(spec, dict):
            # A schema of plain true takes any value; plain false takes none, so that
            # input is left out and stays unavailable.
            if spec is True:
                constraints[field] = {"any": True}
        elif (
            isinstance(spec.get("enum"), list)
            and 1 <= len(spec["enum"]) <= 100
            and not any(isinstance(choice, dict) for choice in spec["enum"])
        ):
            # Object choices cannot be listed in a rule; the tool's own schema still
            # limits them, and the rule below says so explicitly.
            constraints[field] = {"one_of": list(spec["enum"])}
        elif spec.get("type") in ("integer", "number") and "minimum" in spec and "maximum" in spec:
            constraints[field] = {"range": [spec["minimum"], spec["maximum"]]}
        else:
            constraints[field] = {"any": True}
    return {"name": schema.get("name"), "constraints": constraints}


def _how_to_fix(name, schema, rules, requested=None):
    """The corrected entry to send again: the inputs that were asked for (`requested`, or
    those in `rules`) and the required ones, with the rules in `rules` kept. An optional
    input nobody asked for is not added: correcting a mistake must not widen the access."""
    fields = schema["parameters"].get("properties", {})
    known = {k: v for k, v in rules.items() if k in fields}
    asked = set(rules if requested is None else requested) & set(fields)
    wanted = asked | set(schema["parameters"].get("required", []))
    return (
        f" Inputs of {name}: {describe_inputs(schema)}."
        ' Each input needs one rule: {"equals": value}, {"one_of": [values]}, {"range": [low, high]}'
        ' or {"any": true} when any value is acceptable. Narrow this and send it again: '
        + json.dumps(example_entry(schema, known, only=wanted), ensure_ascii=False)
    )


def normalize_rule(rule):
    if not isinstance(rule, dict) or len(rule) != 1:
        raise GuestsError(
            "Each input needs an exact value, allowed values, numeric range, or explicit any-value permission."
        )
    kind, value = next(iter(rule.items()))
    if kind == "equals":
        if isinstance(value, dict):
            raise GuestsError(
                "This SDK cannot pin a whole object input. Use a tool with separate resource fields, or explicitly review unrestricted access to that input."
            )
        canonical(value)
    elif kind == "one_of":
        if not isinstance(value, list) or not 1 <= len(value) <= 100:
            raise GuestsError("Choose between one and 100 allowed values.")
        if any(isinstance(x, dict) for x in value):
            raise GuestsError(
                "Allowed object values cannot be expressed by this SDK. Choose a tool with structured resource fields or review the broader input explicitly."
            )
        value = sorted({canonical(x): x for x in value}.values(), key=canonical)
    elif kind == "range":
        if (
            not isinstance(value, list)
            or len(value) != 2
            or any(
                isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x)
                for x in value
            )
            or value[0] > value[1]
        ):
            raise GuestsError("An input range needs a finite minimum and maximum.")
    elif kind != "any" or value is not True:
        raise GuestsError("This input constraint is not supported.")
    return {kind: value}


def permits(rule, value):
    kind, allowed = next(iter(rule.items()))
    if kind == "any":
        return True
    if kind == "equals":
        return canonical(value) == canonical(allowed)
    if kind == "one_of":
        return canonical(value) in {canonical(x) for x in allowed}
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and allowed[0] <= value <= allowed[1]
    )


def subset(child, parent):
    ck, cv = next(iter(child.items()))
    pk, pv = next(iter(parent.items()))
    if pk == "any":
        return True
    if ck == "equals":
        return permits(parent, cv)
    if ck == "one_of":
        return all(permits(parent, x) for x in cv)
    return ck == pk == "range" and pv[0] <= cv[0] <= cv[1] <= pv[1]


def normalize_tools(requested, resolver, *, parent=None):
    # No fixed count: what one person's access can hold is checked when it is signed.
    if not isinstance(requested, list) or len(requested) > 2000:
        raise GuestsError("Choose the tools this person may use.")
    result = []
    parents = {p["name"]: p for p in parent or []}
    for item in requested:
        if (
            not isinstance(item, dict)
            or set(item) - {"name", "constraints", "presentation"}
            or not isinstance(item.get("name"), str)
        ):
            raise GuestsError("Choose a discovered tool and its input permissions.")
        name = item["name"]
        schema = resolver(name)
        if not schema:
            raise GuestsError(f"The tool {name} is unavailable. Refresh discovery before sharing.")
        schema = validate_schema(schema)
        fields = schema["parameters"].get("properties", {})
        rules = item.get("constraints", {})
        if not isinstance(rules, dict):
            raise GuestsError(
                f"The constraints of {name} must be an object." + _how_to_fix(name, schema, {})
            )
        unknown = sorted(set(rules) - set(fields))
        if unknown:
            raise GuestsError(
                f"{name} has no input called {', '.join(unknown)}."
                + _how_to_fix(name, schema, rules)
            )
        if any(SECRET_FIELDS.search(k) for k in rules):
            raise GuestsError("Share service capabilities, not credentials or signing material.")
        try:
            constraints = {k: normalize_rule(v) for k, v in sorted(rules.items())}
        except GuestsError as exc:
            sound = {}
            for field, rule in rules.items():
                try:  # the example to retry with keeps every rule that was fine
                    sound[field] = normalize_rule(rule)
                except GuestsError:
                    pass
            raise GuestsError(str(exc) + _how_to_fix(name, schema, sound, rules)) from exc
        missing = set(schema["parameters"].get("required", [])) - set(constraints)
        if missing:
            raise GuestsError(
                f"{name} needs a rule for each required input; missing: {', '.join(sorted(missing))}."
                + _how_to_fix(name, schema, rules)
            )
        for field, rule in constraints.items():
            kind, value = next(iter(rule.items()))
            samples = [value] if kind == "equals" else (value if kind == "one_of" else [])
            field_schema = fields[field]
            for sample in samples:
                try:
                    Draft202012Validator(schema["parameters"]).evolve(schema=field_schema).validate(
                        sample
                    )
                except Exception as exc:
                    raise GuestsError(
                        f"The selected value for {field} does not match {name}'s input schema."
                    ) from exc
            if kind == "range":
                # The input's type may sit behind a reference, or be a list of types.
                resolved = property_schema(schema["parameters"], field)
                types = resolved.get("type") if isinstance(resolved, dict) else None
                types = set(types) if isinstance(types, list) else {types}
                if not types & {"number", "integer"}:
                    raise GuestsError("Numeric limits apply only to numeric inputs.")
        entry = {
            "name": name,
            "constraints": constraints,
            "schema": schema,
            "schema_digest": digest(schema),
        }
        if "presentation" in item:
            entry["presentation"] = merge_presentation({}, item["presentation"])
        if parent is not None:
            p = parents.get(name)
            if (
                not p
                or p["schema_digest"] != entry["schema_digest"]
                or not any(
                    (
                        (not p.get("alternatives") or set(constraints) == set(option))
                        and all(
                            k in option and subset(v, option[k]) for k, v in constraints.items()
                        )
                    )
                    for option in rule_options(p)
                )
            ):
                raise GuestsError("A delegated task cannot expand its parent's tool permissions.")
            if p.get("alternatives"):
                entry["alternatives"] = [constraints]
                entry["parent_expression"] = grouped_expression(p)
        existing = next((e for e in result if e["name"] == name), None)
        if existing is None:
            result.append(entry)
        else:
            if existing["schema_digest"] != entry["schema_digest"]:
                raise GuestsError("This tool changed during discovery. Refresh its permissions.")
            if parent is not None and not existing.get("alternatives"):
                raise GuestsError("Choose one operation rule for this subtask.")
            if "presentation" in entry:
                existing["presentation"] = merge_presentation(
                    existing.get("presentation", {}), entry["presentation"]
                )
            options = rule_options(existing) + rule_options(entry)
            existing["alternatives"] = sorted(
                {canonical(v): v for v in options}.values(), key=canonical
            )
            existing.pop("constraints", None)
    for entry in result:
        if "presentation" in entry:
            entry["presentation"] = normalize_presentation(entry["presentation"], entry)
    return sorted(result, key=lambda t: t["name"])


def rule_options(entry):
    return entry.get("alternatives") or [entry["constraints"]]


def grouped_expression(entry):
    """Compile complete argument clauses using the SDK's existing CEL constraint.

    A singleton request list keeps CEL's value binding available for a whole map.
    Every field is required and the map size closes each alternative independently.
    Expressions are generated from validated typed rules, never supplied as code.
    """
    alternatives = []
    for option in rule_options(entry):
        clauses = [f"value[0].size() == {len(option)}"]
        for field, rule in option.items():
            key = canonical(field)
            ref = "value[0][" + key + "]"
            clauses.append(key + " in value[0]")
            kind, value = next(iter(rule.items()))
            if kind == "equals":
                clauses.append(ref + " == " + canonical(value))
            elif kind == "one_of":
                clauses.append(ref + " in " + canonical(value))
            elif kind == "range":
                clauses.append(
                    f"({canonical(value[0])} <= {ref} && {ref} <= {canonical(value[1])})"
                )
        alternatives.append("(" + " && ".join(clauses) + ")")
    expression = "value.size() == 1 && (" + " || ".join(alternatives) + ")"
    if entry.get("parent_expression"):
        expression = "(" + entry["parent_expression"] + ") && (" + expression + ")"
    return expression


def compile_constraints(entry):
    if entry.get("alternatives"):
        return {"request": CEL(grouped_expression(entry))}
    result = {}
    for name, rule in entry["constraints"].items():
        kind, value = next(iter(rule.items()))
        if kind == "equals":
            result[name] = (
                Exact(value)
                if isinstance(value, str)
                else Range(value, value)
                if isinstance(value, (int, float)) and not isinstance(value, bool)
                else CEL("value == " + canonical(value))
            )
        elif kind == "one_of":
            result[name] = (
                OneOf(value)
                if all(isinstance(x, str) for x in value)
                else CEL("value in " + canonical(value))
            )
        elif kind == "range":
            result[name] = Range(*value)
        else:
            result[name] = Wildcard()
    return result


def validate_call(entry, args, resolver):
    current = resolver(entry["name"])
    if not current or digest(validate_schema(current)) != entry["schema_digest"]:
        raise GuestsError(
            "This tool changed or disconnected. The owner needs to review its access again."
        )
    if not isinstance(args, dict):
        raise GuestsError("The tool needs structured inputs.")
    if entry.get("alternatives"):
        for option in rule_options(entry):
            try:
                validate_call(
                    {**entry, "alternatives": None, "constraints": option}, args, resolver
                )
                return
            except GuestsError:
                pass
        raise GuestsError("That action is outside the approved operation rules.")
    rules = entry["constraints"]
    if set(args) - set(rules):
        raise GuestsError("That input is outside the approved access.")
    for field, rule in rules.items():
        if field in args and not permits(rule, args[field]):
            raise GuestsError("That action is outside the approved access.")
    try:
        Draft202012Validator(entry["schema"]["parameters"]).validate(args)
    except Exception as exc:
        raise GuestsError("The request does not match the approved tool inputs.") from exc


def proxy_schema(entry):
    if entry.get("alternatives"):
        branches = []
        for index, option in enumerate(rule_options(entry)):
            branch = proxy_schema({**entry, "alternatives": None, "constraints": option})[
                "parameters"
            ]
            rebase_schema_refs(branch, f"#/properties/request/items/anyOf/{index}/")
            branches.append(branch)
        return {
            **entry["schema"],
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "request": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 1,
                        "description": "One complete allowed operation. Keep its resource and action together.",
                        "items": {"anyOf": branches},
                    }
                },
                "required": ["request"],
            },
        }
    spec = copy.deepcopy(entry["schema"])
    params = spec["parameters"]
    params["additionalProperties"] = False
    params["required"] = sorted(set(params.get("required", [])) | set(entry["constraints"]))
    params["properties"] = {
        k: v for k, v in params.get("properties", {}).items() if k in entry["constraints"]
    }
    for field, rule in entry["constraints"].items():
        kind, value = next(iter(rule.items()))
        if kind == "equals":
            params["properties"][field] = {"allOf": [params["properties"][field], {"const": value}]}
        elif kind == "one_of":
            params["properties"][field] = {"allOf": [params["properties"][field], {"enum": value}]}
        elif kind == "range":
            params["properties"][field] = {
                "allOf": [params["properties"][field], {"minimum": value[0], "maximum": value[1]}]
            }
    return spec


def short_label(description, name="", limit=44):
    """A tool in a few words a person can read on a phone. Tool descriptions are written for
    models and run to paragraphs; this keeps the opening phrase, cut at a word."""
    text = " ".join(str(description or "").split())
    for stop in (". ", ": ", " (", " — ", " - ", "; "):
        head = text.split(stop, 1)[0]
        if 3 <= len(head) < len(text):
            text = head
    text = text.rstrip(".:;,")
    if not text:
        text = str(name).replace("_", " ").strip().capitalize()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def rebase_schema_refs(value, prefix):
    if isinstance(value, dict):
        if "$ref" in value:
            value["$ref"] = prefix + value["$ref"][2:]
        for key, child in value.items():
            if key in {"const", "enum", "default", "examples"}:
                continue
            if key in {
                "properties",
                "patternProperties",
                "$defs",
                "definitions",
                "dependentSchemas",
            }:
                for schema in child.values():
                    rebase_schema_refs(schema, prefix)
            else:
                rebase_schema_refs(child, prefix)
    elif isinstance(value, list):
        for child in value:
            rebase_schema_refs(child, prefix)


def tool_selections(entries):
    return [
        {
            "name": entry["name"],
            "constraints": option,
            **(
                {"presentation": copy.deepcopy(entry["presentation"])}
                if "presentation" in entry
                else {}
            ),
        }
        for entry in entries
        for option in rule_options(entry)
    ]


TOOL_SELECTION_SCHEMA = {
    "type": "array",
    "maxItems": 2000,
    "items": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "presentation": PRESENTATION_SCHEMA,
            "constraints": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 1,
                    "additionalProperties": False,
                    "properties": {
                        "equals": {},
                        "one_of": {"type": "array", "minItems": 1, "maxItems": 100},
                        "range": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "any": {"const": True},
                    },
                },
            },
        },
        "required": ["name", "constraints"],
    },
}


def default_arguments(entry, args):
    if not isinstance(args, dict):
        return args
    args = dict(args)
    for field, rule in entry["constraints"].items():
        if field in args:
            continue
        props = property_schema(entry["schema"]["parameters"], field)
        if not isinstance(props, dict):  # a schema of plain true or false has no default
            props = {}
        if "equals" in rule:
            args[field] = copy.deepcopy(rule["equals"])
        elif "default" in props and permits(rule, props["default"]):
            args[field] = copy.deepcopy(props["default"])
    return args
