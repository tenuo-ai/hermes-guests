"""Readable audit times and scoped retrieval preserve existing stored evidence."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tenuo import SigningKey

from hermes_guests.audit import Audit
from hermes_guests.plugin import activity_text


class ActivityTests(unittest.TestCase):
    def test_readable_times_include_date_seconds_and_requested_zone(self):
        rows = [
            {
                "kind": "decision",
                "decision": "ALLOW",
                "tool": "calendar_read",
                "at": datetime(2026, 1, 1, 1, 2, 3, tzinfo=timezone.utc).timestamp(),
            }
        ]
        est = activity_text("Alex", rows, "EST")
        self.assertIn("Wed 31 Dec 2025", est)
        self.assertIn("8:02:03 PM", est)
        self.assertIn("Times are EST.", est)
        rows[0]["at"] = datetime(2026, 7, 1, 16, tzinfo=timezone.utc).timestamp()
        toronto = activity_text("Alex", rows, "America/Toronto")
        self.assertIn("Wed 1 Jul 2026", toronto)
        self.assertIn("12:00:00 PM", toronto)
        self.assertIn("Times are Toronto time.", toronto)
        self.assertIn("11:00:00 AM", activity_text("Alex", rows, "EST"))

    def test_owner_lifecycle_actions_are_distinct_from_guest_service_completion(self):
        rows = [
            {
                "at": 0,
                "kind": "owner_approved",
                "action": "revoke",
                "actor_label": "Owner",
                "person": "Alex",
            },
            {"at": 1, "kind": "access_activated", "actor_label": "Owner", "subject_label": "Blair"},
        ]
        text = activity_text("everyone", rows, "UTC", lifecycle=True)
        self.assertIn("Owner · Approved ending access — Alex", text)
        self.assertIn("Owner · Access activated — Blair", text)

    def test_labels_remain_one_line_in_the_recorded_activity_view(self):
        rows = [
            {
                "at": 0,
                "kind": "owner_approved",
                "action": "revoke",
                "actor_label": "Alex\nOwner",
                "person": "Blair\x00",
            }
        ]
        text = activity_text("everyone", rows, "UTC", lifecycle=True)
        self.assertIn("Alex Owner", text)
        self.assertNotIn("\x00", text)
        # Heading, day, the one entry and the time zone: the label added no lines.
        self.assertEqual(len([line for line in text.splitlines() if "Owner" in line]), 1)

    def test_retained_reader_filters_owner_actor_and_keeps_existing_share_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            issuer = SigningKey.generate()
            audit = Audit(Path(directory), SigningKey.generate(), issuer.public_key)
            audit.event("decision", share_id="owner", actor="100", tool="calendar_read")
            audit.event("owner_approved", share_id="sh_example", actor="100")
            audit.event("decision", share_id="sh_example", actor="200", tool="calendar_read")
            own = audit.list(None, actor="100")
            self.assertEqual(len(own), 2)
            self.assertEqual(len(audit.list("sh_example")), 2)
            self.assertEqual(len(audit.list(None)), 3)
            self.assertEqual(len(audit.list(None, 1)), 1)
            self.assertEqual(audit.list(None, actor="300"), [])


if __name__ == "__main__":
    unittest.main()
