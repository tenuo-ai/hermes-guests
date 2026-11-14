"""Human permission cards retain exact scope across unrelated structured tools."""

import copy
import tempfile
import unittest

from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError
from hermes_guests.permissions import normalize_tools, review_lines, tool_selections
from hermes_guests.store import FileState
from tests.test_grouped_rules import SCHEMA


def device_selection():
    display = {
        "action": "action",
        "resource": "device",
        "namespace": "domain",
        "resource_labels": {
            "lock.door_b": "Door B",
            "light.c": "Light C",
            "light.d": "Light D",
            "light.e": "Light E",
        },
    }
    return [
        {
            "name": "device_control",
            "constraints": {
                "domain": {"equals": category},
                "device": {"one_of": resources},
                "action": {"one_of": actions},
            },
            "presentation": display,
        }
        for category, resources, actions in [
            ("lock", ["lock.door_b"], ["lock", "unlock"]),
            ("light", ["light.c", "light.d", "light.e"], ["turn_on", "turn_off"]),
        ]
    ]


class PermissionCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.effects = []
        self.schemas = {"device_control": copy.deepcopy(SCHEMA)}
        self.state = FileState(self.temp.name)
        self.service = self.restore()

    def restore(self):
        return GuestsService(
            self.state,
            "100",
            tool_resolver=self.schemas.get,
            dispatch=lambda t, a, e: self.effects.append((t, a)) or {"ok": True},
        )

    def prepare(self, tools):
        return self.service.prepare("100", "Alex", {"tools": tools, "duration_seconds": 3600})

    def activate(self, tools):
        p = self.prepare(tools)
        invitation = self.service.approve("100", p["proposal_id"])
        claim = self.service.claim(invitation["token"], "200", "Alex")
        return self.service.accept_claim("100", claim["claim_id"])["share_id"]

    def test_a_resource_the_never_share_list_removes_takes_its_label_with_it(self):
        self.service.never_set("100", "values", "light.d", True)
        p = self.prepare(device_selection())
        self.assertEqual(p["left_out"], ["device_control · device: light.d"])
        self.assertIn("Light C (light.c)", p["text"])
        self.assertIn("Light E (light.e)", p["text"])
        self.assertNotIn("Light D", p["text"])
        self.assertIn("Door B (lock.door_b)", p["text"])  # the other rule set is untouched

    def test_device_card_describes_actions_and_keeps_exact_resource_ids(self):
        text = self.prepare(device_selection())["text"]
        self.assertIn("Door B (lock.door_b)\n• Lock or unlock", text)
        for name in ("Light C (light.c)", "Light D (light.d)", "Light E (light.e)"):
            self.assertIn(name + "\n• Turn off or turn on", text)
        for raw in ("Domain", "Permission group", "device_control", '"turn_on"'):
            self.assertNotIn(raw, text)
        self.assertIn("Lasts 1 hour from approval", text)

    def test_descriptions_survive_restart_renewal_and_permission_checks(self):
        sid = self.activate(device_selection())
        before = self.service.get_access("100", sid)["scope"]
        self.service = self.restore()
        self.assertEqual(self.service.normalize_scope(before, saved=True), before)
        p = self.service.renewal("200", sid)
        self.assertIn("Door B (lock.door_b)", p["text"])
        self.service.approve("100", p["proposal_id"])
        self.service.call(
            "200",
            sid,
            "device_control",
            {"request": [{"domain": "lock", "device": "lock.door_b", "action": "unlock"}]},
        )
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                sid,
                "device_control",
                {"request": [{"domain": "lock", "device": "light.c", "action": "unlock"}]},
            )
        self.assertEqual(len(self.effects), 1)

    def test_resource_review_combines_read_and_control_without_hiding_read_only_resources(self):
        self.schemas["device_state"] = {
            "name": "device_state",
            "description": "Read device state and details.",
            "parameters": {
                "type": "object",
                "properties": {"device": {"type": "string"}},
                "required": ["device"],
            },
        }
        tools = device_selection()
        tools.append(
            {
                "name": "device_state",
                "constraints": {"device": {"one_of": ["light.c", "sensor.temperature"]}},
                "presentation": {
                    "resource": "device",
                    "operation": "Read state and details",
                    "resource_labels": {
                        "light.c": "Light C",
                        "sensor.temperature": "Temperature sensor",
                    },
                },
            }
        )
        text = self.prepare(tools)["text"]
        self.assertEqual(text.count("Light C (light.c)"), 1)
        self.assertIn(
            "Light C (light.c)\n• Turn off or turn on\n  Tool: Control selected devices\n"
            "• Read state and details\n  Tool: Read device state and details",
            text,
        )
        self.assertIn(
            "Temperature sensor (sensor.temperature)\n• Read state and details\n"
            "  Tool: Read device state and details",
            text,
        )
        self.assertNotIn("device_state", text)
        sid = self.activate(tools)
        self.service.call("200", sid, "device_state", {"device": "sensor.temperature"})
        with self.assertRaises(GuestsError):
            self.service.call("200", sid, "device_state", {"device": "sensor.private"})
        self.assertEqual(len(self.effects), 1)

    def test_calendar_card_keeps_dates_private_flag_and_numeric_limit(self):
        self.schemas["calendar_events"] = {
            "name": "calendar_events",
            "description": "Read calendar events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string"},
                    "after": {"type": "string"},
                    "include_private": {"type": "boolean"},
                    "max_events": {"type": "integer"},
                },
                "required": ["calendar_id", "after", "include_private", "max_events"],
            },
        }
        selection = {
            "name": "calendar_events",
            "constraints": {
                "calendar_id": {"equals": "calendar-family"},
                "after": {"equals": "2026-11-10"},
                "include_private": {"equals": False},
                "max_events": {"range": [1, 20]},
            },
            "presentation": {
                "resource": "calendar_id",
                "resource_labels": {"calendar-family": "Family calendar"},
                "labels": {
                    "after": "Events starting",
                    "include_private": "Include private events",
                    "max_events": "Events per request",
                },
            },
        }
        text = self.prepare([selection])["text"]
        for value in (
            "Read calendar events",
            "Family calendar (calendar-family)",
            "2026-11-10",
            "Events starting (after)",  # a label of the assistant's keeps the input's name
            "Include private events — No",  # one that already says it needs nothing more
            "Events per request (max events) — between 1 and 20",
        ):
            self.assertIn(value, text)
        self.assertNotIn("calendar_id", text)

    def test_wording_written_for_the_review_cannot_stand_in_for_what_a_tool_is(self):
        self.schemas["door_unlock"] = {
            "name": "door_unlock",
            "description": "Unlock a door and leave it unlocked for the given number of minutes.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "door_id": {"type": "string"},
                    "minutes": {"type": "integer", "minimum": 1, "maximum": 600},
                },
                "required": ["door_id"],
            },
        }
        self.schemas["media_delete"] = {
            "name": "media_delete",
            "description": "Permanently delete every title in a library.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"library": {"type": "string"}},
                "required": ["library"],
            },
        }
        misleading = [
            {
                "name": "door_unlock",
                "constraints": {"door_id": {"equals": "front"}, "minutes": {"range": [1, 600]}},
                "presentation": {
                    "resource": "door_id",
                    "operation": "Read state and details",
                    "resource_labels": {"front": "Porch light"},
                    "labels": {"minutes": "Refresh interval"},
                },
            },
            {
                "name": "media_delete",
                "constraints": {"library": {"equals": "Movies"}},
                "presentation": {"resource": "library", "operation": "Browse titles"},
            },
        ]
        text = self.prepare(misleading)["text"]
        # Whatever the wording says, the tool's own description and input names are there.
        self.assertIn(
            "• Read state and details\n  Tool: Unlock a door and leave it unlocked for the "
            "given number of minutes",
            text,
        )
        self.assertIn("Refresh interval (minutes) — between 1 and 600", text)
        self.assertIn("• Browse titles\n  Tool: Permanently delete every title in a library", text)
        self.assertIn("Porch light (front)", text)
        # Wording that is the tool's own description is not repeated.
        honest = [dict(misleading[1], presentation={"resource": "library"})]
        self.assertNotIn("Tool:", self.prepare(honest)["text"])

    def test_wording_chosen_for_a_review_brings_the_tools_whole_description_with_it(self):
        self.schemas["media_manage"] = {
            "name": "media_manage",
            "description": "Manage a media library. Permanently delete every title in the\n"
            "selected library.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"library": {"type": "string"}},
                "required": ["library"],
            },
        }

        def card(wording):
            display = {"resource": "library", **({"operation": wording} if wording else {})}
            entry = {
                "name": "media_manage",
                "constraints": {"library": {"equals": "Movies"}},
                "presentation": display,
            }
            return self.prepare([entry])["text"]

        self.assertIn(
            "• Browse titles\n  Tool: Manage a media library. Permanently delete every title "
            "in the selected library\n",
            card("Browse titles"),
        )
        # Even the tool’s opening sentence can leave out what it does.
        for own_words in ("Manage a media library", None):
            with self.subTest(wording=own_words):
                self.assertIn("• Manage a media library\n", card(own_words))
                self.assertIn("Permanently delete every title", card(own_words))
        # A tool that describes itself in no words at all is shown by its name.
        for nothing in ("", "   \n", "."):
            self.schemas["media_manage"]["description"] = nothing
            with self.subTest(description=nothing):
                self.assertIn("• Browse titles\n  Tool: media manage\n", card("Browse titles"))
                for own_words in ("media manage", None):
                    self.assertIn("• media manage\n", card(own_words))
                    self.assertNotIn("Tool:", card(own_words))
                plain = {"name": "media_manage", "constraints": {"library": {"any": True}}}
                self.assertIn("\nmedia manage\n", self.prepare([plain])["text"])
        # A description that runs to paragraphs is cut at a word, and says that it was cut.
        self.schemas["media_manage"]["description"] = "Manage a media library. " + "word " * 200
        shown = next(
            line for line in card("Browse titles").split("\n") if line.startswith("  Tool: ")
        )
        self.assertLessEqual(len(shown), 320)
        self.assertTrue(shown.endswith("word…"), shown[-30:])

    def test_unmentioned_limits_are_always_rendered(self):
        tools = device_selection()
        self.schemas["device_control"]["parameters"]["properties"]["amount"] = {"type": "integer"}
        for entry in tools:
            entry["constraints"]["amount"] = {"range": [1, 7]}
        self.assertEqual(self.prepare(tools)["text"].count("Amount — between 1 and 7"), 4)

    def test_invalid_roles_hidden_categories_and_ungranted_labels_are_rejected(self):
        for bad in (
            {"action": "missing"},
            {"action": "action", "resource": "action"},
            {"resource": "device", "namespace": "action"},
            {"labels": {"missing": "Hidden"}},
            {"summary": "Read only"},
            {"resource": "device", "resource_labels": {"other": "Door B"}},
            {"labels": {"domain": "Type\nEnds tomorrow"}},
        ):
            with self.subTest(bad=bad), self.assertRaises(GuestsError):
                tools = device_selection()
                for item in tools:
                    item["presentation"] = bad
                self.prepare(tools)
        self.assertEqual(self.effects, [])

    def test_blank_or_control_values_fall_back_to_explicit_constraints(self):
        for value in ("", "  ", "lock\nEnds tomorrow", "lock\u202e"):
            entry = normalize_tools(
                [
                    {
                        "name": "device_control",
                        "constraints": {
                            "domain": {"equals": "lock"},
                            "device": {"equals": value},
                            "action": {"equals": "unlock"},
                        },
                        "presentation": {"action": "action", "resource": "device"},
                    }
                ],
                self.schemas.get,
            )[0]
            text = "\n".join(review_lines(entry))
            self.assertIn('Action — "unlock"', text)
            self.assertIn("Device — ", text)
            self.assertNotIn("\nEnds tomorrow", text)
            self.assertNotIn("\u202e", text)

    def test_legacy_selections_remain_valid(self):
        legacy = [
            {
                "name": "device_control",
                "constraints": {
                    "domain": {"equals": "light"},
                    "device": {"equals": "C"},
                    "action": {"one_of": ["off", "on"]},
                },
            }
        ]
        normalized = normalize_tools(legacy, self.schemas.get)
        self.assertEqual(tool_selections(normalized), legacy)
        self.assertIn('Domain — "light"', "\n".join(review_lines(normalized[0])))

    def test_separately_discovered_resource_labels_merge_without_changing_scope(self):
        tools = device_selection()
        tools[0]["presentation"] = {
            **tools[0]["presentation"],
            "resource_labels": {"lock.door_b": "Door B"},
        }
        tools[1]["presentation"] = {
            **tools[1]["presentation"],
            "resource_labels": {"light.c": "Light C", "light.d": "Light D", "light.e": "Light E"},
        }
        text = self.prepare(tools)["text"]
        self.assertIn("Door B (lock.door_b)", text)
        self.assertIn("Light E (light.e)", text)
