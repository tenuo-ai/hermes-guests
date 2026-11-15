"""The owner's never-share list removes tools and values from every path to a guest."""

import tempfile
import time
import unittest

from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError, iso
from hermes_guests.never import NeverShare, fold
from hermes_guests.store import FileState
from tests.tiers import slow

LIBRARY = {
    "name": "library_find",
    "description": "Find titles in a media library.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "library": {"type": "string", "enum": ["Movies", "TV", "Home Videos"]},
            "query": {"type": "string"},
        },
        "required": ["library", "query"],
    },
}
LIGHT = {
    "name": "light_set",
    "description": "Turn a light on or off.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "light": {"type": "string", "enum": ["guest_room", "office", "front_door_lock"]},
            "state": {"type": "string", "enum": ["on", "off"]},
        },
        "required": ["light", "state"],
    },
}
TOOLS = {"library_find": LIBRARY, "light_set": LIGHT}
ANY_LIGHT = {"name": "light_set", "constraints": {"light": {"any": True}, "state": {"any": True}}}


class MatchingTests(unittest.TestCase):
    def test_a_value_is_found_however_it_is_spelled_or_nested(self):
        never = NeverShare(values=["Front_Door_Lock"])
        for carried in (
            "front_door_lock",
            "FRONT_DOOR_LOCK",
            "  front_door_lock  ",
            "lock.front_door_lock",  # inside a longer identifier
            "front_door​_lock",  # an invisible character inside
            "ｆｒｏｎｔ_ｄｏｏｒ_ｌｏｃｋ",  # full-width letters
            ["office", ["x", {"deep": "Front_Door_Lock"}]],
            {"front_door_lock": "on"},  # as a key
        ):
            self.assertEqual(never.blocked_in(carried), "Front_Door_Lock", carried)
        for clean in ("front door", "office", 42, None, True, {"light": "guest_room"}):
            self.assertIsNone(never.blocked_in(clean), clean)

    def test_a_character_shown_as_nothing_does_not_hide_a_listed_word(self):
        never = NeverShare(values=["front_door_lock"])
        hidden = (
            0x00AD, 0x034F, 0x061C, 0x115F, 0x1160, 0x180B, 0x200B, 0x200D, 0x202E, 0x2060,
            0x2064, 0x3164, 0xFE00, 0xFE0F, 0xFEFF, 0xFFA0, 0x1D173, 0xE0001, 0xE0020, 0xE0100,
        )  # fmt: skip
        for code in hidden:
            word = "front" + chr(code) + "_door" + chr(code) * 3 + "_lock"
            self.assertEqual(never.blocked_in(word), "Front_Door_Lock".lower(), hex(code))
        self.assertEqual(fold("fro\ufe0fnt\u3164"), "front")
        # Visible marks still make a different word, and a space still separates words.
        self.assertIsNone(never.blocked_in("frönt_door_lock"))
        self.assertIsNone(never.blocked_in("front _door_lock"))

    def test_choices_kept_behind_a_schema_reference_are_seen(self):
        from hermes_guests.permissions import describe_inputs, example_entry, validate_schema

        referenced = {
            "name": "library_find",
            "description": "Find titles in a media library.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "$defs": {
                    "Library": {"type": "string", "enum": ["Movies", "TV", "Home Videos"]},
                    "Limit": {"type": "integer", "minimum": 1, "maximum": 5},
                    "Again": {"$ref": "#/$defs/Library"},
                },
                "properties": {
                    "library": {"$ref": "#/$defs/Again"},
                    "limit": {"$ref": "#/$defs/Limit"},
                    "query": {"type": "string"},
                },
                "required": ["library", "query"],
            },
        }
        checked = validate_schema(referenced)
        said = describe_inputs(checked)
        self.assertIn('library (one of "Movies", "TV", "Home Videos")', said)
        self.assertIn("limit (whole number 1 to 5, optional)", said)
        entry = example_entry(checked)
        self.assertEqual(
            entry["constraints"]["library"], {"one_of": ["Movies", "TV", "Home Videos"]}
        )
        self.assertEqual(entry["constraints"]["limit"], {"range": [1, 5]})
        effects = []
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        s = GuestsService(
            FileState(temp.name),
            "100",
            tool_resolver=lambda name: referenced,
            dispatch=lambda tool, args, envelope: effects.append(args) or {"ok": True},
        )
        s.never_set("100", "values", "Home Videos", True)
        rules = {"library": {"any": True}, "query": {"any": True}}
        p = s.prepare(
            "100",
            "Alex",
            {"tools": [{"name": "library_find", "constraints": rules}], "duration_seconds": 3600},
        )
        self.assertEqual(p["left_out"], ["library_find · library: Home Videos"])  # now said
        self.assertNotIn("Home Videos", p["text"].split("Left out")[0])
        claim = s.claim(s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        sid = s.accept_claim("100", claim["claim_id"])["share_id"]
        s.call("200", sid, "library_find", {"library": "Movies", "query": "x"})
        with self.assertRaises(GuestsError):
            s.call("200", sid, "library_find", {"library": "Home Videos", "query": "x"})
        self.assertEqual(effects, [{"library": "Movies", "query": "x"}])

    def test_a_value_inside_a_web_address_is_found_when_encoded(self):
        never = NeverShare(values=["tax records"])
        self.assertEqual(
            never.blocked_in({"urls": ["https://example.com/tax%20records/2024"]}), "tax records"
        )
        self.assertEqual(never.blocked_in("https://example.com/?q=tax+records"), "tax records")
        self.assertIsNone(never.blocked_in("https://example.com/taxes"))

    def test_an_input_nested_too_deeply_to_inspect_is_refused(self):
        deep = "front_door_lock"
        for _ in range(20):
            deep = {"more": [deep]}
        self.assertIsNotNone(NeverShare(values=["Front_Door_Lock"]).blocked_in(deep))
        self.assertIsNotNone(
            NeverShare(values=["something else"]).blocked_in(deep)
        )  # unchecked is not clean
        self.assertIsNone(NeverShare().blocked_in(deep))  # nothing listed: nothing to check

    def test_choices_of_a_list_input_are_left_to_the_call_check(self):
        tags = {
            "name": "tag_set",
            "description": "Set tags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["office", "front_door_lock"]},
                    }
                },
            },
        }
        never = NeverShare(values=["front_door_lock"])
        entry = {"name": "tag_set", "constraints": {"tags": {"any": True}}}
        self.assertEqual(never.lock(entry, tags), (entry, []))  # the rule is not rewritten
        self.assertEqual(
            never.blocked_for("tag_set", {"tags": ["office", "front_door_lock"]}), "front_door_lock"
        )

    def test_numbers_and_short_values(self):
        self.assertEqual(NeverShare(values=["4242"]).blocked_in({"pin": 4242}), "4242")
        self.assertIsNone(NeverShare(values=["ab"]).blocked_in("abacus"))  # too short to be kept
        self.assertEqual(fold("  Home Videos "), "home videos")

    def test_tool_names_match_exactly_not_by_part(self):
        never = NeverShare(tools=["Light_Set"])
        self.assertTrue(never.blocks_tool("light_set"))
        self.assertFalse(never.blocks_tool("light_settings"))


class OneToolTests(unittest.TestCase):
    def test_a_word_listed_for_one_tool_is_refused_only_there(self):
        never = NeverShare(scoped=[{"tool": "library_find", "value": "Comedy"}])
        self.assertEqual(
            never.blocked_for("library_find", {"library": "Movies", "query": "a comedy night"}),
            "Comedy",
        )
        self.assertEqual(never.blocked_for("LIBRARY_FIND", {"query": "ＣＯＭＥＤＹ"}), "Comedy")
        self.assertIsNone(never.blocked_for("page_read", {"url": "https://example.com/comedy"}))
        self.assertIsNone(never.blocked_in({"query": "comedy"}))  # not listed for every tool

    def test_a_word_listed_for_one_input_is_refused_only_in_that_input(self):
        never = NeverShare(
            scoped=[{"tool": "light_set", "field": "light", "value": "front_door_lock"}]
        )
        self.assertEqual(
            never.blocked_for("light_set", {"light": "front_door_lock", "state": "on"}),
            "front_door_lock",
        )
        self.assertIsNone(
            never.blocked_for("light_set", {"light": "office", "state": "front_door_lock"})
        )
        self.assertIsNone(never.blocked_for("library_find", {"query": "front_door_lock"}))

    @slow
    def test_one_tool_rules_narrow_that_tools_choices_only(self):
        never = NeverShare(
            scoped=[{"tool": "light_set", "field": "light", "value": "front_door_lock"}]
        )
        entry, gone = never.lock(ANY_LIGHT, LIGHT)
        self.assertEqual(entry["constraints"]["light"], {"one_of": ["guest_room", "office"]})
        self.assertEqual(entry["constraints"]["state"], {"any": True})
        self.assertEqual(gone, ["light_set · light: front_door_lock"])
        other = {
            "name": "library_find",
            "constraints": {"library": {"any": True}, "query": {"equals": "front_door_lock"}},
        }
        self.assertEqual(never.lock(other, LIBRARY), (other, []))


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.effects = []
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog={
                "light.porch": {"name": "Porch light"},
                "light.front_door": {"name": "Front door"},
            },
            dispatch=lambda tool, args, envelope: self.effects.append((tool, args)) or {"ok": True},
            tool_resolver=TOOLS.get,
            clock=time.time,
        )

    def scope(self, *tools, **more):
        return {"tools": list(tools), "expires_at": iso(time.time() + 3600), **more}

    def activate(self, scope, guest="200"):
        p = self.s.prepare("100", "Alex", scope)
        claim = self.s.claim(self.s.approve("100", p["proposal_id"])["token"], guest, "Alex")
        return p, self.s.accept_claim("100", claim["claim_id"])["share_id"]

    def test_only_the_owner_edits_the_list(self):
        with self.assertRaises(GuestsError):
            self.s.never_set("200", "tools", "light_set", True)
        with self.assertRaises(GuestsError):
            self.s.never_set("100", "values", "ab", True)
        self.s.never_set("100", "values", "front​_door_lock", True)
        self.assertEqual(self.s.never().values, ["front_door_lock"])
        self.s.never_set("100", "values", "FRONT_DOOR_LOCK", True)  # the same value again
        self.assertEqual(len(self.s.never().values), 1)
        self.s.never_set("100", "values", "front_door_lock", False)
        self.assertFalse(self.s.never())

    def test_a_listed_tool_cannot_be_put_in_a_grant(self):
        self.s.never_set("100", "tools", "light_set", True)
        with self.assertRaises(GuestsError) as refused:
            self.s.prepare("100", "Alex", self.scope(ANY_LIGHT))
        self.assertIn("never-share list", str(refused.exception))
        self.s.prepare(
            "100",
            "Alex",
            self.scope(
                {
                    "name": "library_find",
                    "constraints": {"library": {"any": True}, "query": {"any": True}},
                }
            ),
        )

    def test_a_listed_value_is_taken_out_of_the_choices_and_the_owner_is_told(self):
        self.s.never_set("100", "values", "front_door_lock", True)
        p = self.s.prepare("100", "Alex", self.scope(ANY_LIGHT))
        self.assertEqual(p["left_out"], ["light_set · light: front_door_lock"])
        saved = self.s.ledger.snapshot()["proposals"][p["proposal_id"]]["scope"]["tools"][0]
        self.assertEqual(saved["constraints"]["light"], {"one_of": ["guest_room", "office"]})
        self.assertNotIn("front_door_lock", p["text"])
        listed = dict(
            ANY_LIGHT,
            constraints={
                "light": {"one_of": ["office", "front_door_lock"]},
                "state": {"any": True},
            },
        )
        self.assertEqual(
            self.s.prepare("100", "Alex", self.scope(listed))["left_out"],
            ["light_set · light: front_door_lock"],
        )
        for rule in (
            {"equals": "front_door_lock"},
            {"equals": "FRONT_DOOR_LOCK"},
            {"one_of": ["front_door_lock"]},
        ):
            exact = dict(ANY_LIGHT, constraints={"light": rule, "state": {"any": True}})
            with self.assertRaises(GuestsError):
                self.s.prepare("100", "Alex", self.scope(exact))

    def test_presets_respect_the_list(self):
        self.s.never_set("100", "values", "front door", True)
        with self.assertRaises(GuestsError):
            self.s.prepare(
                "100",
                "Alex",
                {
                    "lights": ["light.front_door"],
                    "research": False,
                    "expires_at": iso(time.time() + 3600),
                },
            )
        self.s.never_set("100", "tools", "web_search", True)
        with self.assertRaises(GuestsError):
            self.s.prepare(
                "100",
                "Alex",
                {"lights": [], "research": True, "expires_at": iso(time.time() + 3600)},
            )

    def test_a_guest_call_carrying_a_listed_value_is_refused_with_no_effect(self):
        _, sid = self.activate(
            self.scope(
                {
                    "name": "library_find",
                    "constraints": {"library": {"any": True}, "query": {"any": True}},
                }
            )
        )
        self.s.call("200", sid, "library_find", {"library": "Movies", "query": "comedy"})
        self.assertEqual(len(self.effects), 1)
        self.s.never_set("100", "values", "taxes 2024", True)
        for query in ("taxes 2024", "TAXES  2024", "my Taxes​ 2024 folder", "ｔａｘｅｓ 2024"):
            with self.assertRaises(GuestsError) as refused:
                self.s.call("200", sid, "library_find", {"library": "Movies", "query": query})
            self.assertIn("does not share", str(refused.exception))
        self.assertEqual(len(self.effects), 1)  # nothing more reached the service
        self.s.call("200", sid, "library_find", {"library": "Movies", "query": "taxes"})
        self.assertEqual(len(self.effects), 2)  # other requests still work
        kinds = [r["kind"] for r in self.s.activity("100", sid)]
        self.assertEqual(kinds.count("never_share_blocked"), 4)

    def test_adding_to_the_list_cuts_off_access_that_already_exists(self):
        _, sid = self.activate(self.scope(ANY_LIGHT))
        self.s.call("200", sid, "light_set", {"light": "front_door_lock", "state": "on"})
        self.assertEqual(len(self.effects), 1)  # allowed before the list
        self.s.never_set("100", "values", "front_door_lock", True)
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "light_set", {"light": "front_door_lock", "state": "on"})
        self.s.call("200", sid, "light_set", {"light": "office", "state": "on"})
        self.s.never_set("100", "tools", "light_set", True)
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "light_set", {"light": "office", "state": "off"})
        self.assertEqual(len(self.effects), 2)
        self.s.never_set(
            "100", "tools", "light_set", False
        )  # taking it off restores the grant as reviewed
        self.s.never_set("100", "values", "front_door_lock", False)
        self.s.call("200", sid, "light_set", {"light": "front_door_lock", "state": "off"})
        self.assertEqual(len(self.effects), 3)

    def test_people_taken_over_at_setup_get_nothing_from_the_list(self):
        self.s.never_set("100", "values", "front_door_lock", True)
        self.s.never_set("100", "tools", "library_find", True)
        self.s.setup_owner("100", {"light_set": "x", "library_find": "y"})
        with self.assertRaises(GuestsError):
            self.s.adopt("100", "300", "Blair", ["light_set", "library_find"])
        self.s.adopt("100", "300", "Blair", ["light_set"])
        tools = self.s.guest("300")["scope"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["light_set"])
        self.assertEqual(tools[0]["constraints"]["light"], {"one_of": ["guest_room", "office"]})

    def test_the_owners_own_tool_use_is_not_limited_by_the_list(self):
        self.s.setup_owner("100", {"light_set": "x"})
        self.s.never_set("100", "tools", "light_set", True)
        self.s.never_set("100", "values", "front_door_lock", True)
        self.assertIsNone(
            self.s.check_owner(
                "100", "light_set", {"light": "front_door_lock", "state": "on"}, "s1"
            )
        )

    def test_a_one_tool_rule_holds_for_grants_and_calls_of_that_tool_only(self):
        both = self.scope(
            ANY_LIGHT,
            {
                "name": "library_find",
                "constraints": {"library": {"any": True}, "query": {"any": True}},
            },
        )
        _, sid = self.activate(both)
        self.s.never_set("100", "scoped", "comedy", True, tool="library_find")
        self.s.never_set("100", "scoped", "front_door_lock", True, tool="light_set", field="light")
        with self.assertRaises(GuestsError):
            self.s.never_set("100", "scoped", "x y z", True, tool="no_such_tool")
        with self.assertRaises(GuestsError):
            self.s.never_set("100", "scoped", "x y z", True, tool="light_set", field="nope")
        for tool, args in (
            ("library_find", {"library": "Movies", "query": "Comedy classics"}),
            ("light_set", {"light": "front_door_lock", "state": "on"}),
        ):
            with self.assertRaises(GuestsError):
                self.s.call("200", sid, tool, args)
        self.assertEqual(self.effects, [])
        self.s.call(
            "200", sid, "library_find", {"library": "Movies", "query": "front_door_lock manual"}
        )
        self.s.call("200", sid, "light_set", {"light": "office", "state": "on"})
        self.assertEqual(len(self.effects), 2)
        p = self.s.prepare("100", "Blair", both)
        self.assertEqual(p["left_out"], ["light_set · light: front_door_lock"])
        self.s.never_set("100", "scoped", "comedy", False, tool="library_find")
        self.s.call("200", sid, "library_find", {"library": "Movies", "query": "comedy"})
        self.assertEqual(len(self.effects), 3)

    def test_a_rule_for_one_input_applies_to_a_light_preset(self):
        from hermes_guests.schemas import GUEST_SCHEMAS

        s = GuestsService(
            FileState(self.temp.name + "/lights"),
            "100",
            catalog={"light.porch": {"name": "Porch"}, "light.office": {"name": "Office"}},
            tool_resolver=GUEST_SCHEMAS.get,
        )
        s.never_set("100", "scoped", "light.porch", True, tool="ha_get_state", field="entity_id")

        def lights(*chosen):
            return {"lights": list(chosen), "research": False, "duration_seconds": 3600}

        with self.assertRaisesRegex(GuestsError, "never-share list"):
            s.prepare("100", "Alex", lights("light.porch"))
        s.prepare("100", "Alex", lights("light.office"))
        # The preset promises on and off: with one of them listed it is not offered as a set.
        s.never_set("100", "scoped", "turn_off", True, tool="ha_call_service", field="service")
        with self.assertRaisesRegex(GuestsError, "cannot be shared as a set"):
            s.prepare("100", "Alex", lights("light.office"))
        s.never_set("100", "scoped", "turn_off", False, tool="ha_call_service", field="service")
        s.prepare("100", "Alex", lights("light.office"))

    def test_more_choices_than_a_rule_can_list_do_not_stop_the_tool_being_shared(self):
        rooms = [f"room_{i:03d}" for i in range(150)]
        many = {
            "name": "library_find",
            "description": "Find titles in a media library.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"library": {"type": "string", "enum": rooms}},
                "required": ["library"],
            },
        }
        effects = []
        s = GuestsService(
            FileState(self.temp.name + "/many"),
            "100",
            tool_resolver=lambda name: many,
            dispatch=lambda tool, args, envelope: effects.append(args) or {"ok": True},
        )
        s.never_set("100", "values", "room_007", True)
        rules = {"library": {"any": True}}
        p = s.prepare(
            "100",
            "Alex",
            {"tools": [{"name": "library_find", "constraints": rules}], "duration_seconds": 3600},
        )
        self.assertEqual(p["left_out"], ["library_find · library: room_007"])  # still said
        claim = s.claim(s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        sid = s.accept_claim("100", claim["claim_id"])["share_id"]
        s.call("200", sid, "library_find", {"library": "room_008"})
        with self.assertRaisesRegex(GuestsError, "does not share"):
            s.call("200", sid, "library_find", {"library": "room_007"})
        self.assertEqual(effects, [{"library": "room_008"}])

    def test_choices_that_are_objects_are_left_for_the_check_on_each_call(self):
        sorting = {
            "name": "library_find",
            "description": "Find titles in a media library.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "library": {"type": "string", "enum": ["Movies", "TV"]},
                    "sort": {"type": "object", "enum": [{"by": "year"}, {"by": "title"}]},
                },
                "required": ["library", "sort"],
            },
        }
        effects = []
        s = GuestsService(
            FileState(self.temp.name + "/objects"),
            "100",
            tool_resolver=lambda name: sorting,
            dispatch=lambda tool, args, envelope: effects.append(args) or {"ok": True},
        )
        s.never_set("100", "values", "year", True)
        rules = {"library": {"any": True}, "sort": {"any": True}}
        p = s.prepare(
            "100",
            "Alex",
            {"tools": [{"name": "library_find", "constraints": rules}], "duration_seconds": 3600},
        )
        self.assertEqual(p["left_out"], [])  # nothing the review could narrow
        claim = s.claim(s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        sid = s.accept_claim("100", claim["claim_id"])["share_id"]
        s.call("200", sid, "library_find", {"library": "TV", "sort": {"by": "title"}})
        with self.assertRaisesRegex(GuestsError, "does not share"):
            s.call("200", sid, "library_find", {"library": "TV", "sort": {"by": "year"}})
        self.assertEqual(effects, [{"library": "TV", "sort": {"by": "title"}}])

    def test_repair_leaves_access_intact_when_one_of_its_tools_is_listed(self):
        both = self.scope(
            ANY_LIGHT,
            {
                "name": "library_find",
                "constraints": {"library": {"any": True}, "query": {"any": True}},
            },
        )
        _, sid = self.activate(both)
        self.s.never_set("100", "tools", "light_set", True)
        self.assertEqual(self.s.repair("100")["paused"], [])
        self.assertEqual(self.s.guest("200")["effective_status"], "active")
        self.s.call(
            "200", sid, "library_find", {"library": "Movies", "query": "x"}
        )  # the other tool still works
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "light_set", {"light": "office", "state": "on"})
        self.s.never_set("100", "tools", "light_set", False)
        self.s.call(
            "200", sid, "light_set", {"light": "office", "state": "on"}
        )  # and taking it off restores it
        self.assertEqual(len(self.effects), 2)

    def test_resetting_the_sharing_identity_keeps_the_list(self):
        from hermes_guests.recovery import recover_local_authority

        self.s.never_set("100", "tools", "light_set", True)
        self.s.never_set("100", "values", "front_door_lock", True)
        recover_local_authority(self.s.ledger.state, "100")
        kept = self.s.ledger.snapshot()["setup"]["never_share"]
        self.assertEqual((kept["tools"], kept["values"]), (["light_set"], ["front_door_lock"]))

    def test_a_review_made_before_the_tool_was_listed_cannot_be_activated(self):
        p = self.s.prepare("100", "Alex", self.scope(ANY_LIGHT))
        invite = self.s.approve("100", p["proposal_id"])
        self.s.never_set("100", "tools", "light_set", True)
        claim = self.s.claim(invite["token"], "200", "Alex")
        with self.assertRaises(GuestsError):
            self.s.accept_claim("100", claim["claim_id"])
        self.assertEqual(self.s.list_access("100"), [])


if __name__ == "__main__":
    unittest.main()
