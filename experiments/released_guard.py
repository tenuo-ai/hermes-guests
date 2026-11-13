"""Compatibility probes against published packages, without a Hermes process or model.

The tool names below are synthetic. Passing proves guard/API behavior only.
Run with the pinned environment from experiments/requirements.txt.
"""

import importlib.metadata
import time
import unittest
from datetime import datetime, timezone

from hermes_tenuo import HermesGuard
from tenuo import Exact, SigningKey, Warrant


class ReleasedGuardTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(importlib.metadata.version("hermes-tenuo"), "0.4.0")
        self.assertEqual(importlib.metadata.version("tenuo"), "0.3.7")
        self.issuer = SigningKey.generate()
        self.holder = SigningKey.generate()
        self.guard = HermesGuard(
            trusted_roots=[self.issuer.public_key],
            on_denial="block",
            require_session_warrant=True,
        )
        self.effects = []

    def warrant(self, *, lights=False, ttl=120):
        builder = (
            Warrant.mint_builder()
            .holder(self.holder.public_key)
            .ttl(ttl)
            .capability("media_search", library=Exact("movies"), query=Exact("sci-fi"))
        )
        if lights:
            builder = builder.capability(
                "light_set", entity=Exact("light.guest_room"), state=Exact("on")
            )
        return builder.mint(self.issuer)

    def call(self, session, tool, args):
        result = self.guard.pre_tool_call(tool, args, session_id=session)
        if result is None:
            self.effects.append((session, tool))
        return result

    def test_unknown_session_has_zero_effects(self):
        self.assertEqual(
            self.call("unknown", "media_search", {"library": "movies", "query": "sci-fi"})[
                "action"
            ],
            "block",
        )
        self.assertEqual(self.effects, [])

    def test_scope_and_service_denials_have_zero_forbidden_effects(self):
        self.guard.set_session_warrant("alex", self.warrant(), self.holder)
        self.assertIsNone(
            self.call("alex", "media_search", {"library": "movies", "query": "sci-fi"})
        )
        self.assertIsNotNone(
            self.call("alex", "media_search", {"library": "private", "query": "sci-fi"})
        )
        self.assertIsNotNone(
            self.call("alex", "light_set", {"entity": "light.guest_room", "state": "on"})
        )
        self.assertEqual(self.effects, [("alex", "media_search")])

    def test_live_expansion_does_not_expand_another_session(self):
        original = self.warrant()
        self.guard.set_session_warrant("alex", original, self.holder)
        self.guard.set_session_warrant("sam", original, self.holder)
        args = {"entity": "light.guest_room", "state": "on"}
        self.assertIsNotNone(self.call("alex", "light_set", args))
        remaining = int(
            (
                datetime.fromisoformat(original.expires_at().replace("Z", "+00:00"))
                - datetime.now(timezone.utc)
            ).total_seconds()
        )
        expanded = self.warrant(lights=True, ttl=remaining)
        self.assertLessEqual(expanded.expires_at(), original.expires_at())
        self.guard.set_session_warrant("alex", expanded, self.holder)
        self.assertIsNone(self.call("alex", "light_set", args))
        self.assertIsNotNone(self.call("sam", "light_set", args))
        self.assertEqual(self.effects, [("alex", "light_set")])

    def test_live_reduction_stops_removed_capability(self):
        args = {"entity": "light.guest_room", "state": "on"}
        self.guard.set_session_warrant("alex", self.warrant(lights=True), self.holder)
        self.assertIsNone(self.call("alex", "light_set", args))
        self.guard.set_session_warrant("alex", self.warrant(), self.holder)
        self.assertIsNotNone(self.call("alex", "light_set", args))
        self.assertEqual(self.effects, [("alex", "light_set")])

    def test_local_detachment_stops_only_that_session(self):
        warrant = self.warrant()
        for session in ("alex", "sam"):
            self.guard.set_session_warrant(session, warrant, self.holder)
        self.guard.clear_session_warrant("alex")
        args = {"library": "movies", "query": "sci-fi"}
        self.assertIsNotNone(self.call("alex", "media_search", args))
        self.assertIsNone(self.call("sam", "media_search", args))
        self.assertEqual(self.effects, [("sam", "media_search")])

    def test_expiry_stops_later_effects(self):
        self.guard.set_session_warrant("alex", self.warrant(ttl=1), self.holder)
        args = {"library": "movies", "query": "sci-fi"}
        self.assertIsNone(self.call("alex", "media_search", args))
        time.sleep(1.2)
        self.assertIsNotNone(self.call("alex", "media_search", args))
        self.assertEqual(self.effects, [("alex", "media_search")])

    def test_wrong_holder_has_zero_effects(self):
        self.guard.set_session_warrant("alex", self.warrant(), SigningKey.generate())
        self.assertIsNotNone(
            self.call("alex", "media_search", {"library": "movies", "query": "sci-fi"})
        )
        self.assertEqual(self.effects, [])

    def test_explicit_child_chain_and_unknown_session_isolation(self):
        parent = self.warrant()
        child_key = SigningKey.generate()
        child = (
            parent.grant_builder()
            .holder(child_key.public_key)
            .capability("media_search", library=Exact("movies"), query=Exact("sci-fi"))
            .ttl(30)
            .grant(self.holder)
        )
        self.guard.set_session_warrant("child", child, child_key, parent_warrant=parent)
        args = {"library": "movies", "query": "sci-fi"}
        self.assertIsNone(self.call("child", "media_search", args))
        self.assertIsNotNone(self.call("unrelated", "media_search", args))
        self.assertEqual(self.effects, [("child", "media_search")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
