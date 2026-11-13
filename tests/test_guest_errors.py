"""Native provider failures must not expose credentials or owner-only recovery commands."""

import unittest

from hermes_guests.worker import guest_reply


class GuestErrorTests(unittest.TestCase):
    def test_provider_failure_uses_guest_recovery_copy_and_acknowledges_possible_effects(self):
        internal = "Provider said: rejected fixture-secret. Use /model, /retry or edit owner configuration."
        text = guest_reply({"final_response": internal, "error": internal, "partial": True})
        self.assertNotIn("fixture-secret", text)
        self.assertNotIn("/model", text)
        self.assertNotIn("Provider said", text)
        self.assertIn("may have completed", text)
        self.assertIn("owner", text)
        self.assertEqual(
            guest_reply({"final_response": "Found two comedies."}), "Found two comedies."
        )


if __name__ == "__main__":
    unittest.main()
