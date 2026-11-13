"""Access that was ended, deleted or ran out stays that way until a fresh review says otherwise."""

import json
import tempfile
import time
import unittest
from unittest.mock import patch

from hermes_guests.core import GuestsService, signed_guest
from hermes_guests.model import GuestsError, iso
from hermes_guests.recovery import recover_local_authority
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState

CATALOG = {"light.porch": {"name": "Porch light"}}


class EndedAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = FileState(self.temp.name)
        self.effects = []
        self.now = time.time()
        self.s = self.service()

    def service(self, **settings):
        return GuestsService(
            self.state,
            "100",
            catalog=dict(CATALOG),
            dispatch=lambda tool, args, envelope: self.effects.append(tool) or {"state": "on"},
            tool_resolver=GUEST_SCHEMAS.get,
            clock=lambda: self.now,
            **settings,
        )

    def scope(self, seconds=3600):
        return {"lights": ["light.porch"], "research": False, "expires_at": iso(self.now + seconds)}

    def give(self, guest="200", name="Alex", telegram="Alex R"):
        p = self.s.prepare("100", name, self.scope())
        invitation = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invitation["token"], guest, telegram, username="alex_handle")
        active = self.s.accept_claim("100", claim["claim_id"])
        return active["share_id"], p["proposal_id"]

    def end(self, sid, action="revoke"):
        p = self.s.prepare("100", "Alex", None, share_id=sid, action=action)
        return self.s.approve("100", p["proposal_id"])

    def read(self, sid, guest="200"):
        return self.s.call(guest, sid, "ha_get_state", {"entity_id": "light.porch"})

    def test_an_accepted_invitation_cannot_be_replaced_after_the_guests_data_is_deleted(self):
        for action in ("revoke", "delete"):
            sid, review = self.give(guest="200" if action == "revoke" else "201")
            self.end(sid, action)
            with self.assertRaisesRegex(GuestsError, "already accepted"):
                self.s.refresh_invitation("100", review)
            statuses = [i["status"] for i in self.s.ledger.snapshot()["invites"].values()]
            self.assertEqual(set(statuses), {"used"}, action)  # spent, and it stays spent
        self.assertEqual(len([r for r in self.s.list_access("100") if r["status"] == "active"]), 0)

    def test_an_accepted_invitation_stays_spent_after_its_record_is_cleared_away(self):
        # Short invitation windows: upkeep clears the used invitation a little before the
        # review it came from.
        self.s = self.service(invitation_seconds=60, claim_seconds=60)
        p = self.s.prepare("100", "Alex", self.scope(20 * 86400))
        invitation = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invitation["token"], "200", "Alex R")
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        self.end(sid)
        self.now += 7 * 86400 + 200
        self.s.maintenance("100")
        db = self.s.ledger.snapshot()
        self.assertEqual(db["invites"], {})  # the case in question: the used invitation is gone
        self.assertIn(p["proposal_id"], db["proposals"])
        with self.assertRaises(GuestsError):
            self.s.refresh_invitation("100", p["proposal_id"])
        self.assertEqual(self.s.ledger.snapshot()["invites"], {})

    def test_an_invitation_with_no_record_left_needs_a_new_review(self):
        p = self.s.prepare("100", "Alex", self.scope(20 * 86400))
        self.s.approve("100", p["proposal_id"])
        with self.s.ledger.transaction() as db:  # state from before acceptance was marked
            db["invites"].clear()
        with self.assertRaisesRegex(GuestsError, "Prepare a new invitation"):
            self.s.refresh_invitation("100", p["proposal_id"])

    def test_an_invitation_still_open_is_withdrawn_when_the_guests_data_is_deleted(self):
        sid, _ = self.give()
        self.end(sid)
        again = self.s.prepare("100", "Someone else", self.scope())
        link = self.s.approve("100", again["proposal_id"])
        self.s.claim(link["token"], "200", "Alex R")  # claimed again, not yet confirmed
        self.end(sid, "delete")
        statuses = sorted(i["status"] for i in self.s.ledger.snapshot()["invites"].values())
        self.assertEqual(statuses, ["denied", "used"])

    def test_a_review_that_switches_ended_access_back_on_says_whose_account_it_is(self):
        sid, _ = self.give()
        fresh = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.assertNotIn("switches access back on", fresh["text"])  # still active: a change
        self.end(sid)
        back = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.assertIn("This switches access back on for the same Telegram account", back["text"])
        self.assertIn("Alex R · @alex_handle · ID 200", back["text"])
        self.assertIn("was ended", back["text"])
        self.assertIn("No invitation is sent", back["text"])
        saved = self.s.ledger.snapshot()["proposals"][back["proposal_id"]]
        self.assertEqual(saved["returns_access_to"]["guest_id"], "200")  # part of what is approved
        self.assertEqual(self.s.proposal_review(saved), back["text"])  # and of a restored card
        self.s.approve("100", back["proposal_id"])
        self.read(sid)
        # Access that only ran out says so, in the same place.
        self.now += 3 * 3600
        late = self.s.prepare("100", "Alex", self.scope(3600), share_id=sid)
        self.assertIn("ran out", late["text"])

    def test_permission_changes_keep_the_confirmed_account_name_for_later_reactivation(self):
        sid, _ = self.give()
        change = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.s.approve("100", change["proposal_id"])
        record = self.s.get_access("100", sid)
        self.assertEqual(record["telegram_name"], "Alex R")
        self.assertEqual(record["telegram_username"], "alex_handle")
        self.end(sid)
        again = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.assertIn("Alex R · @alex_handle · ID 200", again["text"])
        self.end(sid, "delete")
        record = self.s.get_access("100", sid)
        self.assertNotIn("telegram_name", record)
        self.assertNotIn("telegram_username", record)

    def test_access_is_not_switched_back_on_for_an_account_other_than_the_one_reviewed(self):
        sid, _ = self.give()
        self.end(sid)
        back = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.assertIn("ID 200", back["text"])
        with self.s.ledger.transaction() as db:  # the saved record now names someone else
            db["shares"][sid]["guest_id"] = "300"
        before = self.s.get_access("100", sid)
        with self.assertRaisesRegex(GuestsError, "account .* changed since this review"):
            self.s.approve("100", back["proposal_id"])
        after = self.s.get_access("100", sid)
        self.assertEqual(after["warrant_id"], before["warrant_id"])
        self.assertEqual(after["effective_status"], "revoked")
        self.assertEqual(self.effects, [])

    def test_access_whose_record_names_another_account_can_only_be_ended(self):
        sid, _ = self.give()
        other, _ = self.give(guest="300", name="Blair", telegram="Blair T")
        with self.s.ledger.transaction() as db:  # the saved record now names someone else
            db["shares"][sid]["guest_id"] = "300"
        before = self.s.ledger.snapshot()["shares"][sid]["warrant_id"]
        mismatch = "different account than it was approved for"

        def refused(action):
            with self.assertRaisesRegex(GuestsError, mismatch) as raised:
                self.s.prepare(
                    "100",
                    "Alex",
                    self.scope(7200) if action == "replace" else None,
                    share_id=sid,
                    action=action,
                )
            # What it tells the owner to do is something that works (below).
            self.assertIn("End this access", str(raised.exception))
            self.assertNotIn("guests_repair", str(raised.exception))

        refused("replace")
        self.assertEqual(self.s.repair("100")["paused"], [sid])
        refused("replace")  # no review is offered that could only be refused again
        # Deleting by account would take the other person's access and data with it.
        refused("delete")
        self.assertEqual(self.s.ledger.snapshot()["shares"][sid]["warrant_id"], before)
        self.end(sid)
        self.assertEqual(self.s.get_access("100", sid)["status"], "revoked")
        with self.assertRaises(GuestsError):
            self.read(sid, guest="300")
        self.read(other, guest="300")  # Blair's own access is untouched
        again, _ = self.give(guest="200", name="Alex again")  # and Alex can be invited afresh
        self.read(again)

    def test_deleting_one_persons_data_leaves_access_signed_for_someone_else(self):
        sid, _ = self.give()
        other, _ = self.give(guest="300", name="Blair", telegram="Blair T")
        with self.s.ledger.transaction() as db:  # Blair's record now names Alex's account
            db["shares"][other]["guest_id"] = "200"
        before = self.s.ledger.snapshot()["shares"][other]
        done = self.end(sid, "delete")
        self.assertEqual(done["shares"], [sid])
        db = self.s.ledger.snapshot()
        self.assertIsNotNone(db["shares"][sid].get("data_deletion_requested_at"))
        # Not revoked, not scrubbed, not marked for deletion: it was never Alex's.
        self.assertEqual(db["shares"][other], before)
        self.assertFalse(self.s._refresh_srl(db).is_revoked(before["warrant_id"]))
        with self.s.ledger.transaction() as db:  # put right, Blair carries on
            db["shares"][other]["guest_id"] = "300"
        self.read(other, guest="300")

    def test_a_review_prepared_before_the_record_changed_is_refused_at_approval_too(self):
        sid, _ = self.give()
        change = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        erase = self.s.prepare("100", "Alex", None, share_id=sid, action="delete")
        before = self.s.ledger.snapshot()["shares"][sid]
        with patch.object(self.s, "_signed_for_another", lambda record: True):
            for review in (change, erase):
                with self.assertRaisesRegex(GuestsError, "different account than it was approved"):
                    self.s.approve("100", review["proposal_id"])
        self.assertEqual(self.s.ledger.snapshot()["shares"][sid], before)

    def test_pending_review_without_account_binding_requires_a_new_review(self):
        from hermes_guests.core import digest

        sid, _ = self.give()
        review = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        with self.s.ledger.transaction() as db:
            proposal = db["proposals"][review["proposal_id"]]
            del proposal["reviewed_for"]
            del proposal["digest"]
            proposal["digest"] = digest(proposal)
        before = self.s.get_access("100", sid)
        with self.assertRaisesRegex(GuestsError, "Prepare a new review"):
            self.s.approve("100", review["proposal_id"])
        self.assertEqual(self.s.get_access("100", sid), before)

    def test_old_signed_account_marker_is_still_verified(self):
        from hermes_guests import core

        with patch.object(core, "GUEST_MARK", "share_guest_"):
            sid, _ = self.give()
        self.read(sid)
        with self.s.ledger.transaction() as db:
            db["shares"][sid]["guest_id"] = "300"
        with self.assertRaisesRegex(GuestsError, "different account than it was approved"):
            self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)

    def test_every_review_of_existing_access_is_for_the_account_it_was_prepared_for(self):
        from hermes_guests import core

        # Access given before grants were signed for their person has nothing signed to
        # compare with, so the review itself holds the account.
        with patch.object(core, "signed_guest", lambda warrant: None):
            for action in ("replace", "revoke", "delete"):
                with self.subTest(action=action):
                    sid, _ = self.give(guest=str(200 + len(action)))
                    review = self.s.prepare(
                        "100",
                        "Alex",
                        self.scope(7200) if action == "replace" else None,
                        share_id=sid,
                        action=action,
                    )
                    with self.s.ledger.transaction() as db:
                        db["shares"][sid]["guest_id"] = "300"
                    before = self.s.ledger.snapshot()["shares"][sid]
                    with self.assertRaisesRegex(GuestsError, "changed since this review"):
                        self.s.approve("100", review["proposal_id"])
                    self.assertEqual(self.s.ledger.snapshot()["shares"][sid], before)
                    with self.s.ledger.transaction() as db:  # put back for the next round
                        db["shares"][sid]["guest_id"] = str(200 + len(action))
                    self.end(sid)

    def test_the_owners_choice_of_how_long_guest_data_is_kept_survives_a_recovery(self):
        chosen = self.s.prepare("100", "Guest data", None, action="retention", retention_seconds=0)
        self.s.approve("100", chosen["proposal_id"])
        self.assertEqual(self.s.data_settings("100")["retention_seconds"], 0)
        recover_local_authority(self.state, "100")
        self.assertEqual(self.service().data_settings("100")["retention_seconds"], 0)

    def test_expiry_while_a_review_is_open_requires_a_new_account_review(self):
        sid, _ = self.give()
        self.now += 3590
        pending = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.assertNotIn(
            "returns_access_to", self.s.ledger.snapshot()["proposals"][pending["proposal_id"]]
        )
        before = self.s.get_access("100", sid)
        self.now += 20
        with self.assertRaisesRegex(GuestsError, "Access ended since this review"):
            self.s.approve("100", pending["proposal_id"])
        after = self.s.get_access("100", sid)
        self.assertEqual(after["warrant_id"], before["warrant_id"])
        self.assertEqual(after["effective_status"], "expired")
        with self.assertRaises(GuestsError):
            self.read(sid)
        self.assertEqual(self.effects, [])
        fresh = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        self.assertIn("Alex R · @alex_handle · ID 200", fresh["text"])
        self.s.approve("100", fresh["proposal_id"])
        self.s.clock = time.time  # SDK proof verification uses the real wall clock.
        self.read(sid)
        self.assertEqual(self.effects, ["ha_get_state"])

    def test_deletion_scrubs_names_from_invitations_and_reactivation_reviews(self):
        sid, original = self.give()
        self.end(sid)
        pending = self.s.prepare("100", "Alex", self.scope(7200), share_id=sid)
        invitation = self.s.prepare("100", "Another invitation", self.scope())
        link = self.s.approve("100", invitation["proposal_id"])
        claim = self.s.claim(link["token"], "200", "Alex R", username="alex_handle")
        other, _ = self.give(guest="300", name="Blair", telegram="Blair T")
        self.end(sid, "delete")
        db = self.s.ledger.snapshot()
        self.assertNotIn("Alex R", json.dumps(db))
        # The other guest uses the fixture's default username; it must remain intact.
        for inv in db["invites"].values():
            if inv.get("guest_id") == "200":
                self.assertNotIn("guest_username", inv)
                self.assertNotIn("guest_label", inv)
        back = db["proposals"][pending["proposal_id"]]
        self.assertNotIn("name", back["returns_access_to"])
        self.assertNotIn("username", back["returns_access_to"])
        self.assertEqual(back["status"], "denied")
        self.assertEqual(db["shares"][other]["telegram_name"], "Blair T")
        self.assertEqual(db["shares"][other]["telegram_username"], "alex_handle")
        with self.assertRaises(GuestsError):
            self.s.approve("100", pending["proposal_id"])
        with self.assertRaises(GuestsError):
            self.s.accept_claim("100", claim["claim_id"])
        with self.assertRaisesRegex(GuestsError, "already accepted"):
            self.s.refresh_invitation("100", original)

    def test_a_grant_is_signed_for_its_person_and_refused_under_any_other(self):
        sid, _ = self.give()
        record = self.s.get_access("100", sid)
        self.assertEqual(signed_guest(self.s._warrant(record)), "200")
        self.read(sid)
        with self.s.ledger.transaction() as db:
            db["shares"][sid]["guest_id"] = "300"  # the record now names someone else
        for attempt in (
            lambda: self.read(sid, "300"),
            lambda: self.s.admit_chat("300", sid),
            lambda: self.s.admit_memory("300", sid, "memory", operation="read"),
        ):
            with self.assertRaises(GuestsError):
                attempt()
        self.assertEqual(self.effects, ["ha_get_state"])  # only the call before the change
        self.assertEqual(self.s.repair("100")["paused"], [sid])  # and repair does not vouch for it
        with self.assertRaises(GuestsError):  # the name that carries the person is not a tool
            self.s.call("200", sid, "guests_guest_200", {})

    def test_access_with_no_end_date_stays_signed_for_its_person_when_it_is_continued(self):
        for name, value in (("STANDING_SECONDS", 3600), ("STANDING_RENEW", 10**9)):  # always due
            patcher = patch("hermes_guests.model." + name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.s.clock = time.time
        p = self.s.prepare(
            "100", "Alex", {"lights": ["light.porch"], "research": False, "until_ended": True}
        )
        claim = self.s.claim(self.s.approve("100", p["proposal_id"])["token"], "200", "Alex R")
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        first = self.s.guest("200")["warrant_id"]
        self.read(sid)  # the call continues it
        record = self.s.guest("200")
        self.assertNotEqual(record["warrant_id"], first)
        self.assertEqual(signed_guest(self.s._warrant(record)), "200")
        self.assertEqual(self.effects, ["ha_get_state"])

    def test_requests_for_more_time_are_limited_like_requests_for_more_access(self):
        sid, _ = self.give()
        self.now += 2 * 3600  # it ran out
        first = self.s.renewal("200", sid)
        self.now += 400
        with self.assertRaisesRegex(GuestsError, "has not answered"):
            self.s.renewal("200", sid)  # one waiting at a time
        asked = 1
        self.assertTrue(first["proposal_id"])
        for _ in range(10):
            with self.s.ledger.transaction() as db:  # the owner lets each one lapse unanswered
                for proposal in db["proposals"].values():
                    proposal["valid_until"] = min(proposal["valid_until"], self.now - 1)
            self.now += 400
            try:
                self.s.renewal("200", sid)
                asked += 1
            except GuestsError as refused:
                self.assertIn("a few times today", str(refused))
                break
        self.assertEqual(asked, 3)
        self.now += 86400
        self.s.renewal("200", sid)  # and again the next day

    def test_requests_for_more_time_are_counted_per_person_not_per_access(self):
        first, _ = self.give()
        self.now += 2 * 3600  # it ran out; the owner invites the same person again
        second, _ = self.give()
        self.now += 2 * 3600
        self.assertNotEqual(first, second)
        self.s.renewal("200", first)
        self.now += 400
        with self.assertRaisesRegex(GuestsError, "has not answered your last request"):
            self.s.renewal("200", second)  # the old access's button is not a second allowance
        asked = 1
        for sid in (second, first, second, first):
            for p in self.s.ledger.snapshot()["proposals"].values():
                if p.get("renewal") and p["status"] == "pending":
                    self.s.deny("100", p["id"])
            self.now += 400
            try:
                self.s.renewal("200", sid)
                asked += 1
            except GuestsError as refused:
                self.assertIn("asked a few times today", str(refused))
        self.assertEqual(asked, 3)

    def test_taking_back_an_old_request_for_more_time_leaves_a_newer_one_counted(self):
        sid, _ = self.give()
        old = self.s.renewal("200", sid)
        first = self.now
        self.now += 700  # the owner was never reached, and that review has run out
        self.s.renewal("200", sid)
        second = self.now
        self.s.withdraw_renewal(sid, old["proposal_id"])
        record = self.s.ledger.snapshot()["shares"][sid]
        self.assertEqual(record["renewals_asked"], [second])
        self.assertEqual(record["renewal_requested"], second)
        self.assertNotEqual(first, second)

    def test_a_request_for_more_time_that_never_reached_the_owner_is_not_counted(self):
        sid, _ = self.give()
        broken = GuestsError("A tool changed since this review. Prepare a new access review.")
        with patch.object(self.s, "prepare", side_effect=broken):
            for _ in range(4):  # more than a day's allowance of failures
                self.now += 400
                with self.assertRaisesRegex(GuestsError, "A tool changed"):
                    self.s.renewal("200", sid)
        self.assertEqual(self.s.ledger.snapshot()["shares"][sid].get("renewals_asked", []), [])
        # Put right, the request goes through. If the owner then cannot be told, it is
        # taken back: nothing is left waiting and the guest may ask again at once.
        review = self.s.renewal("200", sid)
        self.s.withdraw_renewal(sid, review["proposal_id"])
        db = self.s.ledger.snapshot()
        self.assertEqual(db["proposals"][review["proposal_id"]]["status"], "denied")
        self.assertEqual(db["shares"][sid].get("renewals_asked", []), [])
        with self.assertRaises(GuestsError):  # a review that is no longer open cannot be approved
            self.s.approve("100", review["proposal_id"])
        for _ in range(3):
            answered = self.s.renewal("200", sid)
            self.s.deny("100", answered["proposal_id"])
            self.now += 400
        with self.assertRaisesRegex(GuestsError, "asked a few times today"):
            self.s.renewal("200", sid)

    def test_deleting_a_guests_data_also_removes_what_they_wrote_and_their_telegram_name(self):
        sid, _ = self.give()
        self.s.note_more_request("200", sid, "could I also use the office light please")
        saved = self.s.ledger.snapshot()["shares"][sid]
        self.assertIn("office light", str(saved))
        self.assertEqual(saved["telegram_username"], "alex_handle")
        self.end(sid, "delete")
        saved = self.s.ledger.snapshot()["shares"][sid]
        self.assertNotIn("office light", str(saved))
        for gone in ("more_open", "more_asked", "telegram_name", "telegram_username"):
            self.assertNotIn(gone, saved)
        self.assertEqual((saved["guest_id"], saved["label"]), ("200", "Alex"))  # kept for the audit


if __name__ == "__main__":
    unittest.main()
