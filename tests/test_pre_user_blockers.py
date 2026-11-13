"""A model's loosely shaped request is tidied, never widened; access can have no end date."""

import base64
import tempfile
import time
import unittest
from unittest.mock import patch

from hermes_guests.core import GuestsService
from hermes_guests.intake import tidy, tidy_rule
from hermes_guests.model import STANDING_SECONDS, GuestsError, iso, review
from hermes_guests.permissions import (
    describe_inputs,
    example_entry,
    normalize_tools,
    validate_schema,
)
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState

LIBRARY = {
    "name": "library_find",
    "description": "Find titles in a media library.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "library": {"type": "string", "enum": ["Movies", "TV"]},
            "query": {"type": "string"},
            "limit": {"type": "integer", "default": 3, "minimum": 1, "maximum": 50},
        },
        "required": ["library", "query"],
    },
}


def resolver(name):
    return LIBRARY if name == "library_find" else None


class HostToolTests(unittest.TestCase):
    def test_tools_that_act_on_the_owners_machine_or_browser_are_never_shareable(self):
        from hermes_guests.permissions import acts_on_host, validate_schema

        for name, toolset in (
            ("browser_exec", "browser-use"),  # a toolset name Hermes added later
            ("browser_vault", "browser"),
            ("page_click", "browser_automation"),
            ("computer_use", "computer_use"),
            ("terminal", "terminal"),
            ("read_file", "file"),
            ("run_python", "code_execution"),
        ):
            self.assertTrue(acts_on_host(name, toolset), name)
        for name, toolset in (
            ("web_search", "web"),
            ("library_find", "fixture_media"),
            ("ha_call_service", "homeassistant"),
        ):
            self.assertFalse(acts_on_host(name, toolset), name)
        with self.assertRaises(GuestsError):
            validate_schema(
                {
                    "name": "browser_exec",
                    "description": "x",
                    "parameters": {"type": "object", "properties": {}},
                }
            )


class IntakeTests(unittest.TestCase):
    def test_a_bare_true_is_not_read_as_any_value(self):
        got = tidy(
            {
                "person": "Alex",
                "scope": {
                    "tools": [
                        {
                            "name": "library_find",
                            "constraints": {"library": "Movies", "query": True},
                        }
                    ]
                },
            },
            resolver,
        )
        self.assertIs(got["scope"]["tools"][0]["constraints"]["query"], True)  # left as written
        with self.assertRaises(GuestsError):
            normalize_tools(got["scope"]["tools"], resolver)  # and refused
        whole = tidy(
            {"person": "Alex", "scope": {"tools": [{"name": "library_find", "constraints": True}]}},
            resolver,
        )
        with self.assertRaises(GuestsError):
            normalize_tools(whole["scope"]["tools"], resolver)

    def test_several_words_for_one_bound_keep_the_narrowest(self):
        from hermes_guests.intake import tidy_rule

        self.assertEqual(tidy_rule({"min": 1, "gte": 10, "max": 20}, {}), {"range": [10, 20]})
        self.assertEqual(tidy_rule({"min": 1, "max": 20, "lte": 5}, {}), {"range": [1, 5]})
        unreadable = {"min": 1, "gte": "ten", "max": 20}
        self.assertEqual(tidy_rule(dict(unreadable), {}), unreadable)  # left to be refused
        with self.assertRaises(GuestsError):
            normalize_tools(
                [
                    {
                        "name": "library_find",
                        "constraints": {"library": "Movies", "limit": unreadable},
                    }
                ],
                resolver,
            )

    def test_reading_labels_survive_tidying_and_reach_validation(self):
        labels = {"resource": "library", "labels": {"query": "Title"}}
        got = tidy(
            {
                "person": "Alex",
                "chat_only": False,
                "tools": [
                    {
                        "tool": "library_find",
                        "inputs": {"library": "Movies", "query": {"any": True}},
                        "presentation": labels,
                    }
                ],
            },
            resolver,
        )
        self.assertEqual(got["scope"]["tools"][0]["presentation"], labels)
        self.assertIs(got["scope"]["chat_only"], False)
        self.assertIn("presentation", normalize_tools(got["scope"]["tools"], resolver)[0])

    def test_a_limit_stated_twice_with_different_values_is_refused(self):
        entry = {"name": "library_find", "constraints": {"library": "Movies", "query": "x"}}
        for request in (
            {"duration_seconds": 3600, "scope": {"tools": [entry], "duration_seconds": 86400}},
            {
                "scope": {
                    "duration_seconds": 3600,
                    "tools": [
                        dict(entry, constraints={"library": "Movies", "duration_seconds": 60})
                    ],
                }
            },
            {"scope": {"tools": [dict(entry, rules={"library": "TV", "query": {"any": True}})]}},
            {"scope": {"tools": [dict(entry, tool="light_set")]}},
            {"scope": {"tools": [dict(entry, anything_else=True)]}},
        ):
            with self.assertRaises(GuestsError, msg=request):
                tidy(dict(request, person="Alex"), resolver)
        same = tidy(
            {
                "person": "Alex",
                "duration_seconds": 3600,
                "scope": {"tools": [dict(entry, tool="library_find")], "duration_seconds": 3600},
            },
            resolver,
        )
        self.assertEqual(same["scope"]["duration_seconds"], 3600)  # said twice, meant once
        self.assertEqual(same["scope"]["tools"][0]["name"], "library_find")

    def test_the_time_to_accept_an_invitation_passes_through(self):
        got = tidy(
            {"person": "Alex", "invitation_seconds": 86400, "scope": {"duration_seconds": 3600}},
            resolver,
        )
        self.assertEqual(got["invitation_seconds"], 86400)  # beside the scope, not inside it
        self.assertNotIn("invitation_seconds", got["scope"])

    def test_a_call_stopped_before_the_service_is_shown_as_refused(self):
        from hermes_guests.plugin import activity_text

        rows = [
            {
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "c1",
                "at": 0,
                "tool": "web_extract",
            },
            {"kind": "adapter_blocked", "call_id": "c1", "at": 1, "reason": "non-public-url"},
            {
                "kind": "decision",
                "decision": "ALLOW",
                "call_id": "c2",
                "at": 2,
                "tool": "web_search",
            },
            {"kind": "outcome", "call_id": "c2", "at": 3, "outcome": "returned"},
        ]
        text = activity_text("Alex", rows, "UTC")
        self.assertIn("web_extract : refused before it ran", text)
        self.assertIn("web_search : done", text)
        self.assertNotIn("no result recorded", text)

    def test_inputs_whose_type_is_a_list_do_not_break_discovery(self):
        schema = dict(
            LIBRARY,
            parameters=dict(
                LIBRARY["parameters"],
                properties={
                    "library": {"type": "string", "enum": ["Movies", "TV"]},
                    "query": {"type": ["string", "null"]},
                    "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
                },
            ),
        )
        self.assertIn("query", describe_inputs(schema))
        self.assertEqual(example_entry(schema)["name"], "library_find")

    def test_other_words_for_the_person_and_the_tool_name_are_read(self):
        from hermes_guests.intake import tidy

        got = tidy(
            {
                "user": "Alex",
                "tools": [{"tool_name": "library_find", "inputs": {"library": "Movies"}}],
            },
            lambda name: None,
        )
        self.assertEqual(got["person"], "Alex")
        self.assertEqual(
            got["scope"]["tools"],
            [{"name": "library_find", "constraints": {"library": {"equals": "Movies"}}}],
        )

    def test_time_limit_written_one_level_up_or_under_a_tool_ends_up_in_the_scope(self):
        up = tidy(
            {
                "person": "Alex",
                "duration_seconds": 3600,
                "scope": {"tools": [{"name": "library_find", "constraints": {}}]},
            },
            resolver,
        )
        self.assertEqual(up["scope"]["duration_seconds"], 3600)
        self.assertNotIn("duration_seconds", up)
        under = tidy(
            {
                "person": "Alex",
                "scope": {
                    "tools": [
                        {
                            "name": "library_find",
                            "constraints": {
                                "duration_seconds": {"equals": 3600},
                                "library": {"equals": "Movies"},
                            },
                        }
                    ]
                },
            },
            resolver,
        )
        self.assertEqual(under["scope"]["duration_seconds"], 3600)
        self.assertEqual(
            under["scope"]["tools"][0]["constraints"], {"library": {"equals": "Movies"}}
        )

    def test_bare_values_become_rules_without_adding_any(self):
        got = tidy(
            {
                "person": "Alex",
                "tools": [
                    {
                        "name": "library_find",
                        "constraints": {
                            "library": "Movies",
                            "limit": {"max": 5},
                            "query": {"any": True},
                        },
                    }
                ],
                "until_ended": True,
            },
            resolver,
        )
        self.assertEqual(
            got["scope"]["tools"][0]["constraints"],
            {"library": {"equals": "Movies"}, "limit": {"range": [1, 5]}, "query": {"any": True}},
        )
        self.assertTrue(got["scope"]["until_ended"])
        self.assertEqual(tidy_rule(["Movies", "TV"], {}), {"one_of": ["Movies", "TV"]})
        self.assertEqual(tidy_rule(True, {"type": "boolean"}), {"equals": True})

    def test_the_example_to_retry_with_keeps_the_rules_that_were_fine(self):
        import json

        asked = {"library": {"equals": "Movies"}, "limit": {"range": [1, 5]}, "query": True}
        with self.assertRaises(GuestsError) as caught:
            normalize_tools([{"name": "library_find", "constraints": asked}], resolver)
        example = json.loads(str(caught.exception).split("send it again: ", 1)[1])
        self.assertEqual(example["constraints"]["library"], {"equals": "Movies"})
        self.assertEqual(example["constraints"]["limit"], {"range": [1, 5]})
        self.assertEqual(example["constraints"]["query"], {"any": True})  # only this is new

    def test_the_example_to_retry_with_adds_no_input_that_was_not_asked_for(self):
        import json

        schema = dict(
            LIBRARY,
            parameters=dict(
                LIBRARY["parameters"],
                properties=dict(
                    LIBRARY["parameters"]["properties"], include_private={"type": "boolean"}
                ),
            ),
        )
        asked = {"library": {"equals": "Movies"}, "query": True}
        with self.assertRaises(GuestsError) as caught:
            normalize_tools([{"name": "library_find", "constraints": asked}], lambda name: schema)
        example = json.loads(str(caught.exception).split("send it again: ", 1)[1])
        self.assertEqual(set(example["constraints"]), {"library", "query"})  # no limit, no extra
        self.assertEqual(example["constraints"]["library"], {"equals": "Movies"})
        with self.assertRaises(GuestsError) as caught:  # a required input left out is added
            normalize_tools(
                [{"name": "library_find", "constraints": {"library": {"equals": "TV"}}}],
                lambda name: schema,
            )
        example = json.loads(str(caught.exception).split("send it again: ", 1)[1])
        self.assertEqual(set(example["constraints"]), {"library", "query"})

    def test_an_input_whose_schema_is_plain_true_or_false_does_not_break_anything(self):
        schema = dict(
            LIBRARY,
            parameters={
                "type": "object",
                "properties": {"library": {"type": "string"}, "extra": True, "never": False},
                "required": ["library"],
            },
        )
        self.assertIn("extra (any value, optional)", describe_inputs(schema))
        self.assertIn("never (not accepted, optional)", describe_inputs(schema))
        entry = example_entry(schema)
        self.assertEqual(entry["constraints"], {"library": {"any": True}, "extra": {"any": True}})
        got = tidy({"person": "Alex", "tools": [dict(entry)]}, lambda name: schema)
        normalize_tools(got["scope"]["tools"], lambda name: schema)
        # And all the way through: the review, the grant, and calls with and without it.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        effects = []
        s = GuestsService(
            FileState(temp.name),
            "100",
            tool_resolver=lambda name: schema,
            dispatch=lambda tool, args, envelope: effects.append(args) or {"ok": True},
        )
        p = s.prepare("100", "Alex", {"tools": [entry], "duration_seconds": 3600})
        self.assertIn("Extra — any value", p["text"])
        claim = s.claim(s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        sid = s.accept_claim("100", claim["claim_id"])["share_id"]
        s.call("200", sid, "library_find", {"library": "Movies", "extra": {"a": 1}})
        self.assertEqual(effects, [{"library": "Movies", "extra": {"a": 1}}])
        for refused in ({"library": "TV", "never": 1}, {"library": "TV"}):
            # Refused in the ordinary way (an approved input with no default has to be
            # given, as for any other input), not by an error inside Hermes Guests.
            with self.assertRaises(GuestsError):
                s.call("200", sid, "library_find", refused)
        self.assertEqual(len(effects), 1)
        s.takeover_scope(["library_find"])  # what setup would hand to people already admitted

    def test_the_example_for_a_tool_with_referenced_inputs_is_one_the_review_accepts(self):
        defs = {
            "Library": {"type": "string", "enum": ["Movies", "TV"]},
            "Limit": {"type": "integer", "minimum": 1, "maximum": 5},
            "Share": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "Maybe": {"type": ["integer", "null"], "minimum": 1, "maximum": 5},
            "Flag": {"type": "boolean"},
        }
        for kind, call in (
            ("Limit", {"library": "Movies", "extra": 3}),
            ("Share", {"library": "TV"}),  # the default behind the reference is filled in
            ("Maybe", {"library": "TV", "extra": 2}),
            ("Flag", {"library": "TV", "extra": True}),
        ):
            schema = {
                "name": "library_find",
                "description": "Find titles.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "$defs": defs,
                    "properties": {
                        "library": {"$ref": "#/$defs/Library"},
                        "extra": {"$ref": "#/$defs/" + kind},
                    },
                    "required": ["library"],
                },
            }
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            effects = []
            s = GuestsService(
                FileState(temp.name),
                "100",
                tool_resolver=lambda name, schema=schema: schema,
                dispatch=lambda tool, args, envelope, effects=effects: (
                    effects.append(args) or {"ok": True}
                ),
            )
            entry = example_entry(validate_schema(schema))  # what the buttons and setup use
            p = s.prepare("100", "Alex", {"tools": [entry], "duration_seconds": 3600})
            claim = s.claim(s.approve("100", p["proposal_id"])["token"], "200", "Alex")
            sid = s.accept_claim("100", claim["claim_id"])["share_id"]
            s.call("200", sid, "library_find", call)
            self.assertEqual(len(effects), 1, kind)
            s.takeover_scope(["library_find"])
        self.assertEqual(entry["constraints"]["library"], {"one_of": ["Movies", "TV"]})

    def test_a_range_on_an_input_that_is_not_a_number_is_refused_not_a_crash(self):
        for spec in (
            {"type": "string"},
            {"type": ["string", "null"]},
            True,
            {"$ref": "#/$defs/Word"},
        ):
            schema = dict(
                LIBRARY,
                parameters={
                    "type": "object",
                    "$defs": {"Word": {"type": "string"}},
                    "properties": {"library": {"type": "string"}, "limit": spec},
                    "required": ["library"],
                },
            )
            rules = {"library": {"any": True}, "limit": {"range": [1, 5]}}
            with self.assertRaisesRegex(GuestsError, "Numeric limits"):
                normalize_tools(
                    [{"name": "library_find", "constraints": rules}], lambda name: schema
                )
        allowed = dict(
            LIBRARY,
            parameters={
                "type": "object",
                "properties": {
                    "library": {"type": "string"},
                    "limit": {"type": ["integer", "null"]},
                },
                "required": ["library"],
            },
        )
        rules = {"library": {"any": True}, "limit": {"range": [1, 5]}}
        normalize_tools([{"name": "library_find", "constraints": rules}], lambda name: allowed)

    def test_choices_that_are_objects_get_an_explicit_any_rule(self):
        schema = dict(
            LIBRARY,
            parameters=dict(
                LIBRARY["parameters"],
                properties={
                    "library": {"type": "string", "enum": ["Movies", "TV"]},
                    "query": {"type": "string"},
                    "sort": {"type": "object", "enum": [{"by": "year"}, {"by": "title"}]},
                },
            ),
        )
        entry = example_entry(schema)
        self.assertEqual(entry["constraints"]["sort"], {"any": True})
        self.assertEqual(entry["constraints"]["library"], {"one_of": ["Movies", "TV"]})
        normalize_tools([entry], lambda name: schema)  # a selection the review accepts

    def test_an_input_left_out_is_not_filled_in(self):
        got = tidy(
            {
                "person": "Alex",
                "scope": {
                    "duration_seconds": 60,
                    "tools": [{"name": "library_find", "constraints": {"library": "Movies"}}],
                },
            },
            resolver,
        )
        with self.assertRaises(GuestsError) as caught:
            normalize_tools(got["scope"]["tools"], resolver)
        text = str(caught.exception)
        self.assertIn("missing: query", text)
        self.assertIn(
            '"library": {"equals": "Movies"}', text
        )  # what was chosen is kept in the example
        self.assertIn('"query": {"any": true}', text)

    def test_unknown_inputs_and_fields_say_what_is_accepted(self):
        with self.assertRaises(GuestsError) as caught:
            normalize_tools(
                [{"name": "library_find", "constraints": {"max_results": {"equals": 5}}}], resolver
            )
        self.assertIn("no input called max_results", str(caught.exception))
        self.assertIn('library (one of "Movies", "TV")', str(caught.exception))
        with self.assertRaises(GuestsError) as caught:
            tidy({"person": "Alex", "access_type": "search", "scope": {}}, resolver)
        self.assertIn("Unknown fields: access_type", str(caught.exception))

    def test_the_whole_tool_must_be_asked_for_explicitly(self):
        got = tidy(
            {
                "person": "Alex",
                "scope": {"tools": [{"name": "library_find", "constraints": {"any": True}}]},
            },
            resolver,
        )
        self.assertEqual(
            got["scope"]["tools"][0],
            example_entry(LIBRARY)
            | {"constraints": {k: {"any": True} for k in ("library", "query", "limit")}},
        )
        self.assertIn("limit (whole number 1 to 50, optional)", describe_inputs(LIBRARY))
        self.assertEqual(
            example_entry(LIBRARY)["constraints"]["library"], {"one_of": ["Movies", "TV"]}
        )


class StandingAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = time.time()
        self.effects = []
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog={"light.porch": {"name": "Porch light"}},
            dispatch=lambda t, a, e: self.effects.append(t) or {"state": "on"},
            tool_resolver=GUEST_SCHEMAS.get,
            clock=lambda: self.now,
        )

    def activate(self, scope):
        p = self.s.prepare("100", "Alex", scope)
        claim = self.s.claim(self.s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        return p, self.s.accept_claim("100", claim["claim_id"])["share_id"]

    def test_access_with_no_end_date_keeps_working_and_can_still_be_ended(self):
        # Signed grants are checked against the real clock, so this runs in real time with a
        # grant life of a few seconds standing in for the real sixty days.
        self.s.clock = time.time
        with (
            patch("hermes_guests.model.STANDING_SECONDS", 8),
            patch("hermes_guests.model.STANDING_RENEW", 7),
        ):
            p, sid = self.activate(
                {"lights": ["light.porch"], "research": False, "until_ended": True}
            )
            self.assertIn("No end date. Access continues until the owner ends it.", p["text"])
            first = self.s.guest("200")["warrant_id"]
            self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
            self.assertEqual(
                self.s.guest("200")["warrant_id"], first
            )  # plenty of life left: not reissued
            time.sleep(1.6)
            self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
            second = self.s.guest("200")["warrant_id"]
            self.assertNotEqual(second, first)
            time.sleep(8.5)  # longer than any one grant lives
            self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
            self.assertEqual(len(self.effects), 3)
            record = self.s.guest("200")
            self.assertEqual((record["effective_status"], record["revision"]), ("active", 1))
            with self.assertRaises(GuestsError):
                self.s.renewal("200", sid)  # nothing to renew
            revoke = self.s.prepare("100", "Alex", None, share_id=sid, action="revoke")
            self.s.approve("100", revoke["proposal_id"])
            time.sleep(1.2)
            with self.assertRaises(GuestsError):
                self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
            self.s.maintenance("100")  # and maintenance never brings it back
            with self.assertRaises(GuestsError):
                self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
            self.assertEqual(len(self.effects), 3)

    def test_time_limited_access_still_ends_and_is_never_extended(self):
        _, sid = self.activate(
            {"lights": ["light.porch"], "research": False, "duration_seconds": 3600}
        )
        self.now += 7200
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        self.s.maintenance("100")
        self.assertNotEqual(self.s.guest("200")["effective_status"], "active")

    def test_no_end_date_and_a_time_limit_cannot_be_combined(self):
        with self.assertRaises(GuestsError):
            self.s.prepare(
                "100",
                "Alex",
                {"lights": ["light.porch"], "until_ended": True, "duration_seconds": 60},
            )
        with self.assertRaises(GuestsError):
            self.s.prepare(
                "100",
                "Alex",
                {"lights": ["light.porch"], "expires_at": iso(self.now + STANDING_SECONDS)},
            )  # past 30 days
        text = review(
            "Alex",
            {"lights": [], "research": True, "tools": [], "expires_at": iso(self.now + 60)},
            {},
        )
        self.assertIn("Ends ", text)


class StandingRenewalTests(unittest.TestCase):
    """Continuing access with no end date only ever moves its end; it never papers over a
    grant that no longer verifies, and it never leaves a revoked grant behind."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.effects = []
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog={"light.porch": {"name": "Porch light"}},
            dispatch=lambda t, a, e: self.effects.append(t) or {"state": "on"},
            tool_resolver=GUEST_SCHEMAS.get,
            clock=time.time,
        )
        for name, value in (("STANDING_SECONDS", 3600), ("STANDING_RENEW", 10**9)):  # always due
            patcher = patch("hermes_guests.model." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        p = self.s.prepare(
            "100", "Alex", {"lights": ["light.porch"], "research": False, "until_ended": True}
        )
        claim = self.s.claim(self.s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        self.sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        self.first = self.s.guest("200")["warrant_id"]

    def read(self):
        return self.s.call("200", self.sid, "ha_get_state", {"entity_id": "light.porch"})

    def test_chat_only_access_with_no_end_date_is_continued_when_a_chat_turn_arrives(self):
        p = self.s.prepare("100", "Blair", {"chat_only": True, "until_ended": True})
        claim = self.s.claim(self.s.approve("100", p["proposal_id"])["token"], "300", "Blair")
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        first = self.s.guest("300")["warrant_id"]
        self.s.admit_chat("300", sid)  # no upkeep pass has run; the turn itself continues it
        self.assertNotEqual(self.s.guest("300")["warrant_id"], first)
        self.assertEqual(self.s.repair("100")["paused"], [])

    def test_a_continued_grant_is_saved_even_when_the_call_that_triggered_it_is_refused(self):
        with self.assertRaises(GuestsError):
            self.s.call("200", self.sid, "ha_get_state", {"entity_id": "light.kitchen"})
        saved = self.s.guest("200")
        self.assertNotEqual(saved["warrant_id"], self.first)  # the new grant is on record
        self.assertEqual(self.s.repair("100")["paused"], [])  # and it verifies
        self.read()
        self.assertEqual(len(self.effects), 1)

    def test_a_swapped_guest_key_is_not_continued(self):
        from tenuo import SigningKey

        from hermes_guests.store import atomic_private

        atomic_private(
            self.s.root / "control" / (self.sid + ".key"),
            base64.b64encode(SigningKey.generate().secret_key_bytes()),
        )
        with self.assertRaises(Exception):
            self.read()
        self.assertEqual(self.s.guest("200")["warrant_id"], self.first)
        self.assertEqual(self.effects, [])

    def test_a_missing_or_altered_grant_is_not_replaced(self):
        (self.s.root / "warrants" / (self.first + ".warrant")).write_bytes(b"not the grant")
        with self.assertRaises(Exception):
            self.read()
        self.assertEqual(self.s.guest("200")["warrant_id"], self.first)
        self.assertEqual(self.effects, [])

    def test_a_revoked_grant_is_not_continued(self):
        with self.s.ledger.transaction() as db:
            self.s._revoke_warrant(db, self.first)
        with self.assertRaises(Exception):
            self.read()
        self.assertEqual(self.s.guest("200")["warrant_id"], self.first)
        self.assertEqual(self.effects, [])

    def test_permissions_edited_in_the_saved_record_are_not_signed_at_renewal(self):
        with self.s.ledger.transaction() as db:
            db["shares"][self.sid]["scope"]["research"] = True  # wider than what was signed
        self.s._continue(self.sid, "200")
        self.assertEqual(self.s.guest("200")["warrant_id"], self.first)
        with self.s.ledger.transaction() as db:
            db["shares"][self.sid]["scope"]["research"] = False  # as signed again
        self.s._continue(self.sid, "200")
        self.assertNotEqual(self.s.guest("200")["warrant_id"], self.first)

    def test_subtasks_end_with_the_grant_they_were_issued_under(self):
        child = self.s.delegate(
            "200", self.sid, {"lights": ["light.porch"], "research": False}, task="Read porch state"
        )
        self.assertIn(child["task_id"], self.s.delegates)
        with self.s.ledger.transaction() as db:
            self.assertTrue(self.s._keep_standing(db, db["shares"][self.sid]))
        self.assertNotIn(child["task_id"], self.s.delegates)
        with self.assertRaises(GuestsError) as ended:
            self.s.call(
                "200",
                self.sid,
                "ha_get_state",
                {"entity_id": "light.porch"},
                delegation=child["task_id"],
            )
        self.assertIn("delegated task has ended", str(ended.exception))


class LimitedAccessStaysLimitedTests(unittest.TestCase):
    def test_marking_time_limited_access_as_having_no_end_does_not_continue_it(self):
        with tempfile.TemporaryDirectory() as temp:
            s = GuestsService(
                FileState(temp),
                "100",
                catalog={"light.porch": {"name": "Porch light"}},
                dispatch=lambda t, a, e: {},
                tool_resolver=GUEST_SCHEMAS.get,
                clock=time.time,
            )
            p = s.prepare(
                "100",
                "Alex",
                {"lights": ["light.porch"], "research": False, "duration_seconds": 600},
            )
            claim = s.claim(s.approve("100", p["proposal_id"])["token"], "200", "Alex")
            sid = s.accept_claim("100", claim["claim_id"])["share_id"]
            first = s.guest("200")["warrant_id"]
            with s.ledger.transaction() as db:
                db["shares"][sid]["scope"]["until_ended"] = True  # the record says no end date
            with patch("hermes_guests.model.STANDING_RENEW", 10**9):
                s._continue(sid, "200")
            self.assertEqual(
                s.guest("200")["warrant_id"], first
            )  # the signed grant says ten minutes


class OwnerContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog={},
            dispatch=lambda t, a, e: {},
            tool_resolver=GUEST_SCHEMAS.get,
            clock=time.time,
        )

    def test_legacy_setup_stays_enabled_without_the_old_owner_warrant_file(self):
        with self.s.ledger.transaction() as db:
            db["setup"]["owner_authority"] = {"warrant_id": "old", "expires_at": 0}
        self.assertTrue(self.s.owner_ready())
        self.assertTrue(self.s.owner_configured())

    def test_the_owner_is_not_asked_to_repeat_setup_when_their_grant_runs_out(self):
        # Real time, with a grant life of a few seconds standing in for the real week.
        with (
            patch("hermes_guests.core.OWNER_SECONDS", 8),
            patch("hermes_guests.core.OWNER_RENEW", 7),
        ):
            self.s.setup_owner("100", {"guests_list_access": "x", "library_find": "y"})
            first = self.s.ledger.snapshot()["setup"]["owner_authority"]["warrant_id"]
            self.assertIsNone(self.s.check_owner("100", "library_find", {}, "s1"))
            self.assertEqual(
                self.s.ledger.snapshot()["setup"]["owner_authority"]["warrant_id"], first
            )
            time.sleep(9)  # longer than one grant lives
            self.assertIsNone(self.s.check_owner("100", "library_find", {}, "renewal"))
            self.assertNotEqual(
                self.s.ledger.snapshot()["setup"]["owner_authority"]["warrant_id"], first
            )
            self.assertIsNone(self.s.check_owner("100", "library_find", {}, "s2"))
            # A tool that was not part of setup still needs the owner's review.
            blocked = self.s.check_owner("100", "terminal", {}, "s3")
            self.assertIn("/guests_setup", blocked["message"])

    def test_a_grant_that_cannot_be_verified_is_not_continued(self):
        with (
            patch("hermes_guests.core.OWNER_SECONDS", 3),
            patch("hermes_guests.core.OWNER_RENEW", 0),
        ):
            self.s.setup_owner("100", {"library_find": "y"})
            authority = self.s.ledger.snapshot()["setup"]["owner_authority"]
            (self.s.root / "warrants" / (authority["warrant_id"] + ".warrant")).write_bytes(b"x")
            time.sleep(3.5)
            self.assertEqual(
                self.s.check_owner("100", "library_find", {}, "broken")["action"], "block"
            )
            self.assertTrue(self.s.owner_configured())

    def test_a_tool_added_to_the_saved_record_is_not_granted_at_renewal(self):
        with (
            patch("hermes_guests.core.OWNER_SECONDS", 3600),
            patch("hermes_guests.core.OWNER_RENEW", 10**9),
        ):
            self.s.setup_owner("100", {"library_find": "y"})
            with self.s.ledger.transaction() as db:
                db["setup"]["owner_authority"]["tools"].append(
                    "terminal"
                )  # never accepted at setup
            self.assertIsNone(self.s.check_owner("100", "library_find", {}, "renewal"))  # renewed
            self.assertEqual(
                self.s.ledger.snapshot()["setup"]["owner_authority"]["tools"], ["library_find"]
            )
            self.assertIsNone(self.s.check_owner("100", "library_find", {}, "s1"))
            self.assertIn(
                "/guests_setup", self.s.check_owner("100", "terminal", {}, "s2")["message"]
            )

    def test_a_different_owner_key_in_place_is_not_continued(self):
        from tenuo import SigningKey

        from hermes_guests.store import atomic_private

        with (
            patch("hermes_guests.core.OWNER_SECONDS", 3600),
            patch("hermes_guests.core.OWNER_RENEW", 10**9),
        ):
            self.s.setup_owner("100", {"library_find": "y"})
            first = self.s.ledger.snapshot()["setup"]["owner_authority"]["warrant_id"]
            self.assertIsNone(self.s.check_owner("100", "library_find", {}, "renewal"))
            renewed = self.s.ledger.snapshot()["setup"]["owner_authority"]["warrant_id"]
            self.assertNotEqual(renewed, first)  # the owner's own key: continued
            atomic_private(
                self.s.root / "control" / "owner.key",
                base64.b64encode(SigningKey.generate().secret_key_bytes()),
            )
            self.assertEqual(
                self.s.check_owner("100", "library_find", {}, "wrong-holder")["action"], "block"
            )
            self.assertEqual(
                self.s.ledger.snapshot()["setup"]["owner_authority"]["warrant_id"], renewed
            )  # another key: left for the owner

    def test_a_prefixed_tool_name_is_corrected_instead_of_sending_the_owner_to_setup(self):
        self.s.setup_owner("100", {"guests_prepare_access": "x"})
        blocked = self.s.check_owner("100", "functions.guests_prepare_access", {}, "s1")
        self.assertIn("exact name: guests_prepare_access", blocked["message"])
        self.assertNotIn("/guests_setup", blocked["message"])
        other = self.s.check_owner("100", "functions.terminal", {}, "s1")
        self.assertIn("/guests_setup", other["message"])
        wrapped = self.s.check_owner(
            "100",
            "tool_call",
            {"calls": [{"name": "hermes_guests:guests_prepare_access", "arguments": {}}]},
            "s1",
        )
        self.assertIn("exact name: guests_prepare_access", wrapped["message"])
        unknown = self.s.check_owner("100", "tool_call", {"calls": [{"name": "nope"}]}, "s1")
        self.assertEqual(unknown["action"], "block")
        self.assertNotIn("/guests_setup", unknown["message"])


class RequestBoundaryTests(unittest.TestCase):
    def test_no_end_date_with_an_end_time_is_refused_not_widened(self):
        with tempfile.TemporaryDirectory() as temp:
            s = GuestsService(
                FileState(temp),
                "100",
                catalog={"light.porch": {"name": "Porch light"}},
                dispatch=lambda t, a, e: {},
                tool_resolver=GUEST_SCHEMAS.get,
                clock=time.time,
            )
            with self.assertRaises(GuestsError) as refused:
                s.prepare(
                    "100",
                    "Alex",
                    {
                        "lights": ["light.porch"],
                        "research": False,
                        "until_ended": True,
                        "expires_at": iso(time.time() + 3600),
                    },
                )
            self.assertIn("not both", str(refused.exception))


class NoEndDateMarkTests(unittest.TestCase):
    def test_the_no_end_date_mark_is_signed_and_cannot_be_called(self):
        from hermes_guests.core import STANDING_MARK

        with tempfile.TemporaryDirectory() as temp:
            effects = []
            s = GuestsService(
                FileState(temp),
                "100",
                catalog={"light.porch": {"name": "Porch light"}},
                dispatch=lambda t, a, e: effects.append(t) or {},
                tool_resolver=GUEST_SCHEMAS.get,
                clock=time.time,
            )
            for scope, marked in (
                ({"lights": ["light.porch"], "research": False, "until_ended": True}, True),
                ({"lights": ["light.porch"], "research": False, "duration_seconds": 600}, False),
            ):
                guest = "200" if marked else "201"
                p = s.prepare("100", "Guest " + guest, scope)
                claim = s.claim(s.approve("100", p["proposal_id"])["token"], guest, "Guest")
                sid = s.accept_claim("100", claim["claim_id"])["share_id"]
                self.assertEqual(STANDING_MARK in s._warrant(s.guest(guest)).tools, marked)
                with self.assertRaises(GuestsError):
                    s.call(guest, sid, STANDING_MARK, {})
            self.assertEqual(effects, [])


if __name__ == "__main__":
    unittest.main()
