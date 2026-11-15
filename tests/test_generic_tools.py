"""Unfamiliar native tools work without a service adapter."""

import copy
import tempfile
import time
import unittest

from tenuo import MAX_WARRANT_SIZE, SigningKey

from hermes_guests.core import GuestsService, compile_scope
from hermes_guests.model import GuestsError, iso
from hermes_guests.store import FileState

SCHEMAS = {
    "library_find": {
        "name": "library_find",
        "description": "Find titles in a media library.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "library": {"type": "string", "title": "Media library"},
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["library", "query"],
        },
    },
    "parcel_status": {
        "name": "parcel_status",
        "description": "Read delivery status for a parcel.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"tracking": {"type": "string"}},
            "required": ["tracking"],
        },
    },
    "no_input_status": {
        "name": "no_input_status",
        "description": "Read the connected service status.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


class GenericToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.schemas = copy.deepcopy(SCHEMAS)
        self.effects = []
        self.service = GuestsService(
            FileState(self.temp.name),
            "100",
            tool_resolver=self.schemas.get,
            dispatch=lambda t, a, e: self.effects.append((t, a, e)) or {"titles": ["Example"]},
        )

    def selection(self):
        return [
            {
                "name": "library_find",
                "constraints": {
                    "library": {"equals": "Movies"},
                    "query": {"any": True},
                    "limit": {"range": [1, 5]},
                },
            }
        ]

    def prepare(self, tools=None):
        return self.service.prepare(
            "100",
            "Alex",
            {
                "tools": self.selection() if tools is None else tools,
                "expires_at": iso(time.time() + 3600),
            },
        )

    def activate(self, tools=None):
        p = self.prepare(tools)
        inv = self.service.approve("100", p["proposal_id"])
        c = self.service.claim(inv["token"], "200", "Alex")
        return self.service.accept_claim("100", c["claim_id"])["share_id"]

    def test_relative_duration_starts_when_the_owner_confirms(self):
        # "Twenty minutes" means twenty minutes of use: the time starts when the grant is
        # signed, not when the review was prepared.
        self.service.clock = lambda: 1800000000
        p = self.service.prepare(
            "100", "Alex", {"tools": self.selection(), "duration_seconds": 1200}
        )
        proposal = self.service.ledger.snapshot()["proposals"][p["proposal_id"]]
        self.assertEqual(proposal["scope"]["expires_at"], iso(1800001200))
        self.assertEqual(proposal["scope"]["duration_seconds"], 1200)
        self.service.clock = lambda: 1800000060
        inv = self.service.approve("100", p["proposal_id"])
        claim = self.service.claim(inv["token"], "200", "Alex")
        result = self.service.accept_claim("100", claim["claim_id"])
        record = self.service.get_access("100", result["share_id"])
        self.assertEqual(record["scope"]["expires_at"], iso(1800001260))
        self.assertNotIn("duration_seconds", record["scope"])  # the signed grant has an end
        self.assertEqual(record["duration_seconds"], 1200)  # kept to offer the same again
        for extra in (
            {"duration_seconds": True},
            {"duration_seconds": 0},
            {"duration_seconds": 2592001},
            {"duration_seconds": 60, "expires_at": iso(1800001200)},
        ):
            with self.subTest(extra=extra), self.assertRaises(GuestsError):
                self.service.prepare("100", "Alex", {"tools": self.selection(), **extra})

    def test_unknown_plugin_is_discovered_compiled_reviewed_and_enforced(self):
        p = self.prepare()
        self.assertIn("Find titles", p["text"])
        self.assertIn("Movies", p["text"])
        self.assertIn("Media library", p["text"])
        self.assertIn("between 1 and 5", p["text"])
        sid = self.activate()
        self.service.call(
            "200", sid, "library_find", {"library": "Movies", "query": "comedy", "limit": 3}
        )
        for args in (
            {"library": "TV", "query": "comedy"},
            {"library": "Movies", "query": "comedy", "limit": 9},
            {"library": "Movies", "query": "comedy", "admin": True},
            {"library": "Movies", "query": ""},
        ):
            with self.assertRaises(GuestsError):
                self.service.call("200", sid, "library_find", args)
        self.assertEqual(len(self.effects), 1)

    def test_schema_drift_blocks_calls_and_stale_confirmation(self):
        sid = self.activate()
        p = self.prepare()
        self.schemas["library_find"]["parameters"]["properties"]["query"]["minLength"] = 3
        with self.assertRaises(GuestsError):
            self.service.call("200", sid, "library_find", {"library": "Movies", "query": "film"})
        inv = self.service.approve("100", p["proposal_id"])
        c = self.service.claim(inv["token"], "300", "Blair")
        with self.assertRaises(GuestsError):
            self.service.accept_claim("100", c["claim_id"])
        self.assertEqual(self.effects, [])

    def test_unknown_fields_missing_required_and_host_escape_tools_cannot_be_granted(self):
        cases = [
            [{"name": "library_find", "constraints": {"library": {"equals": "Movies"}}}],
            [
                {
                    "name": "library_find",
                    "constraints": {
                        "library": {"equals": "Movies"},
                        "query": {"any": True},
                        "admin": {"any": True},
                    },
                }
            ],
            [{"name": "missing", "constraints": {}}],
        ]
        for tools in cases:
            with self.assertRaises(GuestsError):
                self.prepare(tools)
        self.schemas["terminal"] = {
            "name": "terminal",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
        }
        with self.assertRaises(GuestsError):
            self.prepare([{"name": "terminal", "constraints": {"command": {"any": True}}}])

    def test_narrowed_unknown_tool_subtask_uses_real_chain_without_confirmation(self):
        sid = self.activate()
        before = self.service.ledger.snapshot()
        child = self.service.delegate(
            "200",
            sid,
            {
                "tools": [
                    {
                        "name": "library_find",
                        "constraints": {
                            "library": {"equals": "Movies"},
                            "query": {"equals": "comedy"},
                            "limit": {"equals": 2},
                        },
                    }
                ]
            },
            task="Find two comedies",
        )
        self.assertEqual(before, self.service.ledger.snapshot())
        self.service.call(
            "200",
            sid,
            "library_find",
            {"library": "Movies", "query": "comedy", "limit": 2},
            delegation=child["task_id"],
        )
        self.assertEqual(len(self.effects[0][2]["chain"]), 2)
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                sid,
                "library_find",
                {"library": "Movies", "query": "drama", "limit": 2},
                delegation=child["task_id"],
            )
        with self.assertRaises(GuestsError):
            self.service.delegate(
                "200",
                sid,
                {
                    "tools": [
                        {
                            "name": "library_find",
                            "constraints": {"library": {"any": True}, "query": {"any": True}},
                        }
                    ]
                },
                task="Widen",
            )

    def test_safe_defaults_and_fixed_inputs_are_attached_before_signing(self):
        self.schemas["library_find"]["parameters"]["properties"]["limit"]["default"] = 3
        sid = self.activate()
        self.service.call("200", sid, "library_find", {"query": "comedy"})
        self.assertEqual(self.effects[0][1], {"library": "Movies", "query": "comedy", "limit": 3})
        self.assertEqual(self.effects[0][2]["arguments"], self.effects[0][1])

    def test_typed_json_constraints_are_compiled_by_the_released_sdk(self):
        self.schemas["structured_request"] = {
            "name": "structured_request",
            "description": "Request media.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "integer"},
                    "options": {"type": "object"},
                    "approved": {"type": "boolean"},
                },
                "required": ["kind", "options", "approved"],
            },
        }
        tools = [
            {
                "name": "structured_request",
                "constraints": {
                    "kind": {"one_of": [1, 2]},
                    "options": {"any": True},
                    "approved": {"equals": False},
                },
            }
        ]
        sid = self.activate(tools)
        self.service.call(
            "200",
            sid,
            "structured_request",
            {"kind": 1, "options": {"library": "Movies"}, "approved": False},
        )
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                sid,
                "structured_request",
                {"kind": 3, "options": {"library": "Movies"}, "approved": False},
            )
        self.assertEqual(len(self.effects), 1)

    def test_unrepresentable_object_restriction_is_explained_before_confirmation(self):
        self.schemas["object_input"] = {
            "name": "object_input",
            "description": "Use options.",
            "parameters": {
                "type": "object",
                "properties": {"options": {"type": "object"}},
                "required": ["options"],
            },
        }
        with self.assertRaisesRegex(GuestsError, "whole object"):
            self.prepare(
                [
                    {
                        "name": "object_input",
                        "constraints": {"options": {"equals": {"library": "Movies"}}},
                    }
                ]
            )
        self.assertEqual(self.service.ledger.snapshot()["proposals"], {})

    def test_unrepresentable_size_is_rejected_before_an_owner_review_exists(self):
        tools = self.selection()
        tools[0]["constraints"]["library"] = {"equals": "x" * 200000}
        with self.assertRaisesRegex(GuestsError, "too large"):
            self.prepare(tools)
        self.assertEqual(self.service.ledger.snapshot()["proposals"], {})

    def test_size_probe_reserves_the_signed_guest_before_reviewing_a_grant(self):
        holder, issuer = SigningKey.generate(), SigningKey.generate()
        tools = self.selection()

        def wire_size(length, guest=None):
            tools[0]["constraints"]["library"] = {"equals": "x" * length}
            scope = self.service.normalize_scope(
                {"tools": tools, "expires_at": iso(time.time() + 3600)}
            )
            return len(
                compile_scope(scope, holder, issuer, now=time.time(), guest=guest).to_bytes()
            )

        # Fit the old, unbound probe exactly, then demonstrate that binding the guest
        # makes it unreadable. Compute the boundary from the SDK's actual wire format.
        length = 64000
        length += MAX_WARRANT_SIZE - wire_size(length)
        self.assertEqual(wire_size(length), MAX_WARRANT_SIZE)
        self.assertGreater(wire_size(length, "200"), MAX_WARRANT_SIZE)
        with self.assertRaisesRegex(GuestsError, "too large"):
            self.prepare(tools)
        self.assertEqual(self.service.ledger.snapshot()["proposals"], {})
        self.assertEqual(self.effects, [])

        # A nearby representable grant must still activate and reach its allowed tool.
        tools[0]["constraints"]["library"] = {"equals": "x" * (length - 128)}
        sid = self.activate(tools)
        self.service.call(
            "200",
            sid,
            "library_find",
            {"library": "x" * (length - 128), "query": "comedy", "limit": 2},
        )
        self.assertEqual(len(self.effects), 1)

    def test_standard_schema_definitions_work_without_a_service_adapter(self):
        params = self.schemas["library_find"]["parameters"]
        params["$defs"] = {"Library": {"type": "string", "enum": ["Movies", "TV"]}}
        params["properties"]["library"] = {"$ref": "#/$defs/Library"}
        sid = self.activate()
        self.service.call(
            "200", sid, "library_find", {"library": "Movies", "query": "comedy", "limit": 2}
        )
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(GuestsError):
            self.service.call(
                "200", sid, "library_find", {"library": "TV", "query": "comedy", "limit": 2}
            )

    def test_builtin_names_do_not_inject_unapproved_preset_defaults_into_generic_permissions(self):
        self.schemas["web_search"] = {
            "name": "web_search",
            "description": "Search public web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        }
        sid = self.activate([{"name": "web_search", "constraints": {"query": {"any": True}}}])
        self.service.call("200", sid, "web_search", {"query": "a useful topic"})
        self.assertEqual(self.effects[0][1], {"query": "a useful topic"})

    def test_no_argument_tools_cannot_receive_hidden_inputs(self):
        sid = self.activate([{"name": "no_input_status", "constraints": {}}])
        self.service.call("200", sid, "no_input_status", {})
        with self.assertRaises(GuestsError):
            self.service.call("200", sid, "no_input_status", {"admin": True})
        self.assertEqual(len(self.effects), 1)


if __name__ == "__main__":
    unittest.main()
