import copy
import tempfile
import time
import unittest

from tenuo import verify_receipt

from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError, iso, review, timestamp
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState

CATALOG = {
    "light.porch": {"name": "Porch light"},
    "light.guest": {"name": "Guest room light"},
    "light.all": {"name": "All lights", "group": True},
}


def setUpModule():
    """Guest calls look web addresses up before they are admitted; these tests stay offline."""
    from unittest.mock import patch

    from hermes_guests import public_web

    patcher = patch.object(public_web, "lookup", lambda host: ["93.184.216.34"])
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = FileState(self.temp.name)
        self.effects = []
        self.s = GuestsService(
            self.state,
            "100",
            catalog=CATALOG,
            dispatch=self.dispatch,
            tool_resolver=GUEST_SCHEMAS.get,
        )

    def dispatch(self, tool, args, envelope):
        self.effects.append((tool, args, envelope))
        return {"success": True, "state": "on"}

    def access(self, *, lights=None, research=False, days=1):
        return {
            "lights": lights or [],
            "research": research,
            "expires_at": iso(time.time() + days * 86400),
        }

    def activate(self, guest="200", access=None):
        p = self.s.prepare("100", "Alex", access or self.access(lights=["light.porch"]))
        inv = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(inv["token"], guest, "Alex")
        return self.s.accept_claim("100", claim["claim_id"])

    def test_chat_only_signed_lifecycle_never_dispatches(self):
        from hermes_guests.model import CHAT_ONLY_CAPABILITY

        active = self.activate(access={"chat_only": True, "duration_seconds": 3600})
        sid = active["share_id"]
        for tool in (CHAT_ONLY_CAPABILITY, "ha_get_state", "terminal"):
            with self.subTest(tool=tool), self.assertRaises(GuestsError):
                self.s.call("200", sid, tool, {})
        self.assertEqual(self.effects, [])
        with self.assertRaises(GuestsError):
            self.s.delegate("200", sid, {"tools": []}, task="Execute anything")
        self.s.admit_chat("200", sid)
        self.assertIn("Chat only", active["text"])
        p = self.s.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.s.approve("100", p["proposal_id"])
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, CHAT_ONLY_CAPABILITY, {})
        with self.assertRaises(GuestsError):
            self.s.admit_chat("200", sid)
        self.assertEqual(self.effects, [])

    def test_configurable_handoff_windows_preserve_access_end(self):
        clock = [time.time()]
        self.s.clock = lambda: clock[0]
        self.s.review_seconds = 1200
        self.s.invitation_seconds = 7200
        self.s.claim_seconds = 900
        p = self.s.prepare("100", "Alex", self.access(lights=["light.porch"]))
        self.assertEqual(p["expires_at"], clock[0] + 1200)
        end = self.s.ledger.snapshot()["proposals"][p["proposal_id"]]["scope"]["expires_at"]
        invitation = self.s.approve("100", p["proposal_id"])
        clock[0] += 1500
        claim = self.s.claim(invitation["token"], "200", "Alex")
        db = self.s.ledger.snapshot()
        self.assertEqual(next(iter(db["invites"].values()))["claim_until"], clock[0] + 900)
        self.assertEqual(db["proposals"][p["proposal_id"]]["scope"]["expires_at"], end)
        self.assertTrue(claim["claim_id"])

    def test_invitation_window_is_reviewed_preserved_on_retry_and_not_cut_by_access_length(self):
        now = [round(self.s.clock(), 3)]
        self.s.clock = lambda: now[0]
        p = self.s.prepare(
            "100",
            "Alex",
            {
                "lights": ["light.porch"],
                "duration_seconds": 172800,
            },
            invitation_seconds=86400,
        )
        self.assertIn("within 24 hours after sending", p["text"])
        invitation = self.s.approve("100", p["proposal_id"])
        self.assertEqual(timestamp(invitation["expires_at"]), now[0] + 86400)
        now[0] += 3600
        replacement = self.s.refresh_invitation("100", p["proposal_id"])
        self.assertEqual(timestamp(replacement["expires_at"]), now[0] + 86400)
        claim = self.s.claim(replacement["token"], "200", "Alex")
        active = self.s.accept_claim("100", claim["claim_id"])
        self.assertEqual(
            timestamp(self.s.get_access("100", active["share_id"])["scope"]["expires_at"]),
            now[0] + 172800,  # the length starts when the owner confirms the account
        )
        short = self.s.prepare(
            "100",
            "Other",
            {
                "chat_only": True,
                "duration_seconds": 300,
            },
            invitation_seconds=86400,
        )
        # Five minutes of access is five minutes from when it starts, so the invitation
        # can still be opened later in its own window.
        short_invite = self.s.approve("100", short["proposal_id"])
        self.assertEqual(timestamp(short_invite["expires_at"]), now[0] + 86400)

    def test_invitation_window_tampering_is_rejected_and_legacy_proposals_still_work(self):
        p = self.s.prepare("100", "Alex", self.access(lights=["light.porch"]))
        with self.s.ledger.transaction() as db:
            db["proposals"][p["proposal_id"]]["invitation_seconds"] = 86400
        with self.assertRaisesRegex(GuestsError, "review changed"):
            self.s.approve("100", p["proposal_id"])
        legacy = self.s.prepare("100", "Legacy", self.access(lights=["light.porch"]))
        from hermes_guests.model import digest

        with self.s.ledger.transaction() as db:
            saved = db["proposals"][legacy["proposal_id"]]
            saved.pop("invitation_seconds")
            saved["digest"] = digest({k: v for k, v in saved.items() if k != "digest"})
        self.assertEqual(self.s.approve("100", legacy["proposal_id"])["status"], "invited")
        for invalid in (True, "86400", 59, 2592001, 0):
            with self.subTest(invalid=invalid), self.assertRaises(GuestsError):
                self.s.prepare(
                    "100", "Alex", self.access(lights=["light.porch"]), invitation_seconds=invalid
                )
        sid = self.activate()["share_id"]
        with self.assertRaisesRegex(GuestsError, "Only new invitations"):
            self.s.prepare(
                "100",
                "Alex",
                self.access(lights=["light.porch"]),
                share_id=sid,
                invitation_seconds=86400,
            )

    def test_account_confirmation_binding_uses_configured_claim_window(self):
        now = [round(self.s.clock(), 3)]
        self.s.clock = lambda: now[0]
        self.s.claim_seconds = 900
        p = self.s.prepare("100", "Alex", self.access(lights=["light.porch"]))
        invite = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invite["token"], "200", "Alex")
        self.s.present_review("100", "claim", claim["claim_id"][:24], 1, "100")
        now[0] += 700
        self.s.check_review("100", "claim", claim["claim_id"][:24], 1, "100")
        self.assertEqual(self.s.accept_claim("100", claim["claim_id"])["status"], "active")

    def test_invalid_handoff_setting_fails_before_identity_creation(self):
        for seconds in (True, 0, 86401, "600"):
            with self.subTest(seconds=seconds), self.assertRaises(GuestsError):
                GuestsService(self.state, "100", review_seconds=seconds)

    def test_light_preset_can_delegate_only_state_reading(self):
        sid = self.activate()["share_id"]
        requested = {
            "tools": [
                {"name": "ha_get_state", "constraints": {"entity_id": {"equals": "light.porch"}}}
            ]
        }
        child = self.s.delegate("200", sid, requested, task="Read porch state")
        self.s.call(
            "200", sid, "ha_get_state", {"entity_id": "light.porch"}, delegation=child["task_id"]
        )
        for tool, args in [
            ("ha_get_state", {"entity_id": "light.guest"}),
            (
                "ha_call_service",
                {"domain": "light", "service": "turn_on", "entity_id": "light.porch"},
            ),
        ]:
            with self.subTest(tool=tool), self.assertRaises(GuestsError):
                self.s.call("200", sid, tool, args, delegation=child["task_id"])
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(GuestsError):
            self.s.delegate("200", sid, {"tools": [{"name": []}]}, task="Malformed tool")
        for rule in ({"any": True}, {"equals": "light.guest"}):
            with self.subTest(rule=rule), self.assertRaises(GuestsError):
                self.s.delegate(
                    "200",
                    sid,
                    {"tools": [{"name": "ha_get_state", "constraints": {"entity_id": rule}}]},
                    task="Broader read",
                )

    def test_research_preset_delegates_search_without_projecting_url_predicate_as_any(self):
        sid = self.activate(access=self.access(research=True))["share_id"]
        selected = {
            "tools": [
                {
                    "name": "web_search",
                    "constraints": {"query": {"equals": "python docs"}, "limit": {"range": [1, 2]}},
                }
            ]
        }
        child = self.s.delegate("200", sid, selected, task="Search Python docs")
        self.s.call(
            "200",
            sid,
            "web_search",
            {"query": "python docs", "limit": 2},
            delegation=child["task_id"],
        )
        with self.assertRaises(GuestsError):
            self.s.call(
                "200",
                sid,
                "web_search",
                {"query": "other topic", "limit": 2},
                delegation=child["task_id"],
            )
        with self.assertRaises(GuestsError):
            self.s.delegate(
                "200",
                sid,
                {"tools": [{"name": "web_extract", "constraints": {"urls": {"any": True}}}]},
                task="Read arbitrary URLs",
            )
        self.assertEqual(len(self.effects), 1)

    def test_preset_child_rejects_schema_drift(self):
        schemas = copy.deepcopy(GUEST_SCHEMAS)
        self.s.tool_resolver = schemas.get
        sid = self.activate()["share_id"]
        schemas["ha_get_state"]["description"] += " Changed integration."
        with self.assertRaisesRegex(GuestsError, "changed"):
            self.s.delegate(
                "200",
                sid,
                {
                    "tools": [
                        {
                            "name": "ha_get_state",
                            "constraints": {"entity_id": {"equals": "light.porch"}},
                        }
                    ]
                },
                task="Read porch",
            )
        self.assertFalse(self.s.delegates)

    def test_no_authority_before_actual_guest_confirmation(self):
        p = self.s.prepare("100", "Alex", self.access(lights=["light.porch"]))
        inv = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(inv["token"], "200", "Actual Alex")
        self.assertEqual(self.s.list_access("200"), [])
        with self.assertRaises(GuestsError):
            self.s.accept_claim("200", claim["claim_id"])
        active = self.s.accept_claim("100", claim["claim_id"])
        self.assertEqual(active["guest_id"], "200")
        with self.assertRaises(GuestsError):
            self.s.claim(inv["token"], "300", "Forwarded claimant")

    def test_owner_confirmation_and_revision_are_required(self):
        active = self.activate()
        p1 = self.s.prepare("100", "Alex", self.access(research=True), share_id=active["share_id"])
        p2 = self.s.prepare(
            "100", "Alex", self.access(lights=["light.guest"]), share_id=active["share_id"]
        )
        self.s.approve("100", p1["proposal_id"])
        with self.assertRaises(GuestsError):
            self.s.approve("100", p2["proposal_id"])
        with self.assertRaises(GuestsError):
            self.s.prepare("200", "Alex", self.access(research=True))

    def test_complete_review_matches_requested_scope(self):
        text = review(
            "Alex", self.access(lights=["light.porch", "light.guest"], research=True), CATALOG
        )
        self.assertIn("Porch light", text)
        self.assertIn("Guest room light", text)
        self.assertIn("public web", text)
        self.assertIn("Scope is limited to the capabilities and inputs above", text)

    def test_effect_boundary_and_signed_allow_deny_receipts(self):
        a = self.activate()
        sid = a["share_id"]
        self.s.call(
            "200",
            sid,
            "ha_call_service",
            {"domain": "light", "service": "turn_on", "entity_id": "light.porch"},
        )
        with self.assertRaises(GuestsError):
            self.s.call(
                "200",
                sid,
                "ha_call_service",
                {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
            )
        self.assertEqual(len(self.effects), 1)
        path = self.s.audit.receipt_path
        import json

        receipts = [json.loads(x)["receipt"] for x in path.read_text().splitlines()]
        self.assertEqual(len(receipts), 2)
        for receipt in receipts:
            self.assertIsNotNone(verify_receipt(receipt))
        rows = self.s.activity("100", sid)
        self.assertEqual(
            [x["decision"] for x in rows if x["kind"] == "decision"], ["ALLOW", "DENY"]
        )

    def test_wrong_guest_and_unknown_guest_have_no_effects(self):
        a = self.activate()
        with self.assertRaises(GuestsError):
            self.s.call("300", a["share_id"], "ha_get_state", {"entity_id": "light.porch"})
        with self.assertRaises(GuestsError):
            self.s.get_access("300", a["share_id"])
        self.assertEqual(self.effects, [])

    def test_expansion_reduction_and_revoke_survive_restart(self):
        a = self.activate()
        sid = a["share_id"]
        initial_end = self.s.get_access("100", sid)["scope"]["expires_at"]
        expanded = {
            "lights": ["light.porch", "light.guest"],
            "research": True,
            "expires_at": initial_end,
        }
        p = self.s.prepare("100", "Alex", expanded, share_id=sid)
        self.s.approve("100", p["proposal_id"])
        self.s.call("200", sid, "ha_get_state", {"entity_id": "light.guest"})
        self.s.call("200", sid, "web_search", {"query": "python"})
        reduced = {"lights": ["light.porch"], "research": False, "expires_at": initial_end}
        p = self.s.prepare("100", "Alex", reduced, share_id=sid)
        self.s.approve("100", p["proposal_id"])
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "web_search", {"query": "python"})
        p = self.s.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.s.approve("100", p["proposal_id"])
        restored = GuestsService(
            self.state,
            "100",
            catalog=CATALOG,
            dispatch=self.dispatch,
            tool_resolver=GUEST_SCHEMAS.get,
        )
        with self.assertRaises(GuestsError):
            restored.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        with self.assertRaises(GuestsError):
            restored.renewal("200", sid)

    def test_expiry_requests_renewal_without_automatic_activation(self):
        a = self.activate()
        sid = a["share_id"]
        self.s.clock = lambda: time.time() + 2 * 86400
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        p = self.s.renewal("200", sid)
        self.assertEqual(self.s.get_access("100", sid)["effective_status"], "expired")
        self.assertIn("Porch light", p["text"])

    def test_unsupported_scope_and_hidden_arguments_are_refused(self):
        for lights in [["light.missing"], ["light.all"]]:
            with self.assertRaises(GuestsError):
                self.s.prepare("100", "Alex", self.access(lights=lights))
        a = self.activate()
        with self.assertRaises(GuestsError):
            self.s.call(
                "200",
                a["share_id"],
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": "light.porch",
                    "data": '{"area_id":"whole_house"}',
                },
            )
        self.assertEqual(self.effects, [])

    def test_automatic_delegation_never_needs_owner_confirmation(self):
        a = self.activate(access=self.access(lights=["light.porch", "light.guest"], research=True))
        sid = a["share_id"]
        before = copy.deepcopy(self.s.ledger.snapshot())
        d = self.s.delegate(
            "200", sid, {"lights": ["light.porch"], "research": False}, task="Check the porch light"
        )
        self.assertEqual(
            self.s.ledger.snapshot(),
            before,
            "Child issuance must not create proposals or mutate approved guest access.",
        )
        self.s.call(
            "200", sid, "ha_get_state", {"entity_id": "light.porch"}, delegation=d["task_id"]
        )
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "web_search", {"query": "python"}, delegation=d["task_id"])
        with self.assertRaises(GuestsError):
            self.s.call(
                "200", sid, "ha_get_state", {"entity_id": "light.guest"}, delegation=d["task_id"]
            )
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(len(self.effects[0][2]["chain"]), 2)
        self.assertNotIn("private", str(self.effects[0][2]))

    def test_delegation_cannot_widen_or_outlive_parent(self):
        a = self.activate()
        sid = a["share_id"]
        before = copy.deepcopy(self.s.ledger.snapshot())
        for req in [
            {"research": True},
            {"lights": ["light.guest"]},
            {"lights": ["light.porch"], "expires_at": iso(time.time() + 1000)},
        ]:
            with self.assertRaises(GuestsError):
                self.s.delegate("200", sid, req, task="Overbroad subtask")
        self.assertEqual(
            self.s.ledger.snapshot(), before, "Rejected narrowing must not trigger owner approval."
        )

    def test_revoke_invalidates_existing_delegates(self):
        a = self.activate()
        sid = a["share_id"]
        d = self.s.delegate("200", sid, {"lights": ["light.porch"]}, task="Check")
        p = self.s.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.s.approve("100", p["proposal_id"])
        with self.assertRaises(GuestsError):
            self.s.call(
                "200", sid, "ha_get_state", {"entity_id": "light.porch"}, delegation=d["task_id"]
            )
        self.assertEqual(self.effects, [])

    def test_corrupt_artifact_pauses_instead_of_reminting(self):
        a = self.activate()
        sid = a["share_id"]
        r = self.s.get_access("100", sid)
        path = self.s.root / "warrants" / (r["warrant_id"] + ".warrant")
        path.write_bytes(b"corrupt")
        result = self.s.repair("100")
        self.assertIn(sid, result["paused"])
        self.assertEqual(path.read_bytes(), b"corrupt")
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})

    def test_slow_service_does_not_block_revocation_and_no_new_calls_are_admitted(self):
        import threading

        active = self.activate()
        sid = active["share_id"]
        entered = threading.Event()
        release = threading.Event()
        revoked = threading.Event()
        failures = []

        def slow_dispatch(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("fixture timed out")
            return {"state": "on"}

        self.s.dispatch = slow_dispatch

        def call():
            try:
                self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
            except Exception as exc:
                failures.append(type(exc).__name__)

        worker = threading.Thread(target=call)
        worker.start()
        self.assertTrue(entered.wait(2))

        def revoke():
            p = self.s.prepare("100", "Alex", None, share_id=sid, action="revoke")
            self.s.approve("100", p["proposal_id"])
            revoked.set()

        control = threading.Thread(target=revoke)
        control.start()
        try:
            self.assertTrue(
                revoked.wait(2), "The owner must not wait for a slow service to end new access."
            )
            with self.assertRaises(GuestsError):
                self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        finally:
            release.set()
            worker.join(3)
            control.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(self.s.in_flight, {})

    def test_research_reads_public_pages_and_blocks_private_urls_without_effects(self):
        active = self.activate(access=self.access(research=True))
        sid = active["share_id"]
        self.s.call("200", sid, "web_extract", {"urls": ["https://example.com/research"]})
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "web_extract", {"urls": ["http://127.0.0.1/private"]})
        self.assertEqual(len(self.effects), 1)
        self.assertTrue(any(e["kind"] == "adapter_blocked" for e in self.s.activity("100", sid)))

    def test_owner_review_can_repair_a_lost_guest_holder_without_renewing_scope(self):
        active = self.activate()
        sid = active["share_id"]
        saved = self.s.get_access("100", sid)["scope"]
        key = self.s.root / "control" / (sid + ".key")
        key.write_bytes(b"corrupt")
        self.assertIn(sid, self.s.repair("100")["paused"])
        request = {k: v for k, v in saved.items() if k != "preset_schemas"}
        p = self.s.prepare("100", "Alex", request, share_id=sid)
        self.s.approve("100", p["proposal_id"])
        self.assertEqual(self.s.get_access("100", sid)["scope"]["expires_at"], saved["expires_at"])
        self.s.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        self.assertEqual(len(self.effects), 1)

    def test_an_old_grant_cannot_be_restored_alongside_the_same_persons_new_active_grant(self):
        old = self.activate()
        self.s.clock = lambda: time.time() + 2 * 86400
        access = {
            "lights": ["light.porch"],
            "research": False,
            "expires_at": iso(self.s.clock() + 86400),
        }
        new = self.activate(access=access)
        proposal = self.s.prepare("100", "Alex", access, share_id=old["share_id"])
        with self.assertRaisesRegex(GuestsError, "current access"):
            self.s.approve("100", proposal["proposal_id"])
        self.assertEqual(
            [r["id"] for r in self.s.list_access("100") if r["effective_status"] == "active"],
            [new["share_id"]],
        )

    def test_research_excerpts_do_not_expose_host_cache_paths_or_unavailable_file_instructions(
        self,
    ):
        active = self.activate(access=self.access(research=True))
        text = "Useful public content\n\n──────── [TRUNCATED] ────────\nShowing an excerpt.\nFull text saved to: /host-private/cache/page.txt\nTo read the omitted middle: read_file path=local-file\n─────────────────────────────"
        self.s.dispatch = lambda *args: {
            "results": [{"url": "https://example.com", "content": text}]
        }
        result = self.s.call(
            "200", active["share_id"], "web_extract", {"urls": ["https://example.com"]}
        )
        self.assertIn("Useful public content", result["results"][0]["content"])
        self.assertNotIn("host-private", str(result))
        self.assertNotIn("read_file", str(result))
        self.assertIn("page excerpt", str(result))

    def test_owner_identity_cannot_change_on_recovery(self):
        self.activate()
        with self.assertRaises(GuestsError):
            GuestsService(self.state, "300", catalog=CATALOG, tool_resolver=GUEST_SCHEMAS.get)


if __name__ == "__main__":
    unittest.main()
