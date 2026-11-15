"""Lengths of access start when the owner approves; renewals, notices and activity keep time."""

import json
import os
import tempfile
import time
import unittest

from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError, iso, length_text, parse_length, timestamp
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState

CATALOG = {"light.porch": {"name": "Porch light"}}


class TimeAndRenewalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = float(int(time.time()))  # whole seconds: saved times round-trip exactly
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog=dict(CATALOG),
            dispatch=lambda tool, args, envelope: {"state": "on"},
            tool_resolver=GUEST_SCHEMAS.get,
            clock=lambda: self.now,
        )

    def give(self, seconds=900, *, accept_after=0):
        p = self.s.prepare("100", "Alex", {"lights": ["light.porch"], "duration_seconds": seconds})
        invitation = self.s.approve("100", p["proposal_id"])
        self.now += accept_after
        claim = self.s.claim(invitation["token"], "200", "Alex R")
        return self.s.accept_claim("100", claim["claim_id"])["share_id"]

    def test_a_length_starts_when_the_owner_confirms_the_guest(self):
        start = self.now
        sid = self.give(900, accept_after=600)  # the guest opened the link ten minutes later
        record = self.s.get_access("100", sid)
        self.assertEqual(timestamp(record["scope"]["expires_at"]), start + 600 + 900)
        self.assertEqual(record["duration_seconds"], 900)

    def test_renewal_offers_the_same_length_from_approval(self):
        sid = self.give(900)
        self.now += 1000  # it ran out
        review = self.s.renewal("200", sid)
        saved = self.s.ledger.snapshot()["proposals"][review["proposal_id"]]
        self.assertEqual(saved["scope"]["duration_seconds"], 900)
        self.now += 120  # the owner approves two minutes later
        self.s.approve("100", review["proposal_id"])
        record = self.s.get_access("100", sid)
        self.assertEqual(timestamp(record["scope"]["expires_at"]), self.now + 900)
        self.assertEqual(record["effective_status"], "active")

    def test_a_guest_can_ask_for_a_different_length(self):
        sid = self.give(900)
        review = self.s.renewal("200", sid, 6 * 3600)
        saved = self.s.ledger.snapshot()["proposals"][review["proposal_id"]]
        self.assertEqual(saved["more_seconds"], 6 * 3600)  # still on: added to the end
        with self.assertRaises(GuestsError):
            self.s.renewal("200", sid, 30)

    def test_approving_one_change_retires_the_others(self):
        sid = self.give(900)
        self.now += 1000
        asked = self.s.renewal("200", sid)
        owners = self.s.prepare(
            "100", "Alex", {"lights": ["light.porch"], "duration_seconds": 1800}, share_id=sid
        )
        self.s.approve("100", owners["proposal_id"])
        with self.assertRaisesRegex(GuestsError, "newer change"):
            self.s.approve("100", asked["proposal_id"])

    def deliver(self):
        """Send every due notice, as the plugin does, and say which were due."""
        due = self.s.due_notices()
        for kind, record in due:
            self.s.ack_notice(kind, record)
        return [k for k, _ in due]

    def test_guests_are_told_before_and_when_access_ends_once_each(self):
        sid = self.give(900)
        self.assertEqual(self.deliver(), [])
        self.now += 900 - 120
        self.assertEqual(self.deliver(), ["ending"])
        self.assertEqual(self.deliver(), [])
        self.now += 130
        notices = self.s.due_notices()
        self.assertEqual([k for k, _ in notices], ["ended", "owner_ended"])
        self.assertEqual(notices[0][1]["id"], sid)
        self.assertEqual(self.deliver(), ["ended", "owner_ended"])
        self.assertEqual(self.deliver(), [])

    def test_a_notice_that_could_not_be_delivered_is_sent_again(self):
        self.give(900)
        self.now += 1000
        due = self.s.due_notices()
        self.s.ack_notice(*due[1])  # the owner was told; the guest's chat failed
        self.assertEqual([k for k, _ in self.s.due_notices()], ["ended"])

    def test_a_too_long_request_can_be_corrected_at_once(self):
        sid = self.give(900)
        with self.assertRaises(GuestsError):
            self.s.renewal("200", sid, 31 * 86400)
        self.s.renewal("200", sid, 3600)  # not refused as "already sent recently"

    def test_short_access_gets_no_warning_only_the_end(self):
        self.give(240)
        self.now += 150
        self.assertEqual(self.s.due_notices(), [])
        self.now += 100
        self.assertEqual(self.deliver(), ["ended", "owner_ended"])

    def test_a_weekend_visit_keeps_its_end_and_its_invitation_lasts_the_weekend(self):
        # "Until Sunday 9 pm, and keep the invite open all weekend." The guest opens the
        # link a day later at 2 am; the owner confirms in the morning. Sunday stays Sunday.
        sunday = self.now + 2 * 86400
        p = self.s.prepare(
            "100",
            "Maya",
            {"lights": ["light.porch"], "expires_at": iso(sunday)},
            invitation_seconds=3 * 86400,
        )
        invitation = self.s.approve("100", p["proposal_id"])
        self.assertEqual(timestamp(invitation["expires_at"]), sunday)  # not past the end
        self.now += 86400
        claim = self.s.claim(invitation["token"], "200", "Maya")
        self.now += 6 * 3600  # the owner was asleep
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        record = self.s.get_access("100", sid)
        self.assertEqual(timestamp(record["scope"]["expires_at"]), sunday)
        self.assertNotIn("duration_seconds", record)

    def test_short_access_with_an_invitation_open_for_a_day(self):
        # "15 minutes, and let the invite stay open for a day." She opens it 20 hours later
        # and still gets her 15 minutes, counted from the owner's confirmation.
        p = self.s.prepare(
            "100",
            "Sam",
            {"lights": ["light.porch"], "duration_seconds": 900},
            invitation_seconds=86400,
        )
        invitation = self.s.approve("100", p["proposal_id"])
        self.assertEqual(timestamp(invitation["expires_at"]), self.now + 86400)
        self.now += 20 * 3600
        claim = self.s.claim(invitation["token"], "200", "Sam")
        self.now += 300
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        record = self.s.get_access("100", sid)
        self.assertEqual(timestamp(record["scope"]["expires_at"]), self.now + 900)

    def test_an_invitation_never_outlives_a_fixed_end(self):
        # "Until 9 pm tonight, keep the invite open a day": the link dies at 9 pm.
        tonight = self.now + 5 * 3600
        p = self.s.prepare(
            "100",
            "Sam",
            {"lights": ["light.porch"], "expires_at": iso(tonight)},
            invitation_seconds=86400,
        )
        invitation = self.s.approve("100", p["proposal_id"])
        self.assertEqual(timestamp(invitation["expires_at"]), tonight)
        self.now = tonight + 60
        with self.assertRaises(GuestsError):
            self.s.claim(invitation["token"], "200", "Sam")

    def test_long_access_gets_a_day_to_open_the_invitation(self):
        week = self.s.prepare("100", "Mom", {"lights": ["light.porch"], "duration_seconds": 604800})
        short = self.s.prepare("100", "Sam", {"lights": ["light.porch"], "duration_seconds": 900})
        proposals = self.s.ledger.snapshot()["proposals"]
        self.assertEqual(proposals[week["proposal_id"]]["invitation_seconds"], 86400)
        self.assertEqual(proposals[short["proposal_id"]]["invitation_seconds"], 1800)

    def test_more_time_while_access_is_on_is_added_to_the_end(self):
        sid = self.give(3600)
        end = timestamp(self.s.get_access("100", sid)["scope"]["expires_at"])
        self.now += 3000  # ten minutes left
        review = self.s.renewal("200", sid, 3600)
        saved = self.s.ledger.snapshot()["proposals"][review["proposal_id"]]
        self.assertEqual(saved["more_seconds"], 3600)
        self.now += 120  # the owner answers a little later; nothing is lost
        self.s.approve("100", review["proposal_id"])
        record = self.s.get_access("100", sid)
        self.assertEqual(timestamp(record["scope"]["expires_at"]), end + 3600)

    def test_only_declined_requests_count_toward_the_daily_limit(self):
        sid = self.give(900)
        for _ in range(5):  # approved requests never use up the guest's allowance
            self.now += 400
            self.s.approve("100", self.s.renewal("200", sid, 900)["proposal_id"])
        for _ in range(3):
            self.now += 400
            self.s.deny("100", self.s.renewal("200", sid, 900)["proposal_id"])
        self.now += 400
        with self.assertRaisesRegex(GuestsError, "said no a few times"):
            self.s.renewal("200", sid, 900)
        self.now += 86400
        self.s.renewal("200", sid, 900)

    def test_an_approved_change_answers_an_open_request_for_more(self):
        sid = self.give(3600)
        asked = self.s.note_more_request("200", sid, "the kitchen speaker too")
        self.assertEqual(len(self.s.open_requests("100")), 1)
        change = self.s.prepare(
            "100", "Alex", {"lights": ["light.porch"], "duration_seconds": 3600}, share_id=sid
        )
        self.s.approve("100", change["proposal_id"])
        self.assertEqual(self.s.open_requests("100"), [])
        with self.assertRaisesRegex(GuestsError, "already answered"):
            self.s.answer_more_request("100", sid, asked["token"])
        self.s.note_more_request("200", sid, "and the bedroom")  # they can ask again

    def test_a_declined_request_for_more_counts(self):
        sid = self.give(3600)
        for _ in range(3):
            asked = self.s.note_more_request("200", sid, "more")
            self.s.answer_more_request("100", sid, asked["token"], declined=True)
        with self.assertRaisesRegex(GuestsError, "said no a few times"):
            self.s.note_more_request("200", sid, "more")

    def test_guest_time_zone_from_the_owner_or_the_guest(self):
        p = self.s.prepare(
            "100",
            "Mom",
            {"lights": ["light.porch"], "duration_seconds": 3600},
            guest_zone="America/Vancouver",
        )
        invitation = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invitation["token"], "200", "Linda")
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        record = self.s.get_access("100", sid)
        self.assertEqual(self.s.zone_of(record), "America/Vancouver")
        self.s.set_guest_zone("200", sid, "Europe/London")
        self.assertEqual(self.s.zone_of(self.s.get_access("100", sid)), "Europe/London")
        with self.assertRaises(GuestsError):
            self.s.set_guest_zone("200", sid, "Mars/Olympus")
        with self.assertRaises(GuestsError):
            self.s.set_guest_zone("300", sid, "Europe/Paris")
        # A later change keeps where they are.
        change = self.s.prepare(
            "100", "Mom", {"lights": ["light.porch"], "duration_seconds": 7200}, share_id=sid
        )
        self.s.approve("100", change["proposal_id"])
        self.assertEqual(self.s.zone_of(self.s.get_access("100", sid)), "Europe/London")

    def test_without_a_guest_time_zone_the_owners_is_used(self):
        sid = self.give(900)
        self.assertEqual(self.s.zone_of(self.s.get_access("100", sid)), self.s.zone)

    def test_the_owners_name_for_a_guest_wins_unless_set_otherwise(self):
        p = self.s.prepare("100", "Mom", {"lights": ["light.porch"], "duration_seconds": 3600})
        invitation = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invitation["token"], "200", "Linda")
        self.assertIn("Mom", claim["text"])
        sid = self.s.accept_claim("100", claim["claim_id"])["share_id"]
        record = self.s.get_access("100", sid)
        self.assertEqual(record["label"], "Mom")
        self.assertEqual(record["telegram_name"], "Linda")

    def test_telegram_names_when_the_owner_prefers_them(self):
        s = GuestsService(
            FileState(self.temp.name + "/t"),
            "100",
            catalog=dict(CATALOG),
            dispatch=lambda *a: {},
            tool_resolver=GUEST_SCHEMAS.get,
            guest_names="telegram",
        )
        p = s.prepare("100", "Mom", {"lights": ["light.porch"], "duration_seconds": 3600})
        invitation = s.approve("100", p["proposal_id"])
        claim = s.claim(invitation["token"], "200", "Linda")
        sid = s.accept_claim("100", claim["claim_id"])["share_id"]
        self.assertEqual(s.get_access("100", sid)["label"], "Linda")
        with self.assertRaises(GuestsError):
            GuestsService(
                FileState(self.temp.name + "/u"),
                "100",
                catalog={},
                dispatch=lambda *a: {},
                tool_resolver=GUEST_SCHEMAS.get,
                guest_names="nickname",
            )

    def test_an_undelivered_end_notice_is_kept_past_the_first_hour(self):
        self.give(900)
        self.now += 900 + 5 * 3600  # Telegram was unreachable for hours
        self.assertEqual([k for k, _ in self.s.due_notices()], ["ended", "owner_ended"])

    def test_editing_a_guests_request_is_not_a_decline(self):
        sid = self.give(900)
        for _ in range(4):
            self.now += 400
            asked = self.s.renewal("200", sid, 900)
            self.s.deny("100", asked["proposal_id"], declined=False)  # Change, not Cancel
        self.now += 400
        self.s.renewal("200", sid, 900)

    def test_time_added_becomes_the_length_offered_next(self):
        sid = self.give(900)
        review = self.s.renewal("200", sid, 3600)  # still on: added to the end
        self.s.approve("100", review["proposal_id"])
        self.assertEqual(self.s.get_access("100", sid)["duration_seconds"], 3600)

    def test_short_fixed_access_gets_no_warning(self):
        p = self.s.prepare(
            "100", "Alex", {"lights": ["light.porch"], "expires_at": iso(self.now + 240)}
        )
        invitation = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invitation["token"], "200", "Alex")
        self.s.accept_claim("100", claim["claim_id"])
        self.now += 150
        self.assertEqual(self.s.due_notices(), [])

    def test_the_invitation_window_says_a_fixed_end_comes_first(self):
        fixed = {"expires_at": iso(self.now + 86400)}
        self.assertIn("until access ends", self.s.invitation_window_text(3 * 86400, fixed))
        self.assertNotIn(
            "until access ends", self.s.invitation_window_text(3600, {"duration_seconds": 900})
        )

    def test_deleting_a_guests_data_forgets_their_time_zone(self):
        sid = self.give(3600)
        self.s.set_guest_zone("200", sid, "Europe/London")
        p = self.s.prepare("100", "Alex", None, share_id=sid, action="delete")
        self.s.approve("100", p["proposal_id"])
        self.assertNotIn("guest_zone", self.s.ledger.snapshot()["shares"][sid])

    def test_a_renewed_grant_cancels_its_old_end_notices(self):
        sid = self.give(900)
        self.now += 1000
        due = self.s.due_notices()
        self.s.approve("100", self.s.renewal("200", sid, 900)["proposal_id"])
        self.assertFalse(any(self.s.notice_still_due(k, r) for k, r in due))

    def test_access_records_when_the_owner_last_approved_it(self):
        sid = self.give(900)
        self.assertEqual(self.s.get_access("100", sid)["changed_at"], self.now)

    def test_a_warning_that_arrives_too_late_is_dropped(self):
        self.give(900)
        self.now += 800
        warning = [n for n in self.s.due_notices() if n[0] == "ending"]
        self.now += 200  # delivery was held up past the end
        self.assertFalse(self.s.notice_still_due(*warning[0]))

    def test_a_guests_renewal_never_switches_on_private_memory(self):
        sid = self.give(900)
        with self.s.ledger.transaction() as db:
            db["shares"][sid]["scope"].pop("guest_memory", None)  # saved without it
        for wait in (0, 1000):  # while access is on, and after it ran out
            self.now += wait
            review = self.s.renewal("200", sid, 900)
            saved = self.s.ledger.snapshot()["proposals"][review["proposal_id"]]
            self.assertFalse(saved["scope"].get("guest_memory"))
            self.s.deny("100", review["proposal_id"], declined=False)
            self.now += 400

    def test_typed_lengths(self):
        for text, seconds in (
            ("6 hours", 21600),
            ("an hour please", 3600),
            ("1h30m", 5400),
            ("2 days", 172800),
            ("for a week", 604800),
            ("1.5 hours", 5400),
            ("1 hour and 30 minutes", 5400),
            ("another 2 days please", 172800),
        ):
            self.assertEqual(parse_length(text), seconds, text)
        for text in (
            "sure",
            "30 sec",
            "3 months",
            "",
            "-6 hours",
            ".5 hours",
            "+2 days",
            "1 day or 2 days",
            "not 6 hours, 2 days",
            "6 hours tomorrow",
        ):
            self.assertIsNone(parse_length(text), text)
        self.assertEqual(length_text(5400), "1 hour 30 minutes")


class ActivityRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog=dict(CATALOG),
            dispatch=lambda tool, args, envelope: {},
            tool_resolver=GUEST_SCHEMAS.get,
            activity_retention_days=30,
        )
        self.audit = self.s.audit

    def closed(self, at, kind):
        path = self.audit.root / f"activity.{int(at)}.jsonl"
        path.write_text(json.dumps({"at": at, "kind": kind, "share_id": "", "actor": "100"}) + "\n")
        os.utime(path, (at, at))
        return path

    def test_closed_files_are_read_and_kept_until_they_age_out(self):
        now = time.time()
        old = self.closed(now - 40 * 86400, "old")
        recent = self.closed(now - 10 * 86400, "recent")
        self.audit.event("current")
        kinds = [r["kind"] for r in self.audit.list(None, 1000)]
        self.assertEqual(kinds[:2], ["recent", "current"])
        self.audit.prune(now)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        since = [r["kind"] for r in self.audit.list(None, 1000, since=now - 86400)]
        self.assertEqual(since, ["current"])

    def test_a_quiet_log_still_ages_out(self):
        now = time.time()
        old = {"at": now - 40 * 86400, "kind": "old", "share_id": "", "actor": "100"}
        self.audit.events.write_text(json.dumps(old) + "\n")
        self.assertEqual(self.audit.list(None, 1000), [])  # never shown past retention
        self.audit.upkeep(now)  # closes the day-old current file
        self.audit.upkeep(now + 31 * 86400)
        self.assertEqual(list(self.audit.root.glob("activity.*.jsonl")), [])

    def test_closing_twice_in_one_second_keeps_both(self):
        self.audit.event("first")
        self.audit._close_current(1_000_000)
        self.audit.event("second")
        self.audit._close_current(1_000_000)
        names = sorted(p.name for p in self.audit.root.glob("activity.*.jsonl"))
        self.assertEqual(names, ["activity.1000000.jsonl", "activity.1000001.jsonl"])

    def test_a_short_query_returns_the_newest(self):
        for i in range(30):
            self.audit.event("e", n=i)
        rows = self.audit.list(None, 5)
        self.assertEqual([r["n"] for r in rows], [25, 26, 27, 28, 29])

    def test_a_restored_old_file_ages_out_by_its_events(self):
        now = time.time()
        path = self.closed(now - 40 * 86400, "old")
        os.utime(path, (now, now))  # copied back today
        self.audit.prune(now)
        self.assertFalse(path.exists())

    def test_a_recent_archive_behind_an_old_restored_one_is_still_read(self):
        now = time.time()
        self.closed(now - 3600, "recent")
        old = self.audit.root / f"activity.{int(now) + 5}.jsonl"  # named after it
        old.write_text(json.dumps({"at": now - 20 * 86400, "kind": "older"}) + "\n")
        kinds = [r["kind"] for r in self.audit.list(None, 100, since=now - 86400)]
        self.assertIn("recent", kinds)
        newest = self.audit.list(None, 1)
        self.assertEqual(newest[0]["kind"], "recent")  # by when it happened, not file name

    def test_equal_times_keep_the_order_they_were_written(self):
        at = time.time() - 60
        path = self.audit.root / "activity.1000.jsonl"
        path.write_text(json.dumps({"at": at, "kind": "first"}) + "\n")
        self.audit.events.write_text(json.dumps({"at": at, "kind": "second"}) + "\n")
        self.assertEqual([r["kind"] for r in self.audit.list(None, 1)], ["second"])
        self.assertEqual([r["kind"] for r in self.audit.list(None, 10)], ["first", "second"])

    def test_retention_must_be_sensible(self):
        with self.assertRaises(GuestsError):
            GuestsService(
                FileState(self.temp.name + "/x"),
                "100",
                catalog={},
                dispatch=lambda *a: {},
                tool_resolver=GUEST_SCHEMAS.get,
                activity_retention_days=0,
            )


if __name__ == "__main__":
    unittest.main()
