"""One signed grant keeps separately approved operation/resource rules together."""

import copy
import tempfile
import unittest

from jsonschema import Draft202012Validator
from tenuo import SignedRevocationList

from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError
from hermes_guests.permissions import normalize_tools, proxy_schema, rebase_schema_refs
from hermes_guests.store import FileState

SCHEMA = {
    "name": "device_control",
    "description": "Control selected devices.",
    "parameters": {
        "type": "object",
        "properties": {
            "domain": {"type": "string"},
            "device": {"type": "string"},
            "action": {"type": "string"},
        },
        "required": ["domain", "device", "action"],
        "additionalProperties": False,
    },
}


def rule(domain, devices, actions):
    return {
        "name": "device_control",
        "constraints": {
            "domain": {"equals": domain},
            "device": {"one_of": devices},
            "action": {"one_of": actions},
        },
    }


class GroupedRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.effects = []
        self.state = FileState(self.temp.name)
        self.schema = copy.deepcopy(SCHEMA)
        self.service = self.restore()
        self.scope = {
            "tools": [
                rule("door", ["B"], ["lock", "unlock"]),
                rule("light", ["C", "D", "E"], ["on", "off"]),
            ],
            "duration_seconds": 3600,
        }
        p = self.service.prepare("100", "Alex", self.scope)
        invitation = self.service.approve("100", p["proposal_id"])
        claim = self.service.claim(invitation["token"], "200", "Alex")
        self.sid = self.service.accept_claim("100", claim["claim_id"])["share_id"]

    def restore(self):
        return GuestsService(
            self.state,
            "100",
            tool_resolver=lambda name: self.schema,
            dispatch=lambda tool, args, envelope: (
                self.effects.append((args, envelope)) or {"ok": True}
            ),
        )

    def call(self, domain, device, action, **kwargs):
        return self.service.call(
            "200",
            self.sid,
            "device_control",
            {"request": [{"domain": domain, "device": device, "action": action, **kwargs}]},
        )

    def test_one_warrant_allows_door_b_and_lights_c_d_e(self):
        for action in ("lock", "unlock"):
            self.call("door", "B", action)
        for device in ("C", "D", "E"):
            for action in ("on", "off"):
                self.call("light", device, action)
        self.assertEqual(len(self.effects), 8)
        self.assertTrue(all("request" not in args for args, _ in self.effects))
        self.assertEqual(len({e["chain"][0] for _, e in self.effects}), 1)
        self.assertEqual(self.effects[0][1]["argument_view"], "single_request")

    def test_a_never_share_rule_for_one_input_reaches_inside_a_grouped_call(self):
        self.call("door", "B", "unlock")  # access given before the rule existed
        self.service.never_set(
            "100", "scoped", "unlock", True, tool="device_control", field="action"
        )
        with self.assertRaisesRegex(GuestsError, "does not share"):
            self.call("door", "B", "unlock")
        self.call("door", "B", "lock")  # another value of that input still runs
        self.assertEqual(len(self.effects), 2)
        self.assertEqual(self.effects[-1][1]["argument_view"], "single_request")

    def test_a_tool_input_called_request_is_not_confused_with_the_grouped_envelope(self):
        self.schema.update(
            name="message_send",
            parameters={
                "type": "object",
                "properties": {"request": {"type": "string"}, "note": {"type": "string"}},
                "required": ["request", "note"],
                "additionalProperties": False,
            },
        )

        def to(kind):
            rules = {"request": {"one_of": [kind, kind + " price"]}, "note": {"any": True}}
            return {"name": "message_send", "constraints": rules}

        p = self.service.prepare(
            "100", "Blair", {"tools": [to("weather"), to("bitcoin")], "duration_seconds": 3600}
        )
        claim = self.service.claim(
            self.service.approve("100", p["proposal_id"])["token"], "300", "Blair"
        )
        sid = self.service.accept_claim("100", claim["claim_id"])["share_id"]
        self.service.never_set(
            "100", "scoped", "bitcoin", True, tool="message_send", field="request"
        )

        def send(request, note):
            return self.service.call(
                "300", sid, "message_send", {"request": [{"request": request, "note": note}]}
            )

        send("weather", "about bitcoin")  # the listed word is in another input
        self.assertEqual(self.effects[-1][0], {"request": "weather", "note": "about bitcoin"})
        with self.assertRaisesRegex(GuestsError, "does not share"):
            send("bitcoin price", "x")
        self.assertEqual(len(self.effects), 1)

    def test_tenuo_denies_crossed_rules_extra_inputs_and_other_resources(self):
        for args in [
            ("door", "C", "unlock"),
            ("light", "B", "on"),
            ("light", "kitchen", "off"),
            ("door", "other", "unlock"),
        ]:
            with self.subTest(args=args), self.assertRaises(GuestsError):
                self.call(*args)
        with self.assertRaises(GuestsError):
            self.call("light", "C", "on", data={"device": "kitchen"})
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                self.sid,
                "device_control",
                {"domain": "door", "device": "B", "action": "unlock"},
            )
        self.assertEqual(self.effects, [])
        decisions = [e for e in self.service.activity("100", self.sid) if e["kind"] == "decision"]
        self.assertEqual(len(decisions), 6)
        self.assertTrue(all(e["decision"] == "DENY" for e in decisions))

    def test_noncanonical_request_envelope_never_reaches_the_handler(self):
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                self.sid,
                "device_control",
                {"request": {"value": [{"domain": "door", "device": "B", "action": "unlock"}]}},
            )
        self.assertEqual(self.effects, [])

    def test_saved_grant_revalidates_and_proxy_schema_keeps_pairs(self):
        self.service = self.restore()
        record = self.service.get_access("100", self.sid)
        normalized = self.service.normalize_scope(record["scope"], saved=True)
        self.assertEqual(record["scope"], normalized)
        schema = proxy_schema(record["scope"]["tools"][0])["parameters"]
        validator = Draft202012Validator(schema)
        validator.validate({"request": [{"domain": "door", "device": "B", "action": "unlock"}]})
        self.assertFalse(
            validator.is_valid(
                {"request": [{"domain": "light", "device": "B", "action": "unlock"}]}
            )
        )
        self.call("light", "D", "off")

    def test_child_can_narrow_one_parent_operation_and_revocation_stops_it(self):
        child = self.service.delegate(
            "200", self.sid, {"tools": [rule("light", ["C"], ["off"])]}, task="Switch C off"
        )
        self.service.call(
            "200",
            self.sid,
            "device_control",
            {"request": [{"domain": "light", "device": "C", "action": "off"}]},
            delegation=child["task_id"],
        )
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                self.sid,
                "device_control",
                {"request": [{"domain": "door", "device": "B", "action": "unlock"}]},
                delegation=child["task_id"],
            )
        p = self.service.prepare("100", "Alex", None, share_id=self.sid, action="revoke")
        self.service.approve("100", p["proposal_id"])
        with self.assertRaises(GuestsError):
            self.call("light", "C", "off")
        self.assertEqual(len(self.effects), 1)

    def test_expanding_legacy_rule_replaces_authority_without_changing_guest_or_expiry(self):
        legacy = {"tools": [rule("light", ["C"], ["on", "off"])], "duration_seconds": 3600}
        p = self.service.prepare("100", "Alex", legacy, share_id=self.sid)
        self.service.approve("100", p["proposal_id"])
        old = self.service.get_access("100", self.sid)
        self.service.call(
            "200", self.sid, "device_control", {"domain": "light", "device": "C", "action": "on"}
        )
        replacement = {"tools": self.scope["tools"], "expires_at": old["scope"]["expires_at"]}
        p = self.service.prepare("100", "Alex", replacement, share_id=self.sid)
        self.assertIn('"B"', p["text"])
        self.assertIn('"E"', p["text"])
        result = self.service.approve("100", p["proposal_id"])
        current = self.service.get_access("100", self.sid)
        self.assertEqual(result["share_id"], self.sid)
        self.assertEqual(current["guest_id"], old["guest_id"])
        self.assertEqual(current["scope"]["expires_at"], old["scope"]["expires_at"])
        self.assertNotEqual(current["warrant_id"], old["warrant_id"])
        self.assertIn(old["warrant_id"], current["old_warrants"])
        srl = SignedRevocationList.from_bytes((self.service.root / "revocations.srl").read_bytes())
        srl.verify(self.service.issuer.public_key)
        self.assertTrue(srl.is_revoked(old["warrant_id"]))
        self.call("door", "B", "unlock")
        self.call("light", "E", "off")

    def test_child_cannot_mix_or_expand_parent_rules(self):
        for requested in [
            rule("door", ["C"], ["unlock"]),
            rule("light", ["B"], ["on"]),
            rule("light", ["C", "kitchen"], ["off"]),
            rule("door", ["B"], ["unlock", "on"]),
        ]:
            with self.subTest(requested=requested), self.assertRaises(GuestsError):
                self.service.delegate("200", self.sid, {"tools": [requested]}, task="Expand access")
        self.assertEqual(self.effects, [])

    def test_grouped_schema_resolves_local_definitions(self):
        schema = copy.deepcopy(SCHEMA)
        schema["parameters"]["$defs"] = {"device_id": {"type": "string", "minLength": 1}}
        schema["parameters"]["properties"]["device"] = {"$ref": "#/$defs/device_id"}
        entry = normalize_tools(self.scope["tools"], lambda _: schema)[0]
        validator = Draft202012Validator(proxy_schema(entry)["parameters"])
        self.assertTrue(
            validator.is_valid({"request": [{"domain": "door", "device": "B", "action": "lock"}]})
        )
        self.assertFalse(
            validator.is_valid({"request": [{"domain": "door", "device": "C", "action": "lock"}]})
        )

    def test_schema_drift_blocks_grouped_calls(self):
        self.schema["parameters"]["properties"]["device"]["minLength"] = 2
        with self.assertRaises(GuestsError):
            self.call("light", "C", "on")
        self.assertEqual(self.effects, [])

    def test_renewal_preserves_every_permission_group(self):
        old = self.service.get_access("100", self.sid)
        p = self.service.renewal("200", self.sid)
        proposed = self.service.ledger.snapshot()["proposals"][p["proposal_id"]]["scope"]
        self.assertEqual(proposed["tools"], old["scope"]["tools"])
        self.service.approve("100", p["proposal_id"])
        self.call("door", "B", "lock")
        self.call("light", "E", "on")
        self.assertEqual(len(self.effects), 2)

    def test_grouped_child_keeps_optional_fields_required_by_parent_request(self):
        self.schema["parameters"]["required"] = ["device", "action"]
        parent = normalize_tools(self.scope["tools"], lambda _: self.schema)
        child = rule("door", ["B"], ["lock"])
        del child["constraints"]["domain"]
        with self.assertRaises(GuestsError):
            normalize_tools([child], lambda _: self.schema, parent=parent)

    def test_schema_change_between_groups_rejects_the_proposal(self):
        schemas = [copy.deepcopy(SCHEMA), copy.deepcopy(SCHEMA)]
        schemas[1]["description"] = "Changed service contract."
        with self.assertRaisesRegex(GuestsError, "changed during discovery"):
            normalize_tools(self.scope["tools"], lambda _: schemas.pop(0))

    def test_reference_rebasing_preserves_literal_values_and_named_fields(self):
        schema = {
            "properties": {"const": {"$ref": "#/$defs/value"}},
            "$defs": {"value": {"type": "array"}},
            "const": [{"$ref": "#/literal-data"}],
            "examples": [{"$ref": "#/example-data"}],
        }
        rebase_schema_refs(schema, "#/nested/")
        self.assertEqual(schema["properties"]["const"]["$ref"], "#/nested/$defs/value")
        self.assertEqual(schema["const"], [{"$ref": "#/literal-data"}])
        self.assertEqual(schema["examples"], [{"$ref": "#/example-data"}])
