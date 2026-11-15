import unittest

from hermes_guests import menu
from hermes_guests.permissions import normalize_tools, validate_schema
from hermes_guests.schemas import GUEST_SCHEMAS

NOTES_ADD = {
    "name": "mcp__family_notes__notes_add",
    "description": "Add a note to a folder (recipes, house or shopping).",
    "parameters": {
        "type": "object",
        "properties": {
            "folder": {"type": "string"},
            "title": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["folder", "title", "body"],
    },
}
NOTES_READ = {
    "name": "mcp__family_notes__notes_read",
    "description": "Read one note by its id.",
    "parameters": {
        "type": "object",
        "properties": {"note_id": {"type": "string"}},
        "required": ["note_id"],
    },
}


LIST = {
    "name": "ha_list_entities",
    "description": "List Home Assistant entities. On a large install a bare call can return many.",
    "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}},
}


def tools(*schemas):
    return {s["name"]: validate_schema(s) for s in schemas}


def labels(nodes):
    return [n["label"] for n in nodes]


class ShareMenuTests(unittest.TestCase):
    def test_home_assistant_reads_kind_then_device_then_action(self):
        ha = tools(GUEST_SCHEMAS["ha_get_state"], GUEST_SCHEMAS["ha_call_service"])
        top = menu.build(
            ha,
            lambda name: "homeassistant",
            entities={"light.porch": "Porch light", "switch.fan": "Desk fan"},
            services={"light": ["turn_off", "turn_on"], "switch": ["toggle"]},
        )
        self.assertEqual(labels(top), ["Lights", "Switches"])  # one integration: no extra level
        porch = top[0]["children"][0]
        self.assertEqual(porch["label"], "Porch light")
        self.assertEqual(labels(porch["children"]), ["Check it", "Turn off", "Turn on"])
        self.assertEqual(porch["children"][2]["summary"], "Porch light — turn on")
        on = porch["children"][2]["entry"]
        self.assertEqual(
            on["constraints"],
            {
                "domain": {"equals": "light"},
                "service": {"equals": "turn_on"},
                "entity_id": {"equals": "light.porch"},
            },
        )

    def test_picks_on_several_devices_make_one_valid_rule_set(self):
        ha = tools(GUEST_SCHEMAS["ha_get_state"], GUEST_SCHEMAS["ha_call_service"])
        top = menu.build(
            ha,
            lambda name: "homeassistant",
            entities={"light.porch": "Porch light", "light.desk": "Desk lamp"},
            services={"light": ["turn_off", "turn_on"]},
        )
        picked = [n["entry"] for n in menu.leaves(top) if n["label"] in {"Turn on", "Check it"}]
        # As the plugin sends them: each tool's wording once, on its first rule.
        seen, entries = set(), []
        for entry in picked:
            entry = dict(entry)
            if entry["name"] in seen:
                entry.pop("presentation")
            seen.add(entry["name"])
            entries.append(entry)
        for entry in entries:
            if "presentation" in entry:
                for other in picked:
                    if other["name"] == entry["name"]:
                        entry["presentation"]["resource_labels"].update(
                            other["presentation"]["resource_labels"]
                        )
        result = normalize_tools(entries, GUEST_SCHEMAS.get)
        call = next(e for e in result if e["name"] == "ha_call_service")
        self.assertEqual(len(call["alternatives"]), 2)

    def test_described_values_become_things_with_actions_under_them(self):
        found = tools(NOTES_ADD, NOTES_READ)
        shape = menu.check_shape(
            NOTES_ADD["name"],
            found[NOTES_ADD["name"]],
            {
                "input": "folder",
                "operation": "Add items",
                "values": [
                    {"value": "shopping", "label": "Shopping"},
                    {"value": "garage", "label": "Garage"},  # not listed by the tool
                ],
            },
        )
        self.assertEqual(shape["values"], {"shopping": "Shopping"})
        top = menu.build(found, lambda name: None, shapes={NOTES_ADD["name"]: shape})
        self.assertEqual(labels(top), ["Shopping", "Read one note by its id"])
        add = top[0]["children"][0]
        self.assertEqual(add["summary"], "Shopping — add items")
        self.assertEqual(add["entry"]["constraints"]["folder"], {"equals": "shopping"})
        self.assertEqual(add["entry"]["constraints"]["title"], {"any": True})

    def test_a_shape_naming_an_unknown_input_or_unlisted_values_is_ignored(self):
        found = tools(NOTES_ADD)
        schema = found[NOTES_ADD["name"]]
        for proposal in (
            {"input": "colour", "operation": "Add", "values": [{"value": "house", "label": "H"}]},
            {"input": "folder", "operation": "Add", "values": [{"value": "hous", "label": "H"}]},
            {"input": "folder", "operation": "", "values": [{"value": "house", "label": "H"}]},
        ):
            self.assertIsNone(menu.check_shape(NOTES_ADD["name"], schema, proposal))

    def test_integrations_are_the_top_level_when_there_are_several(self):
        found = tools(NOTES_ADD, GUEST_SCHEMAS["web_search"])
        top = menu.build(found, lambda name: "web" if name == "web_search" else None)
        self.assertEqual(labels(top), ["Family notes", "Web"])
        self.assertEqual(labels(menu.leaves(top[0]["children"])), ["Add a note to a folder"])

    def test_button_labels_fit_a_phone(self):
        long = dict(NOTES_READ, description="List available resources from the MCP server.")
        top = menu.build(tools(long), lambda name: None)
        self.assertLessEqual(len(top[0]["label"]), menu.BUTTON)
        self.assertEqual(menu.check_label("Read a note"), "Read a note")
        self.assertIsNone(menu.check_label("A label much too long to fit a phone button"))
        found = tools(GUEST_SCHEMAS["ha_get_state"])
        ha = menu.home_assistant(
            dict(found, ha_list_entities=validate_schema(LIST)), {"light.a": "A"}, {}
        )
        # Without a model, every tool's label is its own description, cut to fit.
        self.assertIn("List Home Assistant…", labels(ha))

    def test_an_enum_is_used_without_a_model(self):
        schema = {
            "name": "calendar_read",
            "description": "Read events.",
            "parameters": {
                "type": "object",
                "properties": {"calendar": {"type": "string", "enum": ["work", "family"]}},
                "required": ["calendar"],
            },
        }
        shape = menu.enum_shape("calendar_read", validate_schema(schema))
        self.assertEqual(shape["values"], {"work": "Work", "family": "Family"})


if __name__ == "__main__":
    unittest.main()
