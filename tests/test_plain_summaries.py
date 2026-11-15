"""Plain cards and summaries say no more and no less than what happened or is granted."""

import unittest

from hermes_guests.model import length_text, simple_access
from hermes_guests.plugin import activity_summary, activity_text, end_phrase, review_headline

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
        mcp = [
            {
                "at": 1,
                "kind": "owner_tool_requested",
                "tool": "mcp__family_notes__notes_search",
                "share_id": "owner",
            }
        ]
        self.assertIn("You: mcp family notes notes search", activity_summary("you", mcp, "UTC"))
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

    def test_a_long_description_leads_with_its_opening_and_points_to_details(self):
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

        # Short: shown whole, so its effects are on the card before Approve.
        self.assertIn("Permanently delete every title", card("Browse titles"))
        self.assertIn("Permanently delete every title", card(None))
        self.assertNotIn("Details", card(None))

    def test_a_long_description_points_to_details(self):
        long = "Call a service to control a device. " + "Model guidance. " * 30
        card = simple_access(
            {
                "expires_at": END,
                "tools": [{"name": "control", "schema": {"description": long}, "constraints": {}}],
            },
            {},
        )
        self.assertIn("Call a service to control a device", card)
        self.assertIn("More about this tool under Details.", card)
        self.assertNotIn("Model guidance", card)

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

    def test_another_guests_activation_is_not_hidden_by_an_approval(self):
        rows = [
            {
                "at": 10,
                "kind": "owner_approved",
                "action": "revoke",
                "share_id": "a",
                "subject_label": "Alex",
            },
            {"at": 11, "kind": "access_activated", "share_id": "b", "subject_label": "Blair"},
        ]
        self.assertIn("Access started for Blair", activity_summary("everyone", rows, "UTC"))

    def test_the_owner_card_says_what_else_the_grant_allows(self):
        access = {
            "expires_at": END,
            "tools": [
                {
                    "name": "notes_read",
                    "schema": {"description": "Read a note"},
                    "constraints": {"note_id": {"equals": "n1"}},
                }
            ],
        }
        owner = simple_access(access, CATALOG)
        self.assertIn("subtasks may use narrower parts", owner)
        self.assertIn("Tools may return more data", owner)
        self.assertNotIn("subtasks", simple_access(access, CATALOG, for_owner=False))

    def test_refusals_after_access_ended_are_reported_in_both_views(self):
        rows = [
            {
                "at": 1,
                "kind": "admission_denied",
                "reason": "inactive-or-wrong-identity",
                "actor_label": "Sam",
                "tool": "notes_read",
            }
        ]
        self.assertIn("1 not allowed", activity_summary("Sam", rows, "UTC"))
        self.assertIn("refused (access had ended)", activity_text("Sam", rows, "UTC"))

    def test_a_cut_description_says_so_without_changing_the_saved_schema(self):
        from hermes_guests.permissions import validate_schema

        long = {
            "name": "media_tool",
            "description": "x" * 3100 + " Deletes everything.",
            "parameters": {"type": "object", "properties": {}},
        }
        saved = validate_schema(long)
        self.assertEqual(len(saved["description"]), 3000)  # same fingerprint as before
        card = simple_access(
            {
                "expires_at": END,
                "tools": [{"name": "media_tool", "schema": saved, "constraints": {}}],
            },
            {},
        )
        self.assertIn("description was cut at 3000 characters", card)

    def test_a_result_without_its_decision_is_still_reported(self):
        rows = [{"at": 1, "kind": "outcome", "call_id": "x", "outcome": "returned"}]
        text = activity_summary("Sam", rows, "UTC")
        self.assertIn("1 action done", text)
        self.assertIn("An earlier action finished", text)

    def test_two_devices_with_one_name_never_read_the_same(self):
        catalog = {"light.bedroom": {"name": "Lamp"}, "light.kitchen": {"name": "Lamp"}}

        def card(targets):
            return simple_access(
                {
                    "expires_at": END,
                    "tools": [
                        {
                            "name": "ha_call_service",
                            "schema": {"description": "Call a service"},
                            "constraints": {"entity_id": {"one_of": targets}},
                            "presentation": {"resource": "entity_id"},
                        }
                    ],
                },
                catalog,
            )

        self.assertIn("Lamp (light.bedroom)", card(["light.bedroom"]))
        self.assertNotEqual(card(["light.bedroom"]), card(["light.bedroom", "light.kitchen"]))
        preset = simple_access({"expires_at": END, "lights": ["light.bedroom"]}, catalog)
        self.assertIn("Lamp (light.bedroom)", preset)

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
