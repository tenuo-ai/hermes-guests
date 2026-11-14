"""What the guest's model is told about time and about access that changed."""

import unittest

from hermes_guests.model import iso
from hermes_guests.worker import access_times

NOW = 1_794_630_600  # Sat 14 Nov 2026, 04:30 UTC


class AccessTimesTests(unittest.TestCase):
    def payload(self, **overrides):
        return {
            "zone": "America/Toronto",
            "now": NOW,
            "changed_at": NOW - 60,
            "scope": {"expires_at": iso(NOW + 900)},
            **overrides,
        }

    def test_times_are_in_the_readers_zone(self):
        text = access_times(self.payload())
        self.assertIn("It is now Fri 13 Nov, 11:30 PM EST", text)
        self.assertIn("ends Fri 13 Nov, 11:45 PM EST", text)
        self.assertNotIn("UTC", text)

    def test_renewal_says_earlier_refusals_no_longer_apply(self):
        # A renewal keeps the same conversation. Its history still holds the refusals
        # from when access ran out, and the model must not keep refusing because of them.
        text = access_times(self.payload())
        self.assertIn("last granted or changed this access at Fri 13 Nov, 11:29 PM EST", text)
        self.assertIn("from before that time", text)
        self.assertIn("Refusals after it still apply", text)

    def test_access_with_no_end_date(self):
        text = access_times(self.payload(scope={"until_ended": True, "expires_at": iso(NOW + 9e5)}))
        self.assertIn("no end date", text)
        self.assertNotIn("ends ", text)

    def test_the_guest_is_offered_their_own_time_zone_until_they_have_one(self):
        tools = {"tool_schemas": {"ha_get_state": {}}, "delegation": None}
        self.assertIn("guest_set_time_zone", access_times(self.payload(**tools)))
        theirs = access_times(self.payload(zone="Europe/London", zone_is_theirs=True, **tools))
        self.assertNotIn("guest_set_time_zone", theirs)
        self.assertIn("GMT", theirs)
        self.assertNotIn("guest_set_time_zone", access_times(self.payload(tool_schemas={})))


class GuestReplyTests(unittest.TestCase):
    def test_bold_is_unmarked_but_code_is_left_alone(self):
        from hermes_guests.worker import guest_reply

        text = "The **porch light** is on. Use `__init__` and ```def __init__(self): **x**```."
        self.assertEqual(
            guest_reply({"final_response": text}),
            "The porch light is on. Use `__init__` and ```def __init__(self): **x**```.",
        )
        self.assertEqual(guest_reply({"final_response": "__name__"}), "__name__")


if __name__ == "__main__":
    unittest.main()
