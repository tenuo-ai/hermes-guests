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
        self.assertIn("· door_unlock", card)  # the real tool, whatever the wording says
        # The resource first, then the words, then the real tool.
        self.assertIn("• Porch light — read state and details · door_unlock", card)
        self.assertNotIn("minutes", card)  # exact limits are under Details
        guest = simple_access(access, CATALOG, for_owner=False)
        self.assertNotIn("door_unlock", guest)  # the owner's safeguard, not the guest's

    def test_cards_stay_in_plain_words(self):
        access = {
            "expires_at": END,
            "tools": [
                {
                    "name": "ha_call_service",
                    "schema": {"description": "Call a Home Assistant service"},
                    "constraints": {
                        "domain": {"equals": "light"},
                        "service": {"one_of": ["turn_on", "turn_off"]},
                        "entity_id": {"equals": "light.porch"},
                    },
                    "presentation": {
                        "operation": "Turn the porch light on or off",
                        "resource": "entity_id",
                        "resource_labels": {"light.porch": "Porch light"},
                    },
                }
            ],
        }
        owner = simple_access(access, CATALOG)
        self.assertIn("• Porch light — turn the porch light on or off · ha_call_service", owner)
        for jargon in ("light.porch", "domain", "turn_on", '"'):
            self.assertNotIn(jargon, owner)
        guest = simple_access(access, CATALOG, for_owner=False)
        self.assertIn("• Porch light — turn the porch light on or off.", guest)
        self.assertNotIn("ha_call_service", guest)
        anything = {**access["tools"][0], "constraints": {"entity_id": {"any": True}}}
        self.assertIn("• Any entity id — ", simple_access({**access, "tools": [anything]}, CATALOG))

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

    def test_the_card_speaks_in_the_assistants_words_with_the_tools_name(self):
        from hermes_guests.model import review

        description = "Manage a media library. Permanently delete every title in it."
        access = {
            "expires_at": END,
            "lights": [],
            "research": False,
            "tools": [
                {
                    "name": "media_manage",
                    "schema": {
                        "description": description,
                        "parameters": {
                            "type": "object",
                            "properties": {"library": {"type": "string"}},
                        },
                    },
                    "constraints": {"library": {"equals": "Movies"}},
                    "presentation": {"resource": "library", "operation": "Browse titles"},
                }
            ],
        }
        card = simple_access(access, {})
        self.assertIn("• Movies — browse titles · media_manage", card)
        self.assertNotIn("Permanently delete", card)  # the official text is under Details
        self.assertIn("Tool: media_manage — Manage a media library", review("Alex", access, {}))
        self.assertNotIn("media_manage", simple_access(access, {}, for_owner=False))

    def test_a_cut_description_is_flagged_in_details(self):
        from hermes_guests.permissions import validate_schema
        from hermes_guests.presentation import own_description

        long = {
            "name": "media_tool",
            "description": "x" * 3100,
            "parameters": {"type": "object", "properties": {}},
        }
        saved = validate_schema(long)
        self.assertEqual(len(saved["description"]), 3000)  # saved schemas unchanged
        self.assertIn("description cut at 3000 characters", own_description(saved))

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

    def test_activity_uses_the_words_on_the_guests_card(self):
        scope = {
            "tools": [
                {
                    "name": "ha_call_service",
                    "schema": {"description": "Call a service"},
                    "constraints": {},
                    "presentation": {
                        "operation": "Turn the porch light on or off",
                        "resource": "entity_id",
                        "resource_labels": {"light.porch": "Porch light"},
                    },
                }
            ]
        }
        rows = [
            {
                "at": 1,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "share_id": "s",
                "tool": "ha_call_service",
                "actor_label": "Lena",
                "inputs": {"domain": "light", "service": "turn_on", "entity_id": "light.porch"},
            },
            {"at": 2, "kind": "outcome", "call_id": "a", "outcome": "returned"},
        ]
        text = activity_summary("Lena", rows, "UTC", scopes={"s": scope}, catalog={})
        self.assertIn("Lena: Porch light — turn the porch light on or off", text)
        self.assertNotIn("light.porch", text)

    def test_a_past_call_keeps_the_words_it_was_made_with(self):
        # The access now offers only turning off; the call turned the light on.
        now = {
            "tools": [
                {
                    "name": "ha_call_service",
                    "schema": {"description": "Call a service"},
                    "constraints": {"service": {"equals": "turn_off"}},
                    "presentation": {"operation": "Turn off", "resource": "entity_id"},
                }
            ]
        }
        rows = [
            {
                "at": 1,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "share_id": "s",
                "tool": "ha_call_service",
                "actor_label": "Lena",
                "inputs": {"service": "turn_on", "entity_id": "light.porch"},
                "said": "Porch light — turn on",
            },
            {"at": 2, "kind": "outcome", "call_id": "a", "outcome": "returned"},
        ]
        text = activity_summary("Lena", rows, "UTC", scopes={"s": now}, catalog={})
        self.assertIn("Lena: Porch light — turn on", text)
        self.assertNotIn("turn off", text)

    def test_audit_lines_say_what_was_done_when_for_any_tool(self):
        light = {
            "name": "ha_call_service",
            "schema": {"description": "Call a service"},
            "constraints": {
                "domain": {"equals": "light"},
                "service": {"one_of": ["turn_on", "turn_off"]},
                "entity_id": {"equals": "light.living"},
            },
            "presentation": {
                "operation": "Turn the living room light on or off",
                "resource": "entity_id",
                "resource_labels": {"light.living": "Living room light"},
            },
        }
        note = {
            "name": "notes_read",
            "schema": {"description": "Read one note by its id"},
            "constraints": {"note_id": {"one_of": ["n1", "n2"]}},
            "presentation": {
                "operation": "Read recipe",
                "resource": "note_id",
                "resource_labels": {"n2": "Pancakes"},
            },
        }
        from datetime import datetime, timezone

        at = datetime(2026, 11, 15, 5, 3, tzinfo=timezone.utc).timestamp()
        rows = [
            {
                "at": at,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "a",
                "share_id": "s",
                "tool": "ha_call_service",
                "actor_label": "Rita",
                "inputs": {"domain": "light", "service": "turn_off", "entity_id": "light.living"},
            },
            {"at": at + 1, "kind": "outcome", "call_id": "a", "outcome": "returned"},
            {
                "at": at + 60,
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "b",
                "share_id": "s",
                "tool": "notes_read",
                "actor_label": "Rita",
                "inputs": {"note_id": "n2"},
            },
            {"at": at + 61, "kind": "outcome", "call_id": "b", "outcome": "returned"},
        ]
        text = activity_summary(
            "Rita", rows, "America/Toronto", scopes={"s": {"tools": [light, note]}}, catalog={}
        )
        self.assertIn("Sun 15 Nov, 12:03 AM  Rita: Living room light — turn off", text)
        self.assertIn("Sun 15 Nov, 12:04 AM  Rita: Pancakes — read recipe", text)

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
