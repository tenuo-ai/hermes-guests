"""Hermes Guests' own entry points on a Hermes host that serves several profiles."""

import contextlib
import contextvars
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

from hermes_guests.compat import in_own_profile, own_profile

NAME = "GUESTS_TEST_SETTING_THAT_IS_NOT_SET"


@unittest.skipUnless(
    importlib.util.find_spec("run_agent") and importlib.util.find_spec("tui_gateway"),
    "Native Hermes source required",
)
class OwnProfileTests(unittest.TestCase):
    def several_profiles(self, body):
        """Run body as Hermes runs work on a host that serves several profiles."""

        def run():
            from agent.secret_scope import set_multiplex_context

            set_multiplex_context(True)
            return body()

        return contextvars.copy_context().run(run)

    def test_single_profile_host_is_left_as_it_was(self):
        self.assertIsInstance(own_profile(), contextlib.nullcontext)

    def test_read_refused_outside_is_answered_on_the_profiles_behalf(self):
        from agent.secret_scope import UnscopedSecretError, current_secret_scope, get_secret

        def body():
            with self.assertRaises(UnscopedSecretError):
                get_secret(NAME)
            self.assertIsNone(in_own_profile(get_secret, NAME))
            with own_profile():
                self.assertIsNotNone(current_secret_scope())
            self.assertIsNone(current_secret_scope())  # nothing is left bound afterwards

        self.several_profiles(body)

    def test_a_profile_already_chosen_is_kept(self):
        from agent.secret_scope import get_secret, set_secret_scope

        def body():
            set_secret_scope({NAME: "theirs"}, profile_home="/another/profile")
            self.assertIsInstance(own_profile(), contextlib.nullcontext)
            with own_profile():
                self.assertEqual(get_secret(NAME), "theirs")

        self.several_profiles(body)

    def test_a_key_changed_after_the_runner_was_made_is_used_on_the_next_turn(self):
        from unittest import mock

        from agent.secret_scope import get_secret
        from tui_gateway.launch_profile_policy import owner_secret_scope

        from hermes_guests.guest import NativeGuestRunner

        def route():
            return {"model": "local", "api_key": get_secret(NAME)}

        for own in (True, False):  # this process's own profile, and another one it serves
            with self.subTest(own=own), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                (home / ".env").write_text(NAME + "=first\n")

                def body():
                    from agent.secret_scope import set_secret_scope

                    if own:
                        with own_profile():
                            runner = NativeGuestRunner(mock.Mock(), None, route)
                    else:
                        set_secret_scope(owner_secret_scope(home), profile_home=str(home))
                        runner = NativeGuestRunner(mock.Mock(), None, route)
                    self.assertEqual(runner.model_route()["api_key"], "first")
                    (home / ".env").write_text(NAME + "=second\n")
                    self.assertEqual(runner.model_route()["api_key"], "second")
                    if not own:  # a profile Hermes chose for a live turn keeps what Hermes read
                        self.assertEqual(in_own_profile(get_secret, NAME), "second")
                        with own_profile():
                            self.assertEqual(get_secret(NAME), "first")

                with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)} if own else {}):
                    self.several_profiles(body)

    def test_the_runner_keeps_its_own_profile_when_the_host_names_another_home_later(self):
        from unittest import mock

        from agent.secret_scope import get_secret

        from hermes_guests.guest import NativeGuestRunner

        def route():
            return {"model": "local", "api_key": get_secret(NAME)}

        with tempfile.TemporaryDirectory() as mine, tempfile.TemporaryDirectory() as other:
            (Path(mine) / ".env").write_text(NAME + "=mine\n")
            (Path(other) / ".env").write_text(NAME + "=theirs\n")

            def body():
                with mock.patch.dict(os.environ, {"HERMES_HOME": mine}):
                    with own_profile():
                        runner = NativeGuestRunner(mock.Mock(), None, route)
                    self.assertEqual(runner.model_route()["api_key"], "mine")
                # A host that mirrors the profile it is serving into HERMES_HOME.
                with mock.patch.dict(os.environ, {"HERMES_HOME": other}):
                    self.assertEqual(runner.model_route()["api_key"], "mine")
                    (Path(mine) / ".env").write_text(NAME + "=mine, changed\n")
                    self.assertEqual(runner.model_route()["api_key"], "mine, changed")

            self.several_profiles(body)

    def test_a_home_already_named_for_another_profile_is_the_one_read(self):
        from unittest import mock

        from agent.secret_scope import get_secret
        from hermes_constants import set_hermes_home_override

        with tempfile.TemporaryDirectory() as mine, tempfile.TemporaryDirectory() as other:
            (Path(mine) / ".env").write_text(NAME + "=mine\n")
            (Path(other) / ".env").write_text(NAME + "=theirs\n")

            def body():
                set_hermes_home_override(other)  # work begun for another profile, nothing read yet
                with own_profile():
                    self.assertEqual(get_secret(NAME), "theirs")
                self.assertEqual(in_own_profile(get_secret, NAME), "theirs")

            with mock.patch.dict(os.environ, {"HERMES_HOME": mine}):
                self.several_profiles(body)

    def test_the_model_chosen_in_hermes_is_found_for_a_guest_turn(self):
        from unittest import mock

        from hermes_guests.guest import NativeGuestRunner
        from hermes_guests.plugin import native_runtime

        model = {"provider": "openrouter", "default": "test-model", "key_env": NAME}
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text(NAME + "=the profile's key\n")

            def body():
                # Made while the gateway starts, before any work is done for a profile.
                runner = NativeGuestRunner(mock.Mock(), None, native_runtime)
                return runner.model_route()

            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": directory}),
                mock.patch("hermes_cli.config.load_config_readonly", return_value={"model": model}),
            ):
                self.assertEqual(self.several_profiles(body)["api_key"], "the profile's key")

    def test_a_guests_model_request_is_sent_on_the_profiles_behalf(self):
        import json
        from types import SimpleNamespace
        from unittest import mock
        from urllib.request import Request, urlopen

        from agent.secret_scope import get_secret

        from hermes_guests.transport import LocalBroker

        def hermes_sends(payload, *, timeout):
            # Hermes attaches the owner's model sign-in, which it reads for a profile.
            return SimpleNamespace(
                status=200,
                headers={"content-type": "text/event-stream"},
                body=("data: " + get_secret(NAME) + "\n\n").encode(),
            )

        service = mock.Mock()
        service.admit_turn.return_value = {"revision": 3}
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / ".env").write_text(NAME + "=signed-in\n")
            with (
                mock.patch.dict(os.environ, {"HERMES_HOME": directory}),
                mock.patch("hermes_guests.transport.request_codex", hermes_sends),
            ):
                broker = self.several_profiles(lambda: LocalBroker(service))
                self.addCleanup(broker.close)
                token = broker.open("200", "sh_1", codex_model="codex-fixture", revision=3)
                request = Request(
                    broker.url + "/responses",
                    json.dumps({"model": "codex-fixture", "input": []}).encode(),
                    {"Authorization": "Bearer " + token},
                )
                with urlopen(request, timeout=5) as reply:
                    self.assertEqual(reply.read(), b"data: signed-in\n\n")


if __name__ == "__main__":
    unittest.main()
