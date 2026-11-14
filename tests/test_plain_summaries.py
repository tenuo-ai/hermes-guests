"""Plain cards and summaries say no more and no less than what happened or is granted."""

import unittest

from hermes_guests.model import length_text, simple_access
from hermes_guests.plugin import activity_summary, end_phrase, review_headline

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
        self.assertIn("Tool: Unlock a door and leave it unlocked for minutes", card)
        self.assertIn("Porch light (front)", card)
        self.assertIn("Refresh interval (minutes)", card)
        guest = simple_access(access, CATALOG, for_owner=False)
        self.assertNotIn("Tool:", guest)  # the owner's safeguard, not the guest's

    def test_every_tool_is_described_the_same_way(self):
        def card(domain, service, presentation=None):
            return simple_access(
                {
                    "expires_at": END,
                    "tools": [
                        {
                            "name": "ha_call_service",
                            "schema": {"description": "Call a Home Assistant service"},
                            "constraints": {
                                "domain": {"equals": domain},
                                "service": service,
                                "entity_id": {"equals": "light.porch"},
                            },
                            **({"presentation": presentation} if presentation else {}),
                        }
                    ],
                },
                CATALOG,
            )

        self.assertIn('domain: "automation"', card("automation", {"equals": "reload"}))
        self.assertIn('"toggle"', card("light", {"one_of": ["turn_on", "toggle"]}))
        anything = card("light", {"any": True}, {"operation": "Check whether the light is on"})
        self.assertIn("any service", anything)
        self.assertIn("Tool: Call a Home Assistant service", anything)
        named = {"resource": "entity_id", "resource_labels": {"light.porch": "Porch light"}}
        self.assertIn("— Porch light (", card("light", {"equals": "turn_on"}, named))
        self.assertNotIn("light.porch", card("light", {"equals": "turn_on"}, named))
        self.assertIn(
            "Porch light (light.porch)",
            simple_access(
                {
                    "expires_at": END,
                    "tools": [
                        {
                            "name": "ha_call_service",
                            "schema": {"description": "Call a service"},
                            "constraints": {"entity_id": {"equals": "light.porch"}},
                            "presentation": named,
                        }
                    ],
                },
                HA_NAMED,
            ),
        )

    def test_activity_uses_the_same_words_for_every_tool(self):
        rows = [
            {
                "at": 1,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "tool": "notes_read",
                "actor_label": "Priya",
                "inputs": {"note_id": "n2"},
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
        self.assertIn("Priya: notes read (n2)", text)
        self.assertNotIn("guests list access", text)

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

    def test_a_refusal_before_any_decision_is_reported(self):
        rows = [
            {
                "at": 1,
                "kind": "adapter_blocked",
                "reason": "address-not-public",
                "actor_label": "Sam",
                "tool": "web_extract",
            }
        ]
        text = activity_summary("Sam", rows, "UTC")
        self.assertIn("1 not allowed", text)
        self.assertIn("private addresses aren't allowed", text)

    def test_lengths_are_shown_in_full(self):
        self.assertEqual(length_text(95400), "1 day 2 hours 30 minutes")
        self.assertEqual(length_text(3661), "1 hour 1 minute 1 second")

    def test_a_tools_whole_description_is_on_the_plain_card(self):
        def card(wording):
            display = {"resource": "library", **({"operation": wording} if wording else {})}
            return simple_access(
                {
                    "expires_at": END,
                    "tools": [
                        {
                            "name": "media_manage",
                            "schema": {
                                "description": "Manage a media library. Permanently delete "
                                "every title in the selected library."
                            },
                            "constraints": {"library": {"equals": "Movies"}},
                            "presentation": display,
                        }
                    ],
                },
                {},
            )

        for wording in ("Browse titles", "Manage a media library", None):
            with self.subTest(wording=wording):
                self.assertIn("Permanently delete every title", card(wording))

    def test_a_long_description_is_not_cut(self):
        description = "Browse a media library. " + "Details. " * 50 + "Deletes every title."
        card = simple_access(
            {
                "expires_at": END,
                "tools": [
                    {
                        "name": "media_tool",
                        "schema": {"description": description},
                        "constraints": {"library": {"equals": "Movies"}},
                        "presentation": {"resource": "library", "operation": "Browse titles"},
                    }
                ],
            },
            {},
        )
        self.assertIn("Deletes every title", card)

    def test_a_deletion_is_described_as_asked_for(self):
        rows = [{"at": 1, "kind": "owner_approved", "action": "delete", "subject_label": "Sam"}]
        self.assertIn("You asked to delete the data of Sam", activity_summary("Sam", rows, "UTC"))

    def test_the_end_is_described_as_it_happened(self):
        future = "2099-01-01T00:00:00+00:00"
        ended = {
            "effective_status": "revoked",
            "status": "revoked",
            "revoked_at": 0,
            "scope": {"expires_at": future},
        }
        self.assertIn("you ended it", end_phrase(ended, "UTC"))
        paused = {
            "effective_status": "unavailable",
            "status": "unavailable",
            "scope": {"expires_at": future},
        }
        self.assertIn("was set to end", end_phrase(paused, "UTC"))
        ran_out = {
            "effective_status": "expired",
            "status": "active",
            "scope": {"expires_at": "2000-01-01T00:00:00+00:00"},
        }
        self.assertIn("; ended ", end_phrase(ran_out, "UTC"))

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
