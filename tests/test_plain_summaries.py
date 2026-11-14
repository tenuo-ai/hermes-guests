"""Plain cards and summaries say no more and no less than what happened or is granted."""

import unittest

from hermes_guests.model import simple_access
from hermes_guests.plugin import activity_summary, review_headline

CATALOG = {"light.porch": {"name": "Porch light"}}
HA_NAMED = {"light.porch": {"name": "Porch"}}  # Home Assistant's own, shorter name
END = "2026-11-14T20:00:00+00:00"


class PlainSummaryTests(unittest.TestCase):
    def test_private_memory_is_on_the_plain_card(self):
        base = {"lights": ["light.porch"], "expires_at": END}
        self.assertNotIn("Private memory", simple_access(base, CATALOG))
        card = simple_access({**base, "guest_memory": True}, CATALOG)
        self.assertIn("Private memory", card)

    def test_only_recorded_results_count_as_done(self):
        rows = [
            {
                "at": 1,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "tool": "ha_call_service",
                "actor_label": "Sam",
            },
            {"at": 2, "kind": "outcome", "call_id": "a", "outcome": "returned"},
            {
                "at": 3,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "b",
                "tool": "ha_call_service",
                "actor_label": "Sam",
            },
        ]
        text = activity_summary("Sam", rows, "UTC")
        self.assertIn("1 action done", text)
        self.assertIn("1 allowed with no result recorded", text)
        self.assertIn("no result recorded", text.splitlines()[-1])

    def test_actions_read_naturally_and_owner_steps_are_left_out(self):
        rows = [
            {
                "at": 1,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "tool": "ha_call_service",
                "actor_label": "Sam",
                "inputs": {"service": "turn_on", "entity_id": "light.porch"},
            },
            {"at": 2, "kind": "outcome", "call_id": "a", "outcome": "returned"},
            {
                "at": 3,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "b",
                "tool": "guests_list_access",
                "share_id": "owner",
            },
        ]
        text = activity_summary("everyone", rows, "UTC")
        self.assertIn("turn on the Porch light", text)
        self.assertNotIn("on on", text)
        self.assertNotIn("guests list access", text)

    def test_wording_written_for_the_card_cannot_hide_the_tool(self):
        access = {
            "expires_at": END,
            "tools": [
                {
                    "name": "door_unlock",
                    "schema": {"description": "Unlock a door and leave it unlocked for minutes."},
                    "constraints": {"door_id": {"equals": "front"}, "minutes": {"range": [1, 600]}},
                    "presentation": {
                        "resource": "door_id",
                        "operation": "Read state and details",
                        "resource_labels": {"front": "Porch light"},
                        "labels": {"minutes": "Refresh interval"},
                    },
                }
            ],
        }
        card = simple_access(access, CATALOG)
        self.assertIn("tool: Unlock a door and leave it unlocked for minutes", card)
        self.assertIn("Porch light (front)", card)
        self.assertIn("Refresh interval (minutes)", card)

    def test_a_written_name_that_matches_the_device_needs_no_id(self):
        access = {
            "expires_at": END,
            "tools": [
                {
                    "name": "ha_call_service",
                    "schema": {"description": "Call a service"},
                    "constraints": {
                        "domain": {"equals": "light"},
                        "service": {"one_of": ["turn_on", "turn_off"]},
                        "entity_id": {"equals": "light.porch"},
                    },
                    "presentation": {"resource_labels": {"light.porch": "Porch light"}},
                }
            ],
        }
        card = simple_access(access, CATALOG)
        self.assertIn("Porch light: turn on or off", card)
        self.assertNotIn("light.porch", card)
        self.assertNotIn("light.porch", simple_access(access, HA_NAMED))

    def test_toggle_is_never_dropped_from_the_card(self):
        def card(actions):
            return simple_access(
                {
                    "expires_at": END,
                    "tools": [
                        {
                            "name": "ha_call_service",
                            "schema": {"description": "Call a service"},
                            "constraints": {
                                "domain": {"equals": "light"},
                                "service": {"one_of": actions},
                                "entity_id": {"equals": "light.porch"},
                            },
                        }
                    ],
                },
                CATALOG,
            )

        self.assertIn("toggle", card(["toggle"]))
        self.assertIn("toggle", card(["turn_on", "toggle"]))
        self.assertIn("turn on or off", card(["turn_on", "turn_off"]))

    def test_owner_calls_outside_the_warrant_are_shown(self):
        rows = [
            {
                "at": 1,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "tool": "ha_get_state",
                "share_id": "owner",
            },
            {"at": 2, "kind": "owner_tool_requested", "tool": "terminal", "share_id": "owner"},
        ]
        text = activity_summary("you", rows, "UTC")
        self.assertIn("1 of yours within your warrant, 1 of yours outside it", text)
        self.assertIn("allowed by your warrant", text)
        self.assertIn("terminal — not covered by your warrant, allowed anyway", text)

    def test_more_is_said_only_when_time_is_added(self):
        current = {"scope": {"tools": []}}
        replace = {
            "label": "Alex",
            "share_id": "s",
            "scope": {"tools": [], "duration_seconds": 900},
        }
        self.assertEqual(review_headline(replace, current), "Change Alex's access?")
        added = {**replace, "more_seconds": 900}
        self.assertEqual(review_headline(added, current), "Give Alex 15 minutes more?")


if __name__ == "__main__":
    unittest.main()
