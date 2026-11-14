"""Plain cards and summaries say no more and no less than what happened or is granted."""

import unittest

from hermes_guests.model import simple_access
from hermes_guests.plugin import activity_summary, review_headline

CATALOG = {"light.porch": {"name": "Porch light"}}
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
