"""Native registration contract; no real Telegram messages are sent."""

import unittest
from pathlib import Path

import hermes_guests


class SkillRegistrationTests(unittest.TestCase):
    def test_registered_skill_and_references_exist(self):
        calls = []
        tools, hooks, factories = [], [], []

        class Context:
            def spawn_task(self, *args, **kwargs):
                raise AssertionError(
                    "Registration must not start work before a Telegram connection."
                )

            def register_command(self, *args, **kwargs):
                pass

            def register_tool(self, name, *args, **kwargs):
                tools.append(name)

            def register_hook(self, name, handler):
                hooks.append(name)

            def register_telegram_handler(self, factory):
                factories.append(factory)

            def register_skill(self, name, path, *, description):
                calls.append((name, Path(path), description))

        hermes_guests.register(Context())
        self.assertEqual(len(calls), 1)
        name, path, description = calls[0]
        self.assertEqual(name, "guest-access")
        self.assertTrue(path.is_file())
        self.assertIn("expand", description)
        text = path.read_text()
        self.assertIn("guests_prepare_access", text)
        self.assertIn("guests_delegate_task", text)
        self.assertEqual(
            set(tools),
            {
                "guests_discover",
                "guests_prepare_access",
                "guests_list_access",
                "guests_activity",
                "guests_repair",
                "guests_status",
                "guests_data",
                "guests_never",
            },
        )
        self.assertEqual(hooks, ["pre_tool_call", "pre_llm_call"])
        self.assertEqual(len(factories), 1)
        for reference in ("integrations.md", "released-runtime.md"):
            self.assertTrue((path.parent / "references" / reference).is_file())

    def test_missing_native_skill_api_reports_unavailable(self):
        with self.assertRaises(RuntimeError):
            hermes_guests.register(object())


if __name__ == "__main__":
    unittest.main()
