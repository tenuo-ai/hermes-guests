"""First use derives one already trusted native identity; it never appoints a visitor."""

import importlib.util
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_guests.model import GuestsError
from hermes_guests.plugin import other_people, owner_identity
from tests.tiers import slow


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "Native Hermes source required")
class IdentitySetupTests(unittest.TestCase):
    def check(self, allowed="", paired=(), configured="", allow_all=""):
        ctx = SimpleNamespace(get_config=lambda name, default: configured)
        values = {"TELEGRAM_ALLOWED_USERS": allowed, "TELEGRAM_ALLOW_ALL_USERS": allow_all}
        with (
            patch("hermes_cli.config.get_env_value", side_effect=lambda key: values.get(key)),
            patch("gateway.pairing.PairingStore") as store,
        ):
            store.return_value.list_approved.return_value = [{"user_id": x} for x in paired]
            return owner_identity(ctx)

    def test_existing_numeric_owner_requires_no_additional_configuration(self):
        self.assertEqual(self.check(allowed="100"), "100")

    @slow
    def test_sole_native_paired_owner_is_automatically_reused(self):
        self.assertEqual(self.check(paired=["100"]), "100")

    def test_everyone_admitted_or_no_clear_owner_cannot_enable_sharing(self):
        for kwargs in (
            {"paired": ["100", "200"]},  # two paired people and nothing says which is the owner
            {"allowed": "100", "allow_all": "true"},  # no list of people to scope
        ):
            with self.subTest(kwargs), self.assertRaises(GuestsError):
                self.check(**kwargs)

    def test_other_admitted_people_no_longer_block_setup_and_are_listed(self):
        self.assertEqual(self.check(allowed="100,200", configured="100"), "100")
        self.assertEqual(self.check(allowed="100", paired=["200"], configured="100"), "100")
        self.assertEqual(self.check(allowed="200,100", configured="100"), "100")
        values = {"TELEGRAM_ALLOWED_USERS": "100, 200,abc", "TELEGRAM_ALLOW_ALL_USERS": ""}
        with (
            patch("hermes_cli.config.get_env_value", side_effect=lambda key: values.get(key)),
            patch("gateway.pairing.PairingStore") as store,
        ):
            store.return_value.list_approved.return_value = [
                {"user_id": "300", "user_name": "Blair"},
                {"user_id": "100"},
            ]
            self.assertEqual(
                other_people(None, "100"),
                [{"id": "200", "label": "200"}, {"id": "300", "label": "Blair"}],
            )

    def test_multiple_admitted_users_require_explicit_owner_selection(self):
        for kwargs in (
            {"allowed": "100,200"},
            {"allowed": "200,100"},
            {"allowed": "100", "paired": ["200"]},
            {"paired": ["200", "100"]},
        ):
            with (
                self.subTest(kwargs),
                self.assertRaisesRegex(GuestsError, "Choose the sharing owner"),
            ):
                self.check(**kwargs)
        self.assertEqual(self.check(allowed="100,100", paired=["100"]), "100")
        with self.assertRaises(GuestsError):
            self.check(allowed="100,200", configured="300")

    def test_no_first_visitor_or_share_only_setting_bypasses_native_owner_pairing(self):
        with self.assertRaises(GuestsError):
            self.check()
        with self.assertRaises(GuestsError):
            self.check(configured="100")


if __name__ == "__main__":
    unittest.main()
