"""Actual python-telegram-bot Application routing, with a recording bot and services."""

import asyncio
import importlib.util
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import Application, ExtBot, MessageHandler, filters

from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError, iso, timestamp
from hermes_guests.plugin import MANAGEMENT_TOOLS, GuestsPlugin
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState


class RecordingBot(ExtBot):
    def __init__(self):
        super().__init__("123:local-test")
        object.__setattr__(self, "sent", [])
        object.__setattr__(self, "fail_once", False)
        self._bot_user = User(999, "Share", True, username="guests_fixture_bot")

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def get_me(self, **kw):
        return self._bot_user

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.fail_once:
            object.__setattr__(self, "fail_once", False)
            raise RuntimeError("simulated Telegram delivery failure")
        self.sent.append((int(chat_id), text, reply_markup))
        m = Message(
            len(self.sent),
            datetime.now(timezone.utc),
            Chat(int(chat_id), "private"),
            text=text,
            reply_markup=reply_markup,
        )
        m.set_bot(self)
        return m

    async def set_my_commands(self, commands, **kwargs):
        object.__setattr__(self, "recorded_commands", [command.command for command in commands])
        return True

    async def send_chat_action(self, *args, **kwargs):
        return True

    async def answer_callback_query(self, *args, **kw):
        return True

    async def edit_message_reply_markup(self, *args, **kw):
        return True


class TelegramJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bot = RecordingBot()
        self.app = Application.builder().bot(self.bot).build()
        await self.app.initialize()
        self.addAsyncCleanup(self.app.shutdown)
        self._update_id = 0
        self.effects = []
        self.runs = []
        self.tasks = []
        self.fallback = []
        self.service = GuestsService(
            FileState(Path(self.temp.name) / "control"),
            "100",
            catalog={"light.porch": {"name": "Porch light"}},
            dispatch=lambda t, a, e: self.effects.append((t, a, e)) or {"state": "on"},
            tool_resolver=GUEST_SCHEMAS.get,
        )
        outer = self

        class Runner:
            def ensure_profile(self, sid):
                return Path(outer.temp.name)

            def run(self, actor, sid, text):
                outer.runs.append((actor, sid, text))
                outer.service.call(actor, sid, "ha_get_state", {"entity_id": "light.porch"})
                return "The porch light is on."

            def reset_context(self, *args):
                pass

        class Context:
            state = outer.service.ledger.state

            def get_config(self, key, default=None):
                return default

            def spawn_task(self, coro, **kw):
                task = asyncio.create_task(coro)
                outer.tasks.append(task)
                return task

        self.plugin = GuestsPlugin(Context(), service=self.service, runner=Runner())
        from hermes_guests.plugin import MANAGEMENT_TOOLS

        self.plugin.owner_tools = lambda **_: {name: name for name in MANAGEMENT_TOOLS}
        self.plugin.telegram_factory(self.app, None)
        self.addAsyncCleanup(self.close_plugin)

        async def fallback(update, context):
            self.fallback.append(update.effective_user.id)

        self.app.add_handler(MessageHandler(filters.ALL, fallback), group=0)
        self.errors = []

        async def error(update, context):
            self.errors.append(type(context.error).__name__)

        self.app.add_error_handler(error)
        self.owner_patch = patch("hermes_guests.plugin.owner_identity", return_value="100")
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        # Who else the bot admits comes from Hermes; these journeys run without it.
        others_patch = patch("hermes_guests.plugin.other_people", return_value=[])
        others_patch.start()
        self.addCleanup(others_patch.stop)

    async def close_plugin(self):
        self.plugin.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Cancelling the upkeep task does not stop a pass already running in its worker
        # thread; wait for it so it cannot write into the folder while that is removed.
        await asyncio.get_running_loop().shutdown_default_executor()

    async def message(self, who, text, *, wait=True):
        m = Message(
            1000 + len(self.bot.sent),
            datetime.now(timezone.utc),
            Chat(who, "private"),
            from_user=User(
                who, "Alex" if who == 200 else "Owner" if who == 100 else "Blair", False
            ),
            text=text,
        )
        m.set_bot(self.bot)
        self._update_id += 1
        await self.app.process_update(Update(self._update_id, message=m))
        if wait:
            await asyncio.gather(*list(self.plugin._guest_tasks.values()))

    async def click(self, who, data, *, message_id=None):
        matching = [
            i + 1
            for i, (_, _, markup) in enumerate(self.bot.sent)
            if markup
            and any(
                button.callback_data == data for row in markup.inline_keyboard for button in row
            )
        ]
        chosen = message_id if message_id is not None else matching[-1] if matching else -1
        m = Message(
            chosen,
            datetime.now(timezone.utc),
            Chat(who, "private"),
            text="Review",
        )
        m.set_bot(self.bot)
        q = CallbackQuery("callback", User(who, "Person", False), "instance", message=m, data=data)
        q.set_bot(self.bot)
        self._update_id += 1
        await self.app.process_update(Update(self._update_id, callback_query=q))

    def scope(self):
        return {"lights": ["light.porch"], "research": False, "expires_at": iso(time.time() + 3600)}

    async def invite(self):
        p = self.service.prepare("100", "Alex", self.scope())
        await self.plugin.proposal_card(p)
        if not any(
            markup
            and any(
                b.callback_data == "hs:p:" + p["proposal_id"]
                for row in markup.inline_keyboard
                for b in row
            )
            for _, _, markup in self.bot.sent
        ):
            await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"])
        links = [text for who, text, _ in self.bot.sent if who == 100 and "?start=guests_" in text]
        self.assertTrue(links, self.bot.sent)
        return p, links[-1].split("?start=guests_", 1)[1]

    async def activate(self):
        p, token = await self.invite()
        await self.message(200, "/start guests_" + token)
        from hermes_guests.core import token_hash

        await self.click(100, "hs:a:" + token_hash(token)[:24])
        return self.service.guest("200")["id"]

    @unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "Native Hermes required")
    async def test_real_native_telegram_admission_wires_share_before_owner_only_core(self):
        from types import SimpleNamespace

        from gateway.config import PlatformConfig
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from plugins.platforms.telegram.adapter import TelegramAdapter
        from plugins.platforms.telegram.update_admission import TelegramApplication

        token = set_hermes_home_override(str(Path(self.temp.name) / "native"))
        self.addCleanup(reset_hermes_home_override, token)
        adapter = TelegramAdapter(
            PlatformConfig(enabled=True, token="123:local-test", extra={"allow_from": ["100"]})
        )
        adapter._handle_text_message = AsyncMock()
        adapter._handle_command = AsyncMock()
        app = (
            Application.builder()
            .bot(self.bot)
            .application_class(TelegramApplication, {"adapter": adapter})
            .build()
        )
        await app.initialize()
        self.addAsyncCleanup(app.shutdown)
        for callback, block in self.app.error_handlers.items():
            app.add_error_handler(callback, block=block)
        manager = SimpleNamespace(
            get_platform_handler_factories=lambda platform: [
                (self.plugin.telegram_factory, "hermes-guests")
            ]
        )
        with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
            adapter._wire_plugin_handlers(app)
        adapter._register_handlers(app)
        self.app = app
        await self.message(100, "/guests_setup")
        setup = self.bot.sent[-1][2].inline_keyboard[0][0].callback_data
        await self.click(100, setup)
        self.assertTrue(self.service.owner_ready())
        await self.activate()
        await self.message(200, "Turn on the porch light")
        self.assertEqual(len(self.effects), 1)
        await self.message(300, "Turn on the porch light")
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(adapter.config.extra["allow_from"], ["100"])
        adapter._handle_text_message.assert_not_awaited()
        adapter._handle_command.assert_not_awaited()
        if adapter._update_receipt_flush:
            await adapter._update_receipt_flush
        self.assertFalse(self.errors)

    async def test_chat_only_invitation_claim_and_conversation(self):
        from hermes_guests.core import token_hash

        p = self.service.prepare("100", "Alex", {"chat_only": True, "duration_seconds": 3600})
        await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"])
        link = next(
            text
            for who, text, _ in reversed(self.bot.sent)
            if who == 100 and "?start=guests_" in text
        )
        token = link.split("?start=guests_", 1)[1]
        await self.message(200, "/start guests_" + token)
        await self.click(100, "hs:a:" + token_hash(token)[:24])
        self.assertTrue(any("It expires" in text for _, text, _ in self.bot.sent))
        self.assertTrue(any("Confirm by" in text for _, text, _ in self.bot.sent))
        self.assertEqual(self.bot.recorded_commands, ["help", "access", "renew", "new", "timezone"])
        self.plugin.runner.run = lambda *args: "Hello! What would you like to talk about?"
        await self.message(200, "Hello")
        self.assertIn("talk about", self.bot.sent[-1][1])
        self.assertEqual(self.effects, [])
        self.assertEqual(self.fallback, [])
        self.assertEqual(self.errors, [])

    async def test_status_distinguishes_expired_claim_confirmation(self):
        import json

        self.service.claim_seconds = 600  # an owner who chose a ten-minute confirmation window
        _, token = await self.invite()
        self.service.claim(token, "200", "Alex")
        now = time.time()
        self.service.clock = lambda: now + 601
        with patch("hermes_guests.plugin.actor", return_value="100"):
            status = json.loads(await self.plugin.tool("guests_status", {}))
        invitation = status["result"]["invitations"][0]
        self.assertEqual(invitation["status"], "expired")
        self.assertNotEqual(invitation["confirmation_deadline"], invitation["invitation_deadline"])
        self.assertEqual(self.effects, [])

    async def test_permission_review_keeps_primary_control_on_its_own_row(self):
        p = self.service.prepare("100", "Alex", self.scope())
        await self.plugin.proposal_card(p)
        card = self.bot.sent[-1]
        self.assertEqual(
            [[b.text for b in row] for row in card[2].inline_keyboard],
            [["Approve"], ["Change", "Cancel"], ["Preview as Alex", "Details"]],
        )
        # The simple card: what is decided, in plain words, one end time. Details on request.
        self.assertTrue(card[1].startswith("Share with Alex?"))
        self.assertIn("Ends ", card[1])
        self.assertIn("Approve by ", card[1])
        self.assertNotIn("Access details", card[1])
        await self.click(100, self.press("Details"))
        self.assertIn("Access details", self.bot.sent[-1][1])

    async def test_first_owner_message_offers_setup_and_keeps_native_chat_working(self):
        await self.message(100, "Hello")
        self.assertEqual(self.fallback, [100])
        button = self.bot.sent[-1][2].inline_keyboard[0][0]
        self.assertEqual(button.text, "Set up guest sharing")
        await self.message(100, "What lights are on?")
        self.assertEqual(len(self.bot.sent), 1)
        await self.click(100, button.callback_data, message_id=1)
        self.assertIn("Set up guest sharing?", self.bot.sent[-1][1])
        self.assertFalse(self.service.owner_ready())
        self.assertEqual(self.effects, [])

    async def test_first_share_request_opens_setup_even_without_management_tools(self):
        self.plugin.owner_tools = lambda **_: {"ha_get_state": "Read state"}
        await self.message(100, "How's the weather?")
        await self.message(100, "Let Adrian control the guest room lights for a day.")
        self.assertEqual(self.fallback, [100])
        self.assertIn("Set up guest sharing?", self.bot.sent[-1][1])
        self.assertFalse(self.service.owner_ready())
        self.assertEqual(self.service.ledger.snapshot()["proposals"], {})

    async def test_owner_tools_pass_through_even_if_sharing_state_cannot_load(self):
        await self.ready_owner()
        with (
            patch.object(self.plugin, "ensure", side_effect=GuestsError("Cannot load sharing")),
            patch("gateway.session_context.get_session_env") as env,
        ):
            env.side_effect = lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "telegram",
                "HERMES_SESSION_USER_ID": "100",
            }.get(name, default)
            self.assertIsNone(self.plugin.owner_boundary("new_tool"))
            with patch.object(self.plugin.ctx, "get_config", return_value=True):
                self.assertEqual(self.plugin.owner_boundary("new_tool")["action"], "block")
            await self.message(100, "Please check the lights")
            self.assertEqual(self.fallback, [100])
            env.side_effect = lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "telegram",
                "HERMES_SESSION_USER_ID": "200",
            }.get(name, default)
            self.assertEqual(self.plugin.owner_boundary("new_tool")["action"], "block")

    async def test_setup_does_not_grant_an_unreviewed_tool_after_enabling_management(self):
        from hermes_guests.plugin import MANAGEMENT_TOOLS

        tools = {"ha_get_state": "Read a light"}
        self.plugin.owner_tools = lambda **_: dict(tools)
        await self.message(100, "/guests_setup")
        enable = self.press("Enable sharing")
        tools.update({name: name for name in MANAGEMENT_TOOLS})
        tools["ha_call_service"] = "Control a device"
        with patch("hermes_guests.compat.enable_telegram_management_tools"):
            await self.click(100, enable)
        self.assertIn("Your tools changed", self.bot.sent[-1][1])
        self.assertFalse(self.service.owner_ready())

    async def test_fresh_install_preserves_native_tool_admission(self):
        with patch("gateway.session_context.get_session_env", return_value="telegram"):
            with patch(
                "hermes_guests.plugin.actor", side_effect=AssertionError("native owns access")
            ):
                self.assertIsNone(self.plugin.owner_boundary("ha_get_state"))

    async def test_conversational_guest_tool_opens_setup_before_creating_proposal(self):
        with (
            patch("hermes_guests.plugin.actor", return_value="100"),
            patch("hermes_guests.plugin.SETUP_WAIT_SECONDS", 0.05),
        ):
            result = json.loads(await self.plugin.tool("guests_prepare_access", {"person": "Alex"}))
        self.assertFalse(result["ok"])
        self.assertIn("go ahead", result["error"])
        self.assertEqual(self.bot.sent[-1][2].inline_keyboard[0][0].text, "Enable sharing")
        self.assertEqual(self.service.ledger.snapshot()["proposals"], {})

    async def test_a_first_request_carries_on_after_the_owner_enables_sharing(self):
        await self.message(100, "Hello")  # the general setup note goes out once
        self.assertEqual(self.bot.sent[-1][2].inline_keyboard[0][0].text, "Set up guest sharing")
        deleted = []

        async def delete_message(chat_id, message_id, **kw):
            deleted.append(message_id)
            return True

        object.__setattr__(self.bot, "delete_message", delete_message)
        with patch("hermes_guests.plugin.actor", return_value="100"):
            asked = asyncio.create_task(
                self.plugin.tool(
                    "guests_prepare_access",
                    {"person": "Alex", "scope": {"chat_only": True, "duration_seconds": 3600}},
                )
            )
            for _ in range(200):
                if any("Set up guest sharing?" in t for _, t, _ in self.bot.sent):
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(deleted, [1])  # the setup card replaces the general note
            await self.click(100, self.press("Enable sharing"))
            result = json.loads(await asyncio.wait_for(asked, 5))
        self.assertEqual(
            result.get("result", {}).get("status"), "awaiting_owner_confirmation", result
        )
        texts = [t for who, t, _ in self.bot.sent if who == 100]
        self.assertTrue(any("Preparing what you asked for" in t for t in texts), texts)
        self.assertTrue(any(t.startswith("Share with Alex?") for t in texts), texts)

    async def test_without_sharing_tools_the_request_is_quoted_back_after_enable(self):
        self.plugin.owner_tools = lambda **_: {"ha_get_state": "Read state"}
        await self.message(100, "Let Adrian control the guest room lights for a day.")
        self.assertEqual(len(self.bot.sent), 1)  # one setup card, nothing else
        self.plugin.owner_tools = lambda **_: {  # Enable adds the sharing tools
            "ha_get_state": "Read state",
            **{name: name for name in MANAGEMENT_TOOLS},
        }
        await self.click(100, self.press("Enable sharing"))
        self.assertTrue(
            any(
                "Say “go ahead” and I'll prepare “Let Adrian control the guest room lights" in t
                for _, t, _ in self.bot.sent
            ),
            [t for _, t, _ in self.bot.sent],
        )

    async def test_only_owner_private_turns_receive_guest_tool_guidance(self):
        with patch("hermes_guests.plugin.actor", return_value="100"):
            self.assertIn(
                "tool_search", self.plugin.owner_turn_context(platform="telegram")["context"]
            )
            self.assertIsNone(self.plugin.owner_turn_context(platform="cli"))
        with patch("hermes_guests.plugin.actor", return_value="200"):
            self.assertIsNone(self.plugin.owner_turn_context(platform="telegram"))
        with patch("hermes_guests.plugin.actor", side_effect=GuestsError("Private chat required")):
            self.assertIsNone(self.plugin.owner_turn_context(platform="telegram"))

    async def test_conversational_invitation_window_reappears_in_restored_review(self):
        await self.ready_owner()
        with patch("hermes_guests.plugin.actor", return_value="100"):
            result = json.loads(
                await self.plugin.tool(
                    "guests_prepare_access",
                    {
                        "person": "Alex",
                        "scope": {
                            "tools": [
                                {
                                    "name": "ha_get_state",
                                    "constraints": {"entity_id": {"equals": "light.porch"}},
                                    "presentation": {"operation": "Check the porch light"},
                                }
                            ],
                            "duration_seconds": 3600,
                        },
                        "invitation_seconds": 86400,
                    },
                )
            )
        self.assertTrue(result["ok"], result)
        self.assertIn(
            "The invitation link works for 1 day after you approve.", self.bot.sent[-1][1]
        )
        await self.message(100, "/guests")
        restored = [text for _, text, _ in self.bot.sent if "Share with Alex?" in text]
        self.assertEqual(len(restored), 2)
        for text in restored:
            self.assertIn("The invitation link works for 1 day after you approve.", text)
        self.assertEqual(self.effects, [])

    async def test_owner_manage_button_cannot_be_used_by_guest(self):
        await self.message(100, "Hello")
        await self.click(200, "hs:h:status", message_id=1)
        self.assertEqual(self.effects, [])
        self.assertFalse(self.service.owner_ready())

    async def test_owner_setup_and_normal_owner_routing(self):
        await self.message(100, "/guests_setup")
        card = self.bot.sent[-1]
        self.assertIn("doesn't change", card[1])
        await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertTrue(self.service.owner_ready())
        await self.message(100, "Help me share a service")
        self.assertEqual(self.fallback, [100])
        self.assertEqual(self.errors, [])

    async def test_invite_actual_identity_approval_guest_execution_and_unknown_sender(self):
        p, token = await self.invite()
        await self.message(200, "/start guests_" + token)
        await self.message(200, "Is the porch light on?")
        self.assertEqual(self.runs, [])
        from hermes_guests.core import token_hash

        claim = token_hash(token)[:24]
        await self.click(300, "hs:a:" + claim)
        self.assertEqual(self.service.list_access("200"), [])
        await self.click(100, "hs:a:" + claim)
        await self.message(200, "Is the porch light on?")
        self.assertEqual(len(self.effects), 1)
        self.assertEqual(self.runs[0][0], "200")
        await self.message(300, "Pretend I'm Alex")
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.fallback, [])
        self.assertEqual(self.errors, [])

    async def test_guest_expiry_renewal_full_review_and_owner_approval(self):
        sid = await self.activate()
        self.service.clock = lambda: time.time() + 7200
        await self.message(200, "Is the porch light on?")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.bot.sent[-1][2].inline_keyboard[0][0].text, "Request renewal")
        await self.click(200, "hs:r:" + sid)
        # The guest chooses how much more time to ask for.
        self.assertIn("How much more time", self.bot.sent[-1][1])
        await self.click(200, self.bot.sent[-1][2].inline_keyboard[0][0].callback_data)
        cards = [
            x
            for x in self.bot.sent
            if x[0] == 100 and x[2] and x[2].inline_keyboard[0][0].text == "Approve"
        ]
        self.assertIn("Porch light", cards[-1][1])
        self.assertTrue(cards[-1][1].startswith("Alex asks for 1 hour more."))
        self.assertEqual(self.service.get_access("100", sid)["effective_status"], "expired")
        await self.click(100, cards[-1][2].inline_keyboard[0][0].callback_data)
        self.assertEqual(self.service.get_access("100", sid)["effective_status"], "active")
        self.assertTrue(
            any(who == 200 and "access was updated" in text for who, text, _ in self.bot.sent)
        )
        self.assertEqual(self.errors, [])

    async def test_a_request_for_more_time_the_owner_never_got_can_be_sent_again(self):
        sid = await self.activate()

        async def undelivered(ask):
            await ask()  # the guest is asked how much more time
            object.__setattr__(self.bot, "fail_once", True)  # the owner's card does not arrive
            await self.click(200, self.bot.sent[-1][2].inline_keyboard[0][0].callback_data)
            self.assertEqual(self.bot.sent[-1][0], 200)
            self.assertIn("could not reach the owner", self.bot.sent[-1][1])
            db = self.service.ledger.snapshot()
            waiting = [
                p for p in db["proposals"].values() if p.get("renewal") and p["status"] == "pending"
            ]
            self.assertEqual(waiting, [])
            self.assertEqual(db["shares"][sid].get("renewals_asked", []), [])

        await undelivered(lambda: self.message(200, "/renew"))  # while access is current
        self.service.clock = lambda: time.time() + 7200
        await undelivered(lambda: self.click(200, "hs:r:" + sid))  # and after it has run out
        await self.click(200, "hs:r:" + sid)  # sent again straight away, and this time it arrives
        await self.click(200, self.bot.sent[-1][2].inline_keyboard[0][0].callback_data)
        self.assertIn("with the owner", self.bot.sent[-1][1])
        cards = [x for x in self.bot.sent if x[0] == 100 and x[2] and "Approve" in self.buttons(x)]
        self.assertIn("Porch light", cards[-1][1])
        self.assertEqual(self.errors, [])

    async def test_revocation_notifies_both_ends_and_blocks_subsequent_work(self):
        sid = await self.activate()
        p = self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        if not any(
            markup
            and any(
                b.callback_data == "hs:p:" + p["proposal_id"]
                for row in markup.inline_keyboard
                for b in row
            )
            for _, _, markup in self.bot.sent
        ):
            await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"])
        self.assertTrue(any(who == 200 and "owner ended" in text for who, text, _ in self.bot.sent))
        await self.message(200, "Turn on the porch light")
        self.assertEqual(self.effects, [])
        self.assertIn("new invitation", self.bot.sent[-1][1])
        self.assertIsNone(self.bot.sent[-1][2])

    async def test_full_unicode_review_is_not_truncated_and_buttons_only_on_last_part(self):
        text = (
            "Guest permissions\n"
            + ("📚 All resource details, including the last restriction.\n" * 200)
            + "FINAL EXPIRY AND RESTRICTION"
        )
        before = len(self.bot.sent)
        await self.plugin.send("100", text, [[("Accept", "hs:p:fixture")]])
        parts = self.bot.sent[before:]
        self.assertGreater(len(parts), 1)
        self.assertEqual("".join(p[1].split("\n", 1)[1] for p in parts), text)
        self.assertTrue(all(len(p[1].encode("utf-16-le")) // 2 <= 3400 for p in parts))
        self.assertTrue(all(p[2] is None for p in parts[:-1]))
        self.assertIsNotNone(parts[-1][2])

    async def test_lost_link_can_be_replaced_without_another_scope_review(self):
        p, old = await self.invite()
        await self.click(100, "hs:i:" + p["proposal_id"])
        new = self.bot.sent[-1][1].split("?start=guests_", 1)[1]
        self.assertNotEqual(old, new)
        await self.message(200, "/start guests_" + old)
        self.assertEqual(self.service.list_access("200"), [])
        await self.message(200, "/start guests_" + new)
        self.assertTrue(
            any(who == 100 and "opened your invitation" in text for who, text, _ in self.bot.sent)
        )

    async def test_failed_invitation_delivery_is_retryable_without_duplicate_authority(self):
        p = self.service.prepare("100", "Alex", self.scope())
        await self.plugin.proposal_card(p)
        object.__setattr__(self.bot, "fail_once", True)
        if not any(
            markup
            and any(
                b.callback_data == "hs:p:" + p["proposal_id"]
                for row in markup.inline_keyboard
                for b in row
            )
            for _, _, markup in self.bot.sent
        ):
            await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"])
        self.assertEqual(self.service.list_access("100"), [])
        if not any(
            markup
            and any(
                b.callback_data == "hs:p:" + p["proposal_id"]
                for row in markup.inline_keyboard
                for b in row
            )
            for _, _, markup in self.bot.sent
        ):
            await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"])
        self.assertTrue(any("?start=guests_" in text for _, text, _ in self.bot.sent))
        self.assertEqual(self.errors, [])

    async def test_missing_worker_and_unexpected_failure_never_fall_through_to_owner_agent(self):
        await self.activate()
        self.plugin.runner.run = lambda *args: (_ for _ in ()).throw(
            RuntimeError("worker fixture unavailable")
        )
        await self.message(200, "Help me")
        self.assertEqual(self.fallback, [])
        self.assertEqual(self.errors, [])
        self.assertIn("couldn't complete", self.bot.sent[-1][1])

    async def test_status_restores_pending_confirmation_after_transport_restart(self):
        p, token = await self.invite()
        await self.message(200, "/start guests_" + token)
        self.bot.sent.clear()
        await self.message(100, "/guests")
        self.assertTrue(
            any(
                "opened your invitation" in text and "Porch light" in text
                for _, text, _ in self.bot.sent
            )
        )

    async def test_owner_can_revoke_while_guest_is_waiting_for_a_model(self):
        import threading

        sid = await self.activate()
        started = threading.Event()
        release = threading.Event()

        def slow_run(actor, share_id, text):
            started.set()
            if not release.wait(5):
                raise RuntimeError("fixture timed out")
            self.service.call(actor, share_id, "ha_get_state", {"entity_id": "light.porch"})
            return "Done"

        self.plugin.runner.run = slow_run
        await self.message(200, "Check the light", wait=False)
        await asyncio.to_thread(started.wait, 2)
        self.assertTrue(started.is_set())
        p = self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        try:
            if not any(
                markup
                and any(
                    b.callback_data == "hs:p:" + p["proposal_id"]
                    for row in markup.inline_keyboard
                    for b in row
                )
                for _, _, markup in self.bot.sent
            ):
                await self.plugin.proposal_card(p)
            await self.click(100, "hs:p:" + p["proposal_id"])
            self.assertEqual(self.service.guest("200")["effective_status"], "revoked")
        finally:
            release.set()
        await asyncio.gather(*list(self.plugin._guest_tasks.values()))
        self.assertEqual(self.effects, [])
        self.assertEqual(self.fallback, [])

    async def test_guest_cannot_recover_and_owner_must_confirm_reset(self):
        sid = await self.activate()
        await self.message(200, "/guests_recover")
        self.assertEqual(self.service.guest("200")["effective_status"], "active")
        await self.message(100, "/guests_recover")
        card = self.bot.sent[-1]
        self.assertIn("All current guest access", card[1])
        self.assertEqual(self.service.guest("200")["effective_status"], "active")
        await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertEqual(self.service.ledger.snapshot()["shares"][sid]["status"], "revoked")
        self.assertIsNone(self.plugin.service)
        self.assertIn("finish setup", self.bot.sent[-1][1])

    async def test_copied_button_is_not_a_substitute_for_the_delivered_review(self):
        p = self.service.prepare("100", "Alex", self.scope())
        await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"], message_id=999999)
        self.assertEqual(
            self.service.ledger.snapshot()["proposals"][p["proposal_id"]]["status"], "pending"
        )
        self.assertFalse(any("?start=guests_" in text for _, text, _ in self.bot.sent))
        await self.click(100, "hs:p:" + p["proposal_id"])
        self.assertTrue(any("?start=guests_" in text for _, text, _ in self.bot.sent))

    async def test_replayed_applied_review_cannot_reannounce_revoked_access(self):
        sid = await self.activate()
        p = self.service.prepare("100", "Alex", self.scope(), share_id=sid)
        await self.plugin.proposal_card(p)
        await self.click(100, "hs:p:" + p["proposal_id"])
        revoke = self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.service.approve("100", revoke["proposal_id"])
        before = len(self.bot.sent)
        await self.click(100, "hs:p:" + p["proposal_id"])
        self.assertFalse(any("Access updated" in text for _, text, _ in self.bot.sent[before:]))
        self.assertEqual(self.service.guest("200")["effective_status"], "revoked")

    async def test_declined_or_expired_invitation_can_be_reissued_from_status(self):
        p, token = await self.invite()
        await self.message(200, "/start guests_" + token)
        from hermes_guests.core import token_hash

        await self.click(100, "hs:n:" + token_hash(token)[:24])
        await self.message(100, "/guests")
        card = self.bot.sent[-1]
        self.assertEqual(card[2].inline_keyboard[0][0].text, "Replace invitation link")
        await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertIn("?start=guests_", self.bot.sent[-1][1])
        self.assertEqual(self.service.list_access("100"), [])

    async def test_model_readiness_failure_is_explained_before_owner_setup_is_applied(self):
        from hermes_guests.model import GuestsError

        def unavailable():
            raise GuestsError("Reconnect your model through Hermes, then send /guests_setup.")

        self.plugin.runner.check_readiness = unavailable
        await self.message(100, "/guests_setup")
        self.assertFalse(self.service.owner_ready())
        self.assertIn("Reconnect your model", self.bot.sent[-1][1])
        self.assertIsNone(self.bot.sent[-1][2])
        self.assertEqual(self.fallback, [])

    async def test_owner_denial_notifies_waiting_guest(self):
        p, token = await self.invite()
        await self.message(200, "/start guests_" + token)
        from hermes_guests.core import token_hash

        await self.click(100, "hs:n:" + token_hash(token)[:24])
        self.assertTrue(any(who == 200 and "declined" in text for who, text, _ in self.bot.sent))
        self.assertEqual(self.service.list_access("200"), [])

    async def ready_owner(self):
        await self.message(100, "/guests_setup")
        card = self.bot.sent[-1]
        await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertTrue(self.service.owner_ready())
        return card

    async def open_never(self):
        """Open the never-share list the way an owner does: from setup."""
        await self.message(100, "/guests_setup")
        await self.click(100, self.press("Never-share list"))

    async def never_in_words(self, confirm=True, **args):
        """A never-share change asked for in words: the tool's answer, then Confirm."""
        import json

        with patch("hermes_guests.plugin.actor", return_value="100"):
            answer = json.loads(await self.plugin.tool("guests_never", args))
        if answer["ok"] and confirm:
            await self.click(100, self.press("Confirm"))
        return answer

    def press(self, label):
        """The callback of the newest button whose label contains `label`."""
        for _, _, markup in reversed(self.bot.sent):
            for row in markup.inline_keyboard if markup else []:
                for button in row:
                    if label.lower() in button.text.lower():
                        return button.callback_data
        raise AssertionError("no button " + label)

    async def test_chat_only_button_journey_works_with_no_shareable_tools(self):
        self.plugin.shareable = lambda: {}
        await self.ready_owner()
        await self.message(100, "/guests_new Alex")
        await self.click(100, self.press("Chat only"))
        await self.click(100, self.press("Next"))
        await self.click(100, self.press("1 hour"))
        self.assertIn("Chat with the assistant", self.bot.sent[-1][1])
        self.assertEqual(self.service.list_access("100"), [])
        await self.click(100, self.press("Approve"))
        token = self.bot.sent[-1][1].split("?start=guests_", 1)[1]
        await self.message(200, "/start guests_" + token)
        await self.click(100, self.press("Accept guest"))
        record = self.service.guest("200")
        self.assertTrue(record["scope"]["chat_only"])
        self.assertEqual(record["scope"]["tools"], [])

        def chat(actor, sid, text):
            self.service.admit_chat(actor, sid)
            return "Hello!"

        self.plugin.runner.run = chat
        await self.message(200, "Hello")
        self.assertEqual(self.bot.sent[-1][1], "Hello!")
        for tool in ("ha_get_state", "terminal", "guests_prepare_access"):
            with self.assertRaises(GuestsError):
                self.service.call("200", record["id"], tool, {})
        self.assertEqual(self.effects, [])
        self.assertEqual(self.fallback, [])
        self.assertEqual(self.errors, [])

    async def test_chat_only_and_service_buttons_are_mutually_exclusive(self):
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        await self.ready_owner()
        await self.message(100, "/guests_new Alex")
        await self.click(100, self.press("Read a light"))
        await self.click(100, self.press("Chat only"))
        self.assertTrue(self.plugin.wizard["chat_only"])
        self.assertEqual(self.plugin.wizard["chosen"], set())
        await self.click(100, self.press("Read a light"))
        self.assertFalse(self.plugin.wizard["chat_only"])
        self.assertEqual(self.plugin.wizard["chosen"], {"ha_get_state"})
        self.assertEqual(self.effects, [])

    async def test_setup_explains_owner_warrant_behavior_before_confirmation(self):
        await self.message(100, "/guests_setup")
        text = self.bot.sent[-1][1]
        self.assertIn("Your own use of Hermes doesn't change", text)
        self.assertIn("hermes-tenuo", text)
        self.assertNotIn("guests prepare access", text)
        cleared = []

        async def clear(*args, **kwargs):
            cleared.append(1)
            return True

        object.__setattr__(self.bot, "edit_message_reply_markup", clear)
        await self.click(100, self.press("Details"))
        details = self.bot.sent[-1][1]
        self.assertEqual(cleared, [])  # Enable sharing is still there to press
        self.assertIn("Your tools stay available", details)
        self.assertIn("Setup creates a Tenuo warrant for your reviewed tools", details)
        self.assertFalse(self.service.owner_ready())
        self.assertEqual(self.effects, [])

    async def test_change_buttons_can_replace_services_with_chat_only(self):
        sid = await self.activate()
        before = self.service.get_access("100", sid)
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        await self.plugin.change_start("100", before)
        await self.click(100, self.press("Chat only"))
        self.assertFalse(any(self.plugin.wizard["presets"].values()))
        await self.click(100, self.press("Next: review"))
        self.assertIn("Chat with the assistant", self.bot.sent[-1][1])
        await self.click(100, self.press("Approve"))
        current = self.service.get_access("100", sid)
        self.assertTrue(current["scope"]["chat_only"])
        self.assertEqual(current["scope"]["expires_at"], before["scope"]["expires_at"])
        self.service.admit_chat("200", sid)
        with self.assertRaises(GuestsError):
            self.service.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        self.assertEqual(self.effects, [])
        self.assertEqual(self.errors, [])

    async def test_sharing_with_buttons_needs_no_model(self):
        self.plugin.shareable = lambda: {
            "ha_get_state": "Read a light",
            "web_search": "Search the web",
        }
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.message(100, "/guests_new")
        self.assertIn("followed by their name", self.bot.sent[-1][1])
        await self.message(100, "/guests_new Alex")
        self.assertIn("Share with Alex", self.bot.sent[-1][1])
        await self.click(100, self.press("Next"))
        self.assertIn("at least one tool", self.bot.sent[-1][1])
        await self.click(100, self.press("Read a light"))
        self.assertIn(
            "☑ Read a light", [b.text for row in self.bot.sent[-1][2].inline_keyboard for b in row]
        )
        await self.click(100, self.press("Next"))
        await self.click(100, self.press("No end date"))
        review = self.bot.sent[-1]
        self.assertIn("Share with Alex?", review[1])
        self.assertIn("Access continues until the owner ends it", review[1])
        self.assertIn("any entity id", review[1])  # every rule is on the card
        self.assertNotIn("Search", review[1])
        self.assertEqual(
            [b.text for row in review[2].inline_keyboard for b in row],
            ["Approve", "Change", "Cancel", "Preview as Alex", "Details"],
        )
        await self.click(100, self.press("Details"))
        self.assertIn("Entity id — any value", self.bot.sent[-1][1])
        self.assertEqual(self.fallback, [])  # nothing reached the model
        self.assertEqual(
            self.service.list_access("100"), []
        )  # and nothing is granted before Accept
        await self.click(100, self.press("Approve"))
        self.assertTrue(
            any("?start=guests_" in text for who, text, _ in self.bot.sent if who == 100)
        )
        self.assertEqual(self.errors, [])

    async def test_people_the_bot_already_admits_are_taken_over_at_setup(self):
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        with patch(
            "hermes_guests.plugin.other_people", return_value=[{"id": "300", "label": "Blair"}]
        ):
            await self.message(100, "/guests_setup")
            card = self.bot.sent[-1]
            self.assertIn("1 other person can already use this bot: Blair (300)", card[1])
            self.assertIn("separate conversation and memory", card[1])
            self.assertEqual(
                self.service.list_access("100"), []
            )  # nothing changes before the owner accepts
            await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertIn("Now limited to their own access: Blair", self.bot.sent[-1][1])
        record = self.service.guest("300")
        self.assertTrue(record["scope"]["until_ended"])
        self.assertEqual([e["name"] for e in record["scope"]["tools"]], ["ha_get_state"])
        await self.message(300, "Is the porch light on?")
        self.assertEqual(
            [(actor, text) for actor, _, text in self.runs], [("300", "Is the porch light on?")]
        )
        self.assertNotIn(300, self.fallback)  # the unrestricted agent never saw it
        await self.message(300, "/access")
        self.assertEqual(  # no end date: nothing to renew
            [b.text for row in self.bot.sent[-1][2].inline_keyboard for b in row],
            ["Request more access", "See permissions"],
        )
        # A later request that names this person changes their access, not a second grant.
        self.assertEqual(self.plugin.existing_access("100", "blair"), record["id"])
        self.assertEqual(self.plugin.existing_access("100", "300"), record["id"])
        self.assertIsNone(self.plugin.existing_access("100", "Someone Else"))
        with self.assertRaises(GuestsError):
            self.service.call(
                "300",
                record["id"],
                "ha_call_service",
                {"domain": "light", "service": "turn_on", "entity_id": "light.porch"},
            )
        self.assertEqual(self.errors, [])

    async def test_a_later_setup_leaves_access_already_given_as_it_is(self):
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        sid = await self.activate()  # Alex has their own access, with an end time
        before = self.service.guest("200")
        admitted = [{"id": "200", "label": "Alex"}, {"id": "300", "label": "Blair"}]
        with patch("hermes_guests.plugin.other_people", return_value=admitted):
            await self.message(100, "/guests_setup")
            card = self.bot.sent[-1]
            self.assertIn("1 other person can already use this bot: Blair (300)", card[1])
            self.assertIn("Access you already gave stays exactly as it is: Alex", card[1])
            await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertIn("Now limited to their own access: Blair.", self.bot.sent[-1][1])
        after = self.service.guest("200")
        self.assertEqual((after["id"], after["revision"]), (sid, before["revision"]))
        self.assertEqual(after["scope"], before["scope"])
        self.assertTrue(self.service.guest("300")["scope"]["until_ended"])
        self.assertEqual(self.errors, [])

    async def test_a_later_setup_does_not_give_back_access_that_ended(self):
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        sid = await self.activate()
        ending = self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.service.approve("100", ending["proposal_id"])
        admitted = [{"id": "200", "label": "Alex"}, {"id": "300", "label": "Blair"}]
        with patch("hermes_guests.plugin.other_people", return_value=admitted):
            await self.message(100, "/guests_setup")
            card = self.bot.sent[-1]
            self.assertIn("1 other person can already use this bot: Blair (300)", card[1])
            self.assertIn("stays ended: Alex", card[1])
            await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertIn("Now limited to their own access: Blair.", self.bot.sent[-1][1])
        records = [r for r in self.service.list_access("100") if r["guest_id"] == "200"]
        self.assertEqual([(r["id"], r["status"]) for r in records], [(sid, "revoked")])
        # The service refuses it as well, whoever asks.
        scope = self.service.takeover_scope(["ha_get_state"])
        self.assertEqual(self.service.adopt("100", "200", "Alex", scope=scope)["status"], "ended")
        self.assertEqual(
            len([r for r in self.service.list_access("100") if r["guest_id"] == "200"]), 1
        )
        self.assertEqual(self.errors, [])

    async def test_a_takeover_too_large_to_sign_is_refused_before_setup_is_saved(self):
        huge = dict(
            GUEST_SCHEMAS["ha_get_state"],
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "entity_id": {
                        "type": "string",
                        "enum": ["light." + "x" * 900 + str(i) for i in range(100)],
                    }
                },
                "required": ["entity_id"],
            },
        )
        self.service.tool_resolver = lambda name: huge
        with self.assertRaisesRegex(GuestsError, "too large"):
            self.service.takeover_scope(["ha_get_state"])
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        with patch(
            "hermes_guests.plugin.other_people", return_value=[{"id": "300", "label": "Blair"}]
        ):
            await self.message(100, "/guests_setup")
        self.assertIn("too large", self.bot.sent[-1][1])
        self.assertFalse(self.service.owner_configured())  # nothing was enabled halfway

    async def test_reading_access_or_activity_leaves_the_cards_buttons(self):
        await self.activate()
        cleared = []

        async def clear(*args, **kwargs):
            cleared.append(1)
            return True

        object.__setattr__(self.bot, "edit_message_reply_markup", clear)
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Activity"))
        self.assertEqual(cleared, [])  # End access is still there to press
        await self.click(100, self.press("End access"))
        self.assertEqual(cleared, [1])
        self.assertEqual(self.errors, [])

    async def test_in_group_chats_only_the_owner_reaches_the_agent(self):
        await self.ready_owner()
        for who in (300, 100):
            m = Message(
                5000 + who,
                datetime.now(timezone.utc),
                Chat(-777, "group"),
                from_user=User(who, "Person", False),
                text="hello bot",
            )
            m.set_bot(self.bot)
            self._update_id += 1
            await self.app.process_update(Update(self._update_id, message=m))
        self.assertEqual(self.fallback, [100])

    async def group_says(self, who):
        m = Message(
            6000 + who + len(self.bot.sent),
            datetime.now(timezone.utc),
            Chat(-777, "group"),
            from_user=User(who, "Person", False),
            text="hello bot",
        )
        m.set_bot(self.bot)
        self._update_id += 1
        await self.app.process_update(Update(self._update_id, message=m))

    async def test_a_recovery_does_not_reopen_group_chats_to_other_people(self):
        from hermes_guests.recovery import recover_local_authority

        await self.ready_owner()
        recover_local_authority(self.service.ledger.state, "100")
        self.assertFalse(self.service.owner_ready())  # setup has to be repeated
        self.assertTrue(self.service.owner_configured())  # but sharing was set up here
        await self.group_says(300)
        await self.group_says(100)
        self.assertEqual(self.fallback, [100])

    async def test_owner_audit_is_exposed_with_native_timestamps_and_private_inputs(self):
        await self.activate()
        self.service.setup_owner("100", {"ha_get_state": "Read state"})
        with patch(
            "gateway.session_context.get_session_env",
            side_effect=lambda name, default="": {
                "HERMES_SESSION_PLATFORM": "telegram",
                "HERMES_SESSION_USER_ID": "100",
            }.get(name, default),
        ):
            self.assertIsNone(
                self.plugin.owner_boundary(
                    "ha_get_state", args={"secret": "owner-private-sentinel"}
                )
            )
        with patch("hermes_guests.plugin.actor", return_value="100"):
            reply = json.loads(
                await self.plugin.tool(
                    "guests_activity", {"scope": "owner", "timezone": "EST", "limit": 100}
                )
            )
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["already_displayed"])
        self.assertIsInstance(reply["result"], list)
        self.assertTrue(all(row["actor"] == "100" for row in reply["result"]))
        decisions = [row for row in reply["result"] if row["kind"] == "decision"]
        self.assertEqual([row["tool"] for row in decisions], ["ha_get_state"])
        self.assertEqual(decisions[0]["inputs"], "[owner inputs omitted]")
        self.assertTrue(decisions[0]["local_time"].endswith("-05:00"))
        self.assertIn("timestamp", decisions[0])
        self.assertIsInstance(decisions[0]["at"], float)
        card = self.bot.sent[-1][1]
        self.assertIn("Activity for you", card)
        self.assertIn("Times are EST.", card)
        self.assertIn("You: ha get state", card)  # the owner's inputs stay private
        await self.click(100, self.press("Full log"))
        card = self.bot.sent[-1][1]
        self.assertRegex(card, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} EST")
        self.assertIn("ha_get_state", card)
        self.assertIn("no result recorded", card)
        self.assertNotIn("owner-private-sentinel", card + json.dumps(reply))
        self.assertEqual(self.errors, [])

    async def test_all_user_audit_includes_owner_and_guest_and_preserves_guest_filter(self):
        sid = await self.activate()
        self.service.audit.event(
            "owner_tool_requested", share_id="owner", actor="100", tool="ha_get_state"
        )
        self.service.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        with patch("hermes_guests.plugin.actor", return_value="100"):
            all_rows = json.loads(
                await self.plugin.tool("guests_activity", {"scope": "all", "timezone": "UTC"})
            )
            guest = json.loads(await self.plugin.tool("guests_activity", {"person": "Alex"}))
            own = json.loads(await self.plugin.tool("guests_activity", {"person": "me"}))
        self.assertEqual(
            {
                r["actor"]
                for r in all_rows["result"]
                if r["kind"] in {"decision", "owner_tool_requested"}
            },
            {"100", "200"},
        )
        self.assertTrue(all(r["share_id"] == sid for r in guest["result"]))
        self.assertTrue(all(r["actor"] == "100" for r in own["result"]))
        self.assertTrue(any(r["actor_label"] == "Alex" for r in all_rows["result"]))
        self.assertEqual(self.errors, [])

    async def test_audit_scope_and_timezone_errors_cannot_disclose_owner_records(self):
        sid = await self.activate()
        for scope in ("owner", "all"):
            with self.assertRaises(GuestsError):
                self.service.activity("200", scope=scope)
        with patch("hermes_guests.plugin.actor", return_value="200"):
            reply = json.loads(await self.plugin.tool("guests_activity", {"scope": "all"}))
            self.assertFalse(reply["ok"])
        with patch("hermes_guests.plugin.actor", return_value="100"):
            reply = json.loads(
                await self.plugin.tool(
                    "guests_activity", {"share_id": sid, "timezone": "missing/timezone"}
                )
            )
            self.assertFalse(reply["ok"])
            self.assertIn("timezone", reply["error"])
            for selected in ([], {}, 42, "missing"):
                reply = json.loads(await self.plugin.tool("guests_activity", {"scope": selected}))
                self.assertFalse(reply["ok"])
            for limit in (0, 1001, True, "50"):
                reply = json.loads(
                    await self.plugin.tool("guests_activity", {"scope": "all", "limit": limit})
                )
                self.assertFalse(reply["ok"])
        self.assertEqual(self.errors, [])

    async def test_activity_and_ending_access_work_from_buttons_without_a_model(self):
        sid = await self.activate()
        await self.message(200, "Is the porch light on?")
        with self.assertRaises(Exception):
            self.service.call("200", sid, "ha_get_state", {"entity_id": "light.front_door"})
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Activity"))
        seen = self.bot.sent[-1][1]
        self.assertIn("Activity for Alex", seen)
        self.assertIn("Alex: ha get state (light.porch)", seen)
        self.assertIn("1 action done", seen)
        self.assertIn("tried to ha get state (light.front_door) — not allowed", seen)
        await self.click(100, self.press("Full log"))
        self.assertIn("light.front_door", self.bot.sent[-1][1])
        await self.click(100, self.press("End access"))
        self.assertIn("End all access for Alex?", self.bot.sent[-1][1])
        self.assertEqual(self.service.guest("200")["effective_status"], "active")
        await self.click(100, self.press("Approve"))
        self.assertEqual(self.service.guest("200")["effective_status"], "revoked")
        await self.click(200, "hs:l:" + sid)
        self.assertNotIn("Activity for", self.bot.sent[-1][1])

    @unittest.skipUnless(
        importlib.util.find_spec("tools") and importlib.util.find_spec("tools.tool_search_catalog"),
        "Native Hermes required",
    )
    async def test_an_owners_own_words_find_the_right_tool_in_hermes_tool_search(self):
        from tools.tool_search_catalog import (
            CatalogEntry,
            _entry_search_text,
            _tokenize,
            search_catalog,
        )

        from hermes_guests.plugin import DESCRIPTIONS

        described = dict(
            DESCRIPTIONS,
            library_find="Find titles in a media library.",
            light_set="Turn a light in the house on or off.",
            calendar_list="List upcoming calendar events for a date range.",
            notes_add="Add a note to a notebook.",
        )
        catalog = []
        for name, description in described.items():
            entry = {"type": "function", "function": {"name": name, "description": description}}
            source = "hermes_guests" if name in DESCRIPTIONS else "other"
            catalog.append(
                CatalogEntry(
                    name=name,
                    description=description,
                    schema=entry,
                    source="plugin",
                    source_name=source,
                    _tokens=_tokenize(_entry_search_text(entry, source)),
                )
            )
        expect = {
            "hermes_guests end access": "guests_prepare_access",
            "hermes_guests revoke permission": "guests_prepare_access",
            "stop sharing with guest": "guests_prepare_access",
            "share tools with someone": "guests_prepare_access",
            "permanent access no end date": "guests_prepare_access",
            "what has person done activity": "guests_activity",
            "guest history log": "guests_activity",
            "who has access": "guests_list_access",
            "which tools can be shared": "guests_discover",
            "let a friend use a tool": "guests_prepare_access",
            "remove user access": "guests_prepare_access",
            "restrict guest": "guests_prepare_access",
            "what did the user do": "guests_activity",
            "denied attempts": "guests_activity",
            "my owner audit history": "guests_activity",
            "audit for all users": "guests_activity",
            "timestamps owner activity": "guests_activity",
            "list guests": "guests_list_access",
        }
        for query, tool in expect.items():
            found = [e.name for e in search_catalog(catalog, query, limit=3)]
            self.assertIn(tool, found, query + " found " + str(found))

    async def test_group_chats_stay_closed_to_others_while_the_owners_setup_needs_attention(self):
        await self.ready_owner()
        from hermes_guests.recovery import recover_local_authority

        recover_local_authority(self.service.ledger.state, "100")
        self.assertFalse(self.service.owner_ready())
        for who in (300, 100):
            m = Message(
                6000 + who,
                datetime.now(timezone.utc),
                Chat(-778, "group"),
                from_user=User(who, "Person", False),
                text="hello bot",
            )
            m.set_bot(self.bot)
            self._update_id += 1
            await self.app.process_update(Update(self._update_id, message=m))
        self.assertEqual(self.fallback, [100])

    def buttons(self, message):
        return [b.text for row in message[2].inline_keyboard for b in row] if message[2] else []

    async def test_preview_names_the_returning_account_and_cannot_approve(self):
        sid = await self.activate()
        ended = self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.service.approve("100", ended["proposal_id"])
        back = self.service.prepare("100", "Alex", self.scope(), share_id=sid)
        await self.plugin.proposal_card(back)
        await self.click(100, self.press("Preview as Alex"))
        preview = self.bot.sent[-1]
        self.assertIn("Preview only", preview[1])
        self.assertEqual(self.buttons(preview), ["Back to review"])
        self.assertIn("This switches access back on for the same Telegram account", preview[1])
        self.assertIn("ID 200", preview[1])
        self.assertIn("No invitation is sent and the account is not confirmed again", preview[1])
        preview_id = len(self.bot.sent)
        await self.click(100, "hs:p:" + back["proposal_id"], message_id=preview_id)
        self.assertIn("Use the latest complete review", self.bot.sent[-1][1])
        self.assertEqual(
            self.service.ledger.snapshot()["proposals"][back["proposal_id"]]["status"], "pending"
        )

    async def test_owner_previews_what_the_guest_will_see_and_nothing_is_granted(self):
        p = self.service.prepare("100", "Alex", self.scope())
        await self.plugin.proposal_card(p)
        self.assertIn("Preview as Alex", self.buttons(self.bot.sent[-1]))
        before = len(self.bot.sent)
        await self.click(100, self.press("Preview as Alex"))
        preview = self.bot.sent[-1]
        self.assertIn("Preview only", preview[1])
        self.assertIn("Here’s what you can do:", preview[1])
        self.assertIn("turn it on or off", preview[1])
        self.assertIn("Send a message to start", preview[1])
        self.assertEqual(self.buttons(preview), ["Back to review"])
        self.assertTrue(all(who == 100 for who, _, _ in self.bot.sent[before:]))  # only the owner
        db = self.service.ledger.snapshot()
        self.assertEqual(db["invites"], {})
        self.assertEqual(db["proposals"][p["proposal_id"]]["status"], "pending")
        self.assertEqual(self.service.list_access("100"), [])
        await self.click(200, "hs:g:" + p["proposal_id"])  # someone else pressing a copy
        self.assertNotIn("Preview only", self.bot.sent[-1][1])
        await self.click(100, self.press("Back to review"))
        await self.click(100, self.press("Approve"))
        self.assertTrue(any("?start=guests_" in text for _, text, _ in self.bot.sent))

    async def test_the_original_review_still_works_after_a_preview(self):
        p = self.service.prepare("100", "Alex", self.scope())
        await self.plugin.proposal_card(p)
        review_message = len(self.bot.sent)  # message ids count from 1
        await self.click(100, self.press("Preview as Alex"))
        await self.click(100, "hs:p:" + p["proposal_id"], message_id=review_message)
        self.assertTrue(any("?start=guests_" in text for _, text, _ in self.bot.sent))

    async def test_tools_are_named_in_a_few_words_on_buttons_and_reviews(self):
        from hermes_guests.permissions import short_label

        long = (
            "Extract content from web page URLs. Returns clean page content in markdown/text "
            "(no LLM summarization — fast). Also works with PDF URLs."
        )
        self.assertEqual(short_label(long, "web_extract"), "Extract content from web page URLs")
        self.assertEqual(
            short_label("Search the web: returns titles, URLs and snippets (5 by default)."),
            "Search the web",
        )
        self.assertEqual(short_label("", "todo_list"), "Todo list")
        self.assertLessEqual(
            len(
                short_label(
                    "Control, query, and automate every entity exposed by the configured instance",
                    limit=34,
                )
            ),
            34,
        )
        schema = dict(GUEST_SCHEMAS["web_search"], description=long)
        self.plugin.resolve_tool = {"web_search": schema}.get
        self.service.tool_resolver = {"web_search": schema}.get
        text = self.service.prepare(
            "100",
            "Alex",
            {
                "tools": [
                    {
                        "name": "web_search",
                        "constraints": {"query": {"any": True}, "limit": {"any": True}},
                    }
                ],
                "duration_seconds": 3600,
            },
        )["text"]
        self.assertIn("Extract content from web page URLs", text)
        self.assertNotIn("Returns clean page content", text)

    async def test_ending_access_has_no_preview(self):
        sid = await self.activate()
        await self.plugin.proposal_card(
            self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        )
        self.assertEqual(self.buttons(self.bot.sent[-1]), ["Approve", "Change", "Cancel"])

    async def test_guest_welcome_says_what_they_can_ask_for(self):
        await self.activate()
        welcome = [
            text
            for who, text, _ in self.bot.sent
            if who == 200 and "Here’s what you can do" in text
        ][-1]
        self.assertIn("Here’s what you can do:", welcome)
        self.assertIn("Send a message to start", welcome)

    async def test_guest_asks_for_more_and_the_owner_adds_it_with_buttons(self):
        self.plugin.shareable = lambda: {
            "ha_get_state": "Read a light",
            "web_search": "Search the web",
        }
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.activate()
        await self.message(200, "/access")
        self.assertIn("Request more access", self.buttons(self.bot.sent[-1]))
        await self.click(200, self.press("Request more access"))
        self.assertIn("next message", self.bot.sent[-1][1])
        runs = len(self.runs)
        await self.message(200, "Please let me search\u200b the web\u202e too")
        self.assertEqual(len(self.runs), runs)  # not handed to the guest's assistant
        self.assertEqual(self.fallback, [])  # nor to the owner's
        card = [m for m in self.bot.sent if m[0] == 100][-1]
        self.assertIn("Alex asks for more access", card[1])
        self.assertIn("“Please let me search the web too”", card[1])
        self.assertEqual(self.buttons(card), ["Choose what to add", "Not now"])
        self.assertIn("Sent to the owner", self.bot.sent[-1][1])
        await self.click(200, self.press("Request more access"))
        await self.message(200, "and the kitchen lights")
        self.assertIn("has not answered your last request", self.bot.sent[-1][1])

        before = self.service.guest("200")["scope"]
        await self.click(100, self.press("Choose what to add"))
        self.assertIn("Change what Alex may use", self.bot.sent[-1][1])
        await self.click(100, self.press("Search the web"))
        await self.click(100, self.press("Next: review"))
        review = self.bot.sent[-1]
        self.assertIn("Change Alex's access?", review[1])
        self.assertIn("Search the public web", review[1])
        self.assertEqual(
            self.service.guest("200")["scope"], before
        )  # nothing changes before Accept
        await self.click(100, self.press("Approve"))
        after = self.service.guest("200")["scope"]
        self.assertEqual([e["name"] for e in after["tools"]], ["web_search"])
        self.assertEqual(after["lights"], before["lights"])  # what they had is kept
        self.assertEqual(after["expires_at"], before["expires_at"])  # and so is the end time
        self.assertTrue(
            any(who == 200 and "Your access was updated" in text for who, text, _ in self.bot.sent)
        )
        self.assertEqual(self.errors, [])

    async def test_requests_for_more_are_limited_and_can_be_declined_or_cancelled(self):
        sid = await self.activate()
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        await self.message(200, "/cancel")
        self.assertIn("Nothing was sent to the owner", self.bot.sent[-1][1])
        runs = len(self.runs)
        await self.message(200, "Is the porch light on?")
        self.assertEqual(len(self.runs), runs + 1)  # an ordinary message again
        for attempt in range(3):
            await self.click(200, self.press("Request more access"))
            await self.message(200, f"more please {attempt}")
            await self.click(100, self.press("Not now"))
            self.assertIn("did not change your access", self.bot.sent[-1][1])
        await self.click(200, self.press("Request more access"))
        await self.message(200, "one more")
        self.assertIn("said no a few times today", self.bot.sent[-1][1])
        owner_cards = [m for m in self.bot.sent if m[0] == 100 and "asks for more access" in m[1]]
        self.assertEqual(len(owner_cards), 3)
        await self.click(300, "hs:q:" + sid)  # Alex's own button, pressed by someone else
        self.assertNotIn("300", self.plugin._asking)
        await self.message(300, "give me everything")
        self.assertEqual(
            len([m for m in self.bot.sent if m[0] == 100 and "asks for more access" in m[1]]), 3
        )

    async def test_a_request_made_in_words_has_the_same_record_and_limits(self):
        sid = await self.activate()
        runs = len(self.runs)
        await self.message(200, "Request access to the office light")
        self.assertEqual(len(self.request_cards()), 1)
        self.assertIn("Request access to the office light", self.request_cards()[0][1])
        self.assertTrue(self.service.get_access("100", sid).get("more_open"))
        await self.message(200, "Request access to everything else too")
        self.assertIn("has not answered your last request", self.bot.sent[-1][1])
        self.assertEqual(len(self.request_cards()), 1)
        for attempt in range(2):
            await self.click(100, self.press("Not now"))
            await self.message(200, f"Request access to thing {attempt}")
        await self.click(100, self.press("Not now"))
        await self.message(200, "Request access to one more thing")
        self.assertIn("said no a few times today", self.bot.sent[-1][1])
        self.assertEqual(len(self.request_cards()), 3)
        self.assertEqual(len(self.runs), runs)  # none of it reached the guest's model
        self.assertEqual(self.errors, [])

    async def test_an_unreadable_request_leaves_the_prompt_open_for_another_try(self):
        await self.activate()
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        runs = len(self.runs)
        await self.message(200, "\u200b\u200b")  # nothing readable
        self.assertIn("short text message", self.bot.sent[-1][1])
        await self.message(200, "Could I also use the office light?")
        self.assertEqual(len(self.request_cards()), 1)  # the second try went to the owner
        self.assertEqual(len(self.runs), runs)  # and neither went to the assistant

    async def test_a_late_answer_to_the_request_prompt_goes_nowhere_and_says_so(self):
        await self.activate()
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        self.plugin._asking["200"] = time.time() - 1  # the ten minutes have passed
        runs = len(self.runs)
        await self.message(200, "Could I also use the office light?")
        self.assertIn("timed out", self.bot.sent[-1][1])
        self.assertEqual((len(self.runs), len(self.request_cards())), (runs, 0))
        await self.message(200, "Is the porch light on?")
        self.assertEqual(len(self.runs), runs + 1)  # an ordinary message again
        self.assertEqual(self.errors, [])

    async def test_only_the_person_whose_access_it_is_can_ask_from_its_button(self):
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        alex = await self.activate()
        scope = self.service.takeover_scope(["ha_get_state"])
        blair = self.service.adopt("100", "300", "Blair", scope=scope)["share_id"]
        # Blair has access of their own and presses the button on Alex's access.
        await self.click(300, "hs:q:" + alex)
        self.assertIn("no longer available", self.bot.sent[-1][1])
        self.assertNotIn("300", self.plugin._asking)
        runs = len(self.runs)
        await self.message(300, "give me everything Alex has")
        self.assertEqual(self.request_cards(), [])  # no request reached the owner
        self.assertEqual(
            len(self.runs), runs + 1
        )  # it was an ordinary message to Blair's assistant
        # Someone with no access at all, and the owner, cannot start one for Alex either.
        for other in (400, 100):
            await self.click(other, "hs:q:" + alex)
            self.assertNotIn(str(other), self.plugin._asking)
        self.assertFalse(self.service.get_access("100", alex).get("more_open"))
        # Blair's own button works, and the request is recorded on Blair's access only.
        await self.click(300, "hs:q:" + blair)
        await self.message(300, "Could I read the office light too?")
        cards = self.request_cards()
        self.assertEqual(len(cards), 1)
        self.assertIn("Blair asks for more access", cards[0][1])
        self.assertTrue(self.service.get_access("100", blair).get("more_open"))
        self.assertFalse(self.service.get_access("100", alex).get("more_open"))
        # The answer belongs to the owner: neither guest can press it.
        answer = self.press("Not now")
        for guest in (200, 300):
            await self.click(guest, answer)
            self.assertTrue(self.service.get_access("100", blair).get("more_open"))
        await self.click(100, answer)
        self.assertFalse(self.service.get_access("100", blair).get("more_open"))
        self.assertEqual(self.errors, [])

    async def ask(self, words):
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        await self.message(200, words)

    def request_cards(self):
        return [m for m in self.bot.sent if m[0] == 100 and "asks for more access" in m[1]]

    async def test_an_unanswered_request_stays_open_however_long_it_waits(self):
        sid = await self.activate()
        await self.ask("first")
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["more_asked"] = [time.time() - 3 * 86400]  # asked three days ago
        await self.ask("second")
        self.assertIn("has not answered your last request", self.bot.sent[-1][1])
        self.assertEqual(len(self.request_cards()), 1)

    async def test_only_a_requests_own_buttons_answer_it(self):
        sid = await self.activate()
        await self.ask("request A")
        card_a = self.request_cards()[-1]
        not_now_a = card_a[2].inline_keyboard[0][1].callback_data
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(
            100, self.press("Change what they can use")
        )  # looking at changes answers nothing
        self.assertTrue(self.service.guest("200").get("more_open"))
        await self.click(100, not_now_a)
        self.assertFalse(self.service.guest("200").get("more_open"))
        await self.ask("request B")
        self.assertEqual(self.service.guest("200")["more_open"]["words"], "request B")
        await self.click(100, not_now_a)  # the old card again
        self.assertIn("already answered", self.bot.sent[-1][1])
        self.assertEqual(self.service.guest("200")["more_open"]["words"], "request B")
        self.assertEqual(self.service.guest("200")["id"], sid)

    async def test_an_approved_change_answers_an_open_request(self):
        sid = await self.activate()
        await self.ask("more please")
        p = self.service.prepare("100", "Alex", self.scope(), share_id=sid)
        self.service.approve("100", p["proposal_id"])
        record = self.service.guest("200")
        self.assertEqual(record["revision"], 2)
        self.assertEqual(len(record["more_asked"]), 1)
        self.assertFalse(record.get("more_open"))

    async def test_a_request_the_owner_could_not_be_told_about_is_taken_back(self):
        await self.activate()
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        object.__setattr__(self.bot, "fail_once", True)  # the owner's card fails to send
        await self.message(200, "more please")
        self.assertIn("nothing was sent", self.bot.sent[-1][1])
        record = self.service.guest("200")
        self.assertFalse(record.get("more_open"))
        self.assertEqual(record.get("more_asked", []), [])  # it does not count
        await self.ask("more please")  # and can be sent again
        self.assertEqual(len(self.request_cards()), 1)

    async def test_status_shows_requests_still_waiting_for_an_answer(self):
        await self.activate()
        await self.ask("the kitchen lights too")
        before = len(self.request_cards())
        await self.message(100, "/guests")
        cards = self.request_cards()
        self.assertEqual(len(cards), before + 1)
        self.assertIn("the kitchen lights too", cards[-1][1])
        await self.click(100, self.press("Not now"))
        self.assertFalse(self.service.guest("200").get("more_open"))

    async def test_tools_a_preset_already_covers_are_not_offered_as_additions(self):
        self.plugin.shareable = lambda all_tools=False: {
            "ha_get_state": "Read a light",
            "ha_call_service": "Switch a light",
            "web_search": "Search the web",
        }
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.activate()  # a light-preset guest
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Change what they can use"))
        self.assertEqual(
            self.buttons(self.bot.sent[-1]),
            ["☐ Chat only", "☐ Search the web", "☑ Lights (1 chosen)", "Next: review"],
        )

    async def test_a_preset_can_be_taken_away_in_the_change_picker(self):
        self.plugin.shareable = lambda all_tools=False: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.activate()  # a light-preset guest
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Change what they can use"))
        await self.click(100, self.press("Lights (1 chosen)"))
        self.assertIn("☐ Lights (1 chosen)", self.buttons(self.bot.sent[-1]))
        await self.click(100, self.press("Next: review"))
        self.assertIn(
            "Choose Chat only or at least one tool", self.bot.sent[-1][1]
        )  # nothing would be left
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Change what they can use"))
        await self.click(100, self.press("Lights (1 chosen)"))
        await self.click(100, self.press("Search the web"))
        await self.click(100, self.press("Next: review"))
        review = self.bot.sent[-1]
        self.assertIn("Search the public web", review[1])
        self.assertNotIn("Porch light", review[1])
        await self.click(100, self.press("Approve"))
        after = self.service.guest("200")["scope"]
        self.assertEqual(
            (after["lights"], [e["name"] for e in after["tools"]]), ([], ["web_search"])
        )

    async def test_every_word_can_be_taken_off_even_without_a_button(self):
        self.plugin.shareable = lambda all_tools=False: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        for i in range(14):
            self.service.never_set("100", "scoped", f"word number {i}", True, tool="web_search")
        await self.open_never()
        card = self.bot.sent[-1]
        self.assertEqual(len([b for b in self.buttons(card) if b.startswith("✕")]), 12)
        self.assertIn("tell me in words", card[1])
        long_word = "a phrase longer than a tool name may be " + "x" * 30  # 67 characters
        self.service.never_set("100", "scoped", long_word, True, tool="web_search")
        await self.never_in_words(action="remove", kind="word", value=long_word)
        self.assertNotIn(long_word, [r["value"] for r in self.service.never().scoped])
        await self.never_in_words(action="remove", kind="word", value="word number 13")
        self.assertNotIn("word number 13", [r["value"] for r in self.service.never().scoped])
        self.assertEqual(len(self.service.never().scoped), 13)

    async def test_a_tool_without_a_button_can_be_listed_by_name(self):
        tools = {"ha_get_state": "Read a light", "web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: dict(tools)
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.never_in_words(action="add", kind="tool", value="web_search")
        self.assertTrue(self.service.never().blocks_tool("web_search"))
        refused = await self.never_in_words(action="add", kind="tool", value="no_such_tool")
        self.assertIn("No shareable tool is called no_such_tool", refused["error"])
        await self.never_in_words(action="remove", kind="tool", value="web_search")
        self.assertFalse(self.service.never().blocks_tool("web_search"))

    async def test_examples_do_not_promise_more_than_the_access_allows(self):
        from hermes_guests.model import simple_access

        open_tool = {
            "name": "light_set",
            "constraints": {"light": {"any": True}},
            "schema": {"description": "Turn a light on or off."},
        }
        limited = {
            "name": "light_set",
            "constraints": {"light": {"equals": "office"}},
            "schema": {"description": "Turn a light on or off."},
        }
        self.assertIn(
            "• Turn a light on or off (any light) · light_set",
            simple_access({"tools": [open_tool], "until_ended": True}, {}),
        )
        self.assertIn(
            'light: "office"', simple_access({"tools": [limited], "until_ended": True}, {})
        )

    async def test_owner_changes_access_from_the_status_card(self):
        self.plugin.shareable = lambda: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.activate()
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        self.assertIn("Change what they can use", self.buttons(self.bot.sent[-1]))
        await self.click(100, self.press("Change what they can use"))
        self.assertIn("Change what Alex may use", self.bot.sent[-1][1])
        self.assertIn("Next: review", self.buttons(self.bot.sent[-1]))

    async def test_everything_a_person_has_can_be_unticked_however_many_tools_there_are(self):
        many = {f"a_tool_{i:02d}": f"Tool {i}" for i in range(30)}
        self.plugin.shareable = lambda: dict(many, web_search="Search the web")
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        sid = await self.activate()
        search = {
            "tools": [{"name": "web_search", "constraints": {"query": {"any": True}}}],
            "duration_seconds": 3600,
        }
        await self.plugin.proposal_card(self.service.prepare("100", "Alex", search, share_id=sid))
        await self.click(100, self.press("Approve"))
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Change what they can use"))
        shown = self.buttons(self.bot.sent[-1])
        self.assertIn("☑ Search the web", shown)  # sorted last by name, still listed
        self.assertEqual(len([b for b in shown if b.startswith("☐ Tool ")]), 23)

    async def test_a_mistyped_tool_name_is_answered_with_the_exact_names(self):
        tools = {"ha_get_state": "Read a light", "web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: dict(tools)
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        said = await self.never_in_words(action="add", kind="tool", value="websearch")
        self.assertIn("Did you mean: web_search (Search the web)", said["error"])
        said = await self.never_in_words(action="add", kind="word", value="bitcoin", tool="search")
        self.assertIn("web_search (Search the web)", said["error"])
        said = await self.never_in_words(action="add", kind="tool", value="zzz")
        self.assertIn("The exact names are: ha_get_state (Read a light), web_search", said["error"])
        self.assertNotIn("brackets on a review", said["error"])
        self.assertFalse(self.service.never())  # none of the mistypes changed the list
        await self.never_in_words(action="add", kind="tool", value="web_search")
        self.assertTrue(self.service.never().blocks_tool("web_search"))

    async def test_a_greeting_word_can_be_given_as_the_word_to_block(self):
        tools = {"web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: dict(tools)
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        for word in ("hello", "manage access"):
            await self.open_never()
            await self.click(100, self.press("＋ Block a word in one tool"))
            await self.click(100, self.press("Search the web"))
            await self.message(100, word)
            self.assertIn("in Search the web only: " + word, self.bot.sent[-1][1])
        self.assertEqual(
            [r["value"] for r in self.service.never().scoped], ["hello", "manage access"]
        )
        await self.message(100, "hello")  # with no prompt open it is a greeting again
        self.assertEqual(self.fallback, [100])
        await self.open_never()
        await self.click(100, self.press("＋ Block a word in one tool"))
        await self.click(100, self.press("Search the web"))
        await self.message(100, "/cancel")
        self.assertIn("Nothing was added", self.bot.sent[-1][1])
        self.assertEqual(self.fallback, [100])

    async def test_a_word_that_is_not_accepted_leaves_the_prompt_open_for_another_try(self):
        tools = {"web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: dict(tools)
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.open_never()
        await self.click(100, self.press("＋ Block a word in one tool"))
        await self.click(100, self.press("Search the web"))
        await self.message(100, "ab")  # too short to be a word to block
        self.assertIn("Send another word", self.bot.sent[-1][1])
        await self.message(100, "bitcoin")  # the second try is still the answer
        self.assertEqual([r["value"] for r in self.service.never().scoped], ["bitcoin"])
        self.assertEqual(self.fallback, [])  # neither try reached the owner's assistant

    async def test_a_late_answer_to_the_never_share_word_prompt_is_not_passed_on(self):
        tools = {"web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: dict(tools)
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.open_never()
        await self.click(100, self.press("＋ Block a word in one tool"))
        await self.click(100, self.press("Search the web"))
        self.plugin._never_word["until"] = time.time() - 1  # the ten minutes have passed
        await self.message(100, "bitcoin")
        self.assertIn("longer than ten minutes", self.bot.sent[-1][1])
        self.assertEqual(self.service.never().scoped, [])
        self.assertEqual(self.fallback, [])  # the owner's assistant never saw the word
        await self.message(100, "what is the weather")
        self.assertEqual(self.fallback, [100])  # an ordinary message again

    async def test_owner_keeps_a_never_share_list_from_telegram(self):
        tools = {"ha_get_state": "Read a light", "web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: {
            n: d for n, d in tools.items() if all_tools or not self.service.never().blocks_tool(n)
        }
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.open_never()
        card = self.bot.sent[-1]
        self.assertIn("(nothing yet)", card[1])
        self.assertEqual(
            self.buttons(card),
            [
                "☐ Read a light",
                "☐ Search the web",
                "＋ Block a word everywhere",
                "＋ Block a word in one tool",
            ],
        )
        await self.click(100, self.press("Search the web"))
        card = self.bot.sent[-1]
        self.assertIn("• tool: Search the web", card[1])
        self.assertIn("🚫 Search the web", self.buttons(card))
        await self.click(100, self.press("＋ Block a word everywhere"))
        await self.message(100, "kitchen")
        self.assertIn("• word, everywhere: kitchen", self.bot.sent[-1][1])
        self.assertIn("✕ kitchen", self.buttons(self.bot.sent[-1]))
        await self.click(100, self.press("＋ Block a word everywhere"))
        await self.message(100, "ab")
        self.assertIn("between 3 and 80", self.bot.sent[-1][1])
        await self.message(100, "/cancel")
        self.assertIn("Nothing was added", self.bot.sent[-1][1])

        await self.message(100, "/guests_new Alex")  # the listed tool is not offered
        self.assertEqual(
            self.buttons(self.bot.sent[-1]), ["☐ Chat only", "☐ Read a light", "Next: how long"]
        )
        await self.click(200, "hs:N:list")  # not the owner
        await self.click(200, self.press("＋ Block a word everywhere"))
        await self.message(200, "everything")
        self.assertEqual(self.service.never().values, ["kitchen"])
        self.assertEqual(self.fallback, [])

        await self.open_never()
        await self.click(100, self.press("✕ kitchen"))  # a word comes off with its button
        await self.click(100, self.press("🚫 Search the web"))
        self.assertFalse(self.service.never())
        self.assertEqual(self.errors, [])

    async def test_owner_blocks_a_word_in_one_tool_with_buttons_or_in_words(self):
        tools = {"ha_get_state": "Read a light", "web_search": "Search the web"}
        self.plugin.shareable = lambda all_tools=False: dict(tools)
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.open_never()
        await self.click(100, self.press("＋ Block a word in one tool"))
        self.assertEqual(self.buttons(self.bot.sent[-1]), ["Read a light", "Search the web"])
        await self.click(100, self.press("Search the web"))
        self.assertIn("as your next message", self.bot.sent[-1][1])
        await self.message(100, "bitcoin")
        self.assertEqual(self.fallback, [])  # the word went to the list, not to the model
        card = self.bot.sent[-1]
        self.assertIn("• word, in Search the web only: bitcoin", card[1])
        self.assertEqual(
            self.service.never().scoped, [{"tool": "web_search", "field": "", "value": "bitcoin"}]
        )

        await self.never_in_words(
            action="add", kind="word", value="lottery", tool="web_search", input="query"
        )
        self.assertIn("• word, in Search the web → query only: lottery", self.bot.sent[-1][1])
        said = await self.never_in_words(
            action="add", kind="word", value="lottery", tool="web_search", input="nope"
        )
        self.assertIn("has no input called nope", said["error"])
        said = await self.never_in_words(
            action="add", kind="word", value="lottery", tool="no_such_tool"
        )
        self.assertIn("No shareable tool is called no_such_tool", said["error"])
        self.assertIn("web_search (Search the web)", said["error"])  # the names to use

        await self.open_never()
        await self.click(100, self.press("＋ Block a word in one tool"))
        await self.click(100, self.press("Read a light"))
        await self.message(100, "/cancel")
        self.assertIn("Nothing was added", self.bot.sent[-1][1])
        await self.message(100, "What is on my calendar today?")
        self.assertEqual(self.fallback, [100])  # an ordinary message again
        self.assertEqual(len(self.service.never().scoped), 2)

        await self.open_never()
        await self.click(100, self.press("✕ bitcoin"))
        self.assertEqual([r["value"] for r in self.service.never().scoped], ["lottery"])
        self.assertEqual(self.errors, [])

    async def test_review_and_activity_say_what_the_never_share_list_did(self):
        sid = await self.activate()
        self.service.never_set("100", "values", "light.porch", True)
        await self.message(200, "Is the porch light on?")  # the fixture guest reads light.porch
        self.assertEqual(self.effects, [])
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Activity"))
        self.assertIn("— your never-share list", self.bot.sent[-1][1])
        await self.plugin.proposal_card(
            {
                "proposal_id": self.service.prepare("100", "Blair", self.scope_without_lights())[
                    "proposal_id"
                ],
                "text": "Access for Blair",
                "left_out": ["light_set · light: front_door_lock"],
            }
        )
        self.assertIn(
            "Left out because of your never-share list:\n• light_set · light: front_door_lock",
            self.bot.sent[-1][1],
        )
        self.assertEqual(self.service.guest("200")["id"], sid)

    async def test_a_restored_review_still_says_what_the_never_share_list_removed(self):
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        self.service.never_set("100", "values", "bitcoin", True)
        choices = {"query": {"one_of": ["weather", "bitcoin price"]}}
        p = self.service.prepare(
            "100",
            "Blair",
            {"tools": [{"name": "web_search", "constraints": choices}], "duration_seconds": 3600},
        )
        self.assertEqual(p["left_out"], ["web_search · query: bitcoin price"])
        # As /guests restores it: from what was saved, with no list handed over.
        await self.plugin.proposal_card({"proposal_id": p["proposal_id"], "text": p["text"]})
        self.assertIn(
            "Left out because of your never-share list:\n• web_search · query: bitcoin price",
            self.bot.sent[-1][1],
        )
        await self.click(100, self.press("Approve"))  # the saved note is part of what is approved
        self.assertTrue(any("?start=guests_" in text for _, text, _ in self.bot.sent))
        self.assertEqual(self.errors, [])

    async def test_a_change_opened_before_another_change_is_not_applied_over_it(self):
        self.plugin.shareable = lambda: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        sid = await self.activate()

        def search(rule):
            return {
                "tools": [{"name": "web_search", "constraints": {"query": rule}}],
                "duration_seconds": 3600,
            }

        await self.plugin.proposal_card(
            self.service.prepare("100", "Alex", search({"any": True}), share_id=sid)
        )
        await self.click(100, self.press("Approve"))
        await self.message(100, "/guests")
        await self.click(100, self.press("Manage access"))
        await self.click(100, self.press("Change what they can use"))
        picker_next = self.press("Next: review")  # opened while any search was allowed
        await self.plugin.proposal_card(
            self.service.prepare("100", "Alex", search({"equals": "weather"}), share_id=sid)
        )
        await self.click(100, self.press("Approve"))
        await self.click(100, picker_next)
        self.assertIn("changed in the meantime", self.bot.sent[-1][1])
        self.assertEqual(
            self.service.guest("200")["scope"]["tools"][0]["constraints"]["query"],
            {"equals": "weather"},
        )
        with self.service.ledger.transaction() as db:
            self.assertFalse([p for p in db["proposals"].values() if p["status"] == "pending"])

    def scope_without_lights(self):
        return {"lights": [], "research": True, "expires_at": iso(time.time() + 3600)}

    async def test_takeover_shows_the_exact_inputs_and_grants_what_was_reviewed(self):
        self.plugin.shareable = lambda: {"ha_get_state": "Read a light"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        others = patch(
            "hermes_guests.plugin.other_people", return_value=[{"id": "300", "label": "Blair"}]
        )
        others.start()
        self.addCleanup(others.stop)
        await self.message(100, "/guests_setup")
        card = self.bot.sent[-1]
        self.assertIn("Entity id — any value", card[1])  # the inputs, not only a name
        changed = dict(GUEST_SCHEMAS["ha_get_state"], description="Now it also unlocks doors.")
        self.service.tool_resolver = {"ha_get_state": changed}.get  # the tool changes before Accept
        await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertIn("A tool changed since this setup review", self.bot.sent[-1][1])
        self.assertFalse(self.service.owner_ready())  # nothing was committed
        self.assertEqual(self.service.list_access("100"), [])

    async def test_takeover_with_nothing_shareable_enables_sharing_and_grants_nothing(self):
        self.plugin.shareable = lambda: {}
        others = patch(
            "hermes_guests.plugin.other_people", return_value=[{"id": "300", "label": "Blair"}]
        )
        others.start()
        self.addCleanup(others.stop)
        await self.message(100, "/guests_setup")
        card = self.bot.sent[-1]
        self.assertIn("and no tools", card[1])
        await self.click(100, card[2].inline_keyboard[0][0].callback_data)
        self.assertTrue(self.service.owner_ready())
        self.assertEqual(self.service.list_access("100"), [])
        self.assertEqual(self.errors, [])

    async def test_a_broken_sharing_state_keeps_groups_closed_to_others(self):
        await self.ready_owner()
        self.plugin.ensure = lambda: (_ for _ in ()).throw(
            GuestsError("Saved access is unavailable.")
        )
        for who in (300, 100):
            m = Message(
                7000 + who,
                datetime.now(timezone.utc),
                Chat(-779, "group"),
                from_user=User(who, "Person", False),
                text="hello bot",
            )
            m.set_bot(self.bot)
            self._update_id += 1
            await self.app.process_update(Update(self._update_id, message=m))
        self.assertEqual(self.fallback, [100])  # the owner is answered; the other person is not

    async def test_a_name_means_the_same_person_after_their_access_runs_out(self):
        sid = await self.activate()
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["scope"]["expires_at"] = iso(time.time() - 60)  # it ran out
        self.assertEqual(self.service.guest("200")["effective_status"], "expired")
        self.assertEqual(self.plugin.existing_access("100", "alex"), sid)  # extending updates it
        self.service.approve(
            "100",
            self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")["proposal_id"],
        )
        self.assertEqual(
            self.plugin.existing_access("100", "Alex"), sid
        )  # retain for reauthorization
        self.assertEqual(
            self.plugin.existing_access("100", "Alex", ended=True), sid
        )  # but activity is readable
        with self.service.ledger.transaction() as db:
            other = dict(db["shares"][sid], id="sh_other", guest_id="201", status="active")
            other["scope"] = dict(other["scope"], expires_at=iso(time.time() + 600))
            db["shares"]["sh_other"] = other
            db["shares"][sid]["status"] = "active"
            db["shares"][sid]["scope"]["expires_at"] = iso(time.time() + 600)
        with self.assertRaises(GuestsError) as unclear:
            self.plugin.existing_access("100", "Alex")
        self.assertIn("More than one person is called Alex", str(unclear.exception))

    async def test_a_shared_name_is_reported_even_when_only_one_of_them_is_active(self):
        sid = await self.activate()
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["scope"]["expires_at"] = iso(time.time() - 60)  # this Alex has lapsed
            other = dict(db["shares"][sid], id="sh_other", guest_id="201", status="active")
            other["scope"] = dict(other["scope"], expires_at=iso(time.time() + 600))
            db["shares"]["sh_other"] = other  # another Alex is active
        with self.assertRaises(GuestsError) as unclear:
            self.plugin.existing_access("100", "Alex")
        self.assertIn("Use their Telegram ID", str(unclear.exception))
        self.assertEqual(self.plugin.existing_access("100", "200"), sid)  # by ID it is exact
        self.assertEqual(self.plugin.existing_access("100", "201"), "sh_other")

    async def test_saved_state_that_cannot_be_read_as_ours_keeps_the_group_gate_closed(self):
        state = self.plugin.ctx.state
        original = state.get("access", None)
        for saved, expected in (
            (None, False),
            ("garbage", True),
            ([1, 2], True),
            ({"version": 99}, True),
            ({"version": 1}, True),
            ({"version": 1, "setup": {}}, False),
            ({"version": 1, "setup": {"owner_authority": {"x": 1}}}, True),
            ({"version": 1, "setup": {"trust_generation": 1}}, True),  # after a recovery
        ):
            with patch.object(
                type(state), "get", lambda self, key, default=None, saved=saved: saved
            ):
                self.assertEqual(self.plugin.configured_before(), expected, saved)
        self.assertEqual(state.get("access", None), original)

    async def test_naming_a_permission_as_none_is_not_undone(self):
        sid = await self.activate()
        kept = self.plugin.keep_current("100", sid, {"until_ended": True})
        self.assertEqual(kept["lights"], ["light.porch"])  # nothing named: kept
        named = self.plugin.keep_current("100", sid, {"lights": [], "duration_seconds": 3600})
        self.assertEqual(
            named, {"lights": [], "duration_seconds": 3600}
        )  # named as none: not copied back

    async def test_a_change_that_names_no_permissions_keeps_the_current_ones(self):
        sid = await self.activate()
        kept = self.plugin.keep_current("100", sid, {"until_ended": True})
        self.assertEqual(
            (kept["lights"], kept["research"], kept["until_ended"]), (["light.porch"], False, True)
        )
        self.assertNotIn("expires_at", kept)
        same_time = self.plugin.keep_current("100", sid, {})
        self.assertEqual(same_time["expires_at"], self.service.guest("200")["scope"]["expires_at"])
        named = {"research": True, "lights": [], "duration_seconds": 60}
        self.assertEqual(
            self.plugin.keep_current("100", sid, named), named
        )  # stated permissions win
        review = self.service.prepare("100", "Alex", kept, share_id=sid)
        self.assertIn("No end date", review["text"])
        self.assertIn("Porch light", review["text"])

    async def test_a_time_only_change_keeps_grouped_rules_and_chat_only_access(self):
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        sid = await self.activate()
        grouped = {
            "tools": [
                {"name": "web_search", "constraints": {"query": {"equals": "weather"}}},
                {"name": "web_search", "constraints": {"query": {"equals": "news"}}},
            ],
            "duration_seconds": 3600,
        }
        await self.plugin.proposal_card(self.service.prepare("100", "Alex", grouped, share_id=sid))
        await self.click(100, self.press("Approve"))
        self.assertEqual(len(self.service.guest("200")["scope"]["tools"][0]["alternatives"]), 2)
        kept = self.plugin.keep_current("100", sid, {"until_ended": True})
        self.assertEqual(
            sorted(e["constraints"]["query"]["equals"] for e in kept["tools"]), ["news", "weather"]
        )
        self.assertIn(
            "No end date", self.service.prepare("100", "Alex", kept, share_id=sid)["text"]
        )
        chat = {"chat_only": True, "duration_seconds": 3600}
        await self.plugin.proposal_card(self.service.prepare("100", "Alex", chat, share_id=sid))
        await self.click(100, self.press("Approve"))
        self.assertTrue(self.service.guest("200")["scope"].get("chat_only"))
        longer = self.plugin.keep_current("100", sid, {"duration_seconds": 7200})
        current_memory = bool(self.service.guest("200")["scope"].get("guest_memory"))
        self.assertEqual(
            longer,
            {"chat_only": True, "duration_seconds": 7200, "guest_memory": current_memory},
        )
        self.service.prepare("100", "Alex", longer, share_id=sid)  # accepted as it stands
        switch = {"chat_only": True, "duration_seconds": 60}
        self.assertEqual(self.plugin.keep_current("100", sid, dict(switch)), switch)

    async def test_data_retention_is_discoverable_confirmed_and_owner_only(self):
        await self.activate()
        await self.ready_owner()  # setup ends with the never-share list and guest data
        self.assertIn("kept until you delete them", self.bot.sent[-1][1])
        await self.click(100, self.press("Guest data"))
        self.assertIn("Keep until you delete it", self.bot.sent[-1][1])
        await self.click(100, self.press("Keep 7 days"))
        self.assertIsNone(self.service.data_settings("100")["retention_seconds"])
        approve = self.press("Approve")
        await self.click(200, approve)
        self.assertIsNone(self.service.data_settings("100")["retention_seconds"])
        await self.click(100, approve)
        self.assertEqual(self.service.data_settings("100")["retention_seconds"], 604800)
        await self.click(200, "hs:D:settings")
        self.assertIn("Only the owner", self.bot.sent[-1][1])
        self.assertEqual(self.errors, [])

    async def test_delete_data_review_is_irreversible_owner_only_and_cancelable(self):
        sid = await self.activate()
        await self.click(100, "hs:v:" + sid)
        await self.click(100, self.press("End and delete data"))
        self.assertIn("cannot be undone", self.bot.sent[-1][1])
        delete = self.press("Delete data")
        await self.click(200, delete)
        self.assertTrue(self.service._fresh(self.service.get_access("100", sid)))
        await self.click(100, self.press("Cancel"))
        self.assertTrue(self.service._fresh(self.service.get_access("100", sid)))
        await self.click(100, "hs:z:" + sid)
        with patch.object(self.service, "maintenance", side_effect=OSError("storage unavailable")):
            await self.click(100, self.press("Delete data"))
        self.assertTrue(
            any(who == 100 and "deletion is pending" in text for who, text, _ in self.bot.sent)
        )
        self.assertEqual(self.service.get_access("100", sid)["effective_status"], "revoked")
        self.assertIsNotNone(self.service.get_access("100", sid)["data_deletion_requested_at"])
        self.assertEqual(self.errors, [])

    async def test_the_button_journey_changes_access_a_person_already_has(self):
        self.plugin.shareable = lambda: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        sid = await self.activate()
        await self.message(100, "/guests_new Alex")
        await self.click(100, self.press("Search the web"))
        await self.click(100, self.press("Next"))
        await self.click(100, self.press("1 day"))
        with self.service.ledger.transaction() as db:
            pending = [p for p in db["proposals"].values() if p["status"] == "pending"]
        self.assertEqual([p["share_id"] for p in pending], [sid])  # a change, not a second grant
        await self.click(100, self.press("Approve"))
        self.assertEqual(len(self.service.list_access("100")), 1)
        self.assertEqual(
            [e["name"] for e in self.service.guest("200")["scope"]["tools"]], ["web_search"]
        )
        self.assertEqual(self.errors, [])

    async def test_a_never_share_change_in_words_waits_for_the_owners_confirmation(self):
        self.plugin.shareable = lambda all_tools=False: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        answer = await self.never_in_words(
            confirm=False, action="add", kind="word", value="bitcoin"
        )
        self.assertEqual(answer["result"]["status"], "awaiting_owner_confirmation")
        self.assertIn("Never let guests use “bitcoin” in any tool?", self.bot.sent[-1][1])
        self.assertFalse(self.service.never())  # nothing until Confirm
        confirm = self.press("Confirm")
        await self.click(200, confirm)  # not the owner
        self.assertFalse(self.service.never())
        await self.click(100, self.press("Cancel"))
        self.assertIn("Nothing changed", self.bot.sent[-1][1])
        await self.click(100, confirm)  # a decided card cannot be pressed again
        self.assertFalse(self.service.never())
        await self.never_in_words(action="add", kind="word", value="bitcoin")
        self.assertEqual(self.service.never().values, ["bitcoin"])
        self.assertEqual(self.effects, [])

    async def test_removal_review_expires_when_a_matching_restriction_is_added(self):
        self.plugin.shareable = lambda all_tools=False: {"ha_get_state": "Read light state"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        sid = await self.activate()
        self.service.never_set("100", "values", "porch", True)
        await self.never_in_words(confirm=False, action="remove", kind="word", value="porch")
        confirm = self.press("Confirm")
        self.service.never_set(
            "100", "scoped", "porch", True, tool="ha_get_state", field="entity_id"
        )
        await self.click(100, confirm)
        self.assertIn("list changed", self.bot.sent[-1][1])
        self.assertEqual(self.service.never().values, ["porch"])
        self.assertEqual(len(self.service.never().scoped), 1)
        with self.assertRaisesRegex(GuestsError, "does not share"):
            self.service.call("200", sid, "ha_get_state", {"entity_id": "light.porch"})
        self.assertEqual(self.effects, [])

    async def test_removal_at_one_input_preserves_other_rules_for_that_word(self):
        self.plugin.shareable = lambda all_tools=False: {"ha_get_state": "Read light state"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        self.service.never_set("100", "values", "porch", True)
        self.service.never_set(
            "100", "scoped", "porch", True, tool="ha_get_state", field="entity_id"
        )
        removal = {"action": "remove", "kind": "word", "value": "porch", "tool": "ha_get_state"}
        result = await self.never_in_words(confirm=False, **removal)
        self.assertFalse(result["ok"])
        await self.never_in_words(confirm=False, **removal, input="entity_id")
        self.assertIn("ha_get_state → entity_id", self.bot.sent[-1][1])
        await self.click(100, self.press("Confirm"))
        self.assertEqual(self.service.never().scoped, [])
        self.assertEqual(self.service.never().values, ["porch"])

    async def test_starting_a_name_prompt_replaces_the_never_share_word_prompt(self):
        self.plugin.shareable = lambda all_tools=False: {"web_search": "Search the web"}
        await self.ready_owner()
        await self.click(100, self.press("Never-share list"))
        await self.click(100, self.press("＋ Block a word everywhere"))
        await self.message(100, "/guests")
        await self.click(100, self.press("Add a guest"))
        await self.message(100, "Alex")
        self.assertIn("Share with Alex", self.bot.sent[-1][1])
        self.assertFalse(self.service.never())

    async def test_chat_only_never_share_panel_can_render_many_words(self):
        self.plugin.shareable = lambda all_tools=False: {}
        await self.ready_owner()
        for i in range(13):
            self.service.never_set("100", "values", f"keyword{i}", True)
        await self.click(100, self.press("Never-share list"))
        self.assertIn("keyword12", self.bot.sent[-1][1])
        self.assertIn("＋ Block a word everywhere", self.buttons(self.bot.sent[-1]))
        self.assertEqual(self.errors, [])

    async def test_add_a_guest_and_check_sharing_are_buttons_on_the_hub(self):
        self.plugin.shareable = lambda all_tools=False: {"web_search": "Search the web"}
        self.plugin.resolve_tool = GUEST_SCHEMAS.get
        await self.ready_owner()
        await self.message(100, "/guests")
        self.assertEqual(
            self.buttons(self.bot.sent[-1]), ["Add a guest", "Check sharing", "Activity"]
        )
        await self.click(100, self.press("Add a guest"))
        self.assertIn("Send their name", self.bot.sent[-1][1])
        await self.message(100, "Alex")
        self.assertIn("Share with Alex", self.bot.sent[-1][1])
        self.assertEqual(self.fallback, [])  # the name went to the picker, not to the model
        await self.click(100, self.press("Check sharing"))
        self.assertIn("Sharing checked", self.bot.sent[-1][1])
        await self.click(200, "hs:h:add")  # not the owner
        self.assertIn("Only the owner", self.bot.sent[-1][1])
        self.assertEqual(self.errors, [])

    async def test_a_new_guests_review_says_how_long_their_data_is_kept(self):
        await self.ready_owner()
        await self.plugin.proposal_card(self.service.prepare("100", "Alex", self.scope()))
        self.assertIn(
            "Their conversations and memories are kept until you delete them", self.bot.sent[-1][1]
        )
        self.assertIn("/guests_setup", self.bot.sent[-1][1])

    def reviewed_scope(self):
        """The scope of the newest review card's proposal."""
        pid = self.press("Approve").split(":", 2)[2]
        return self.service.ledger.snapshot()["proposals"][pid]["scope"]

    async def test_owner_extends_access_from_the_current_end_with_buttons(self):
        sid = await self.activate()
        before = self.service.get_access("100", sid)
        ends = datetime.fromisoformat(before["scope"]["expires_at"]).timestamp()
        await self.click(100, "hs:v:" + sid)
        self.assertIn("Extend", self.buttons(self.bot.sent[-1]))
        await self.click(100, self.press("Extend"))
        self.assertEqual(
            self.buttons(self.bot.sent[-1]),
            ["1 more day", "1 more week", "No end date"],
        )
        await self.click(200, self.press("1 more week"))  # not the owner
        self.assertIn("Only the owner", self.bot.sent[-1][1])
        await self.click(100, self.press("1 more week"))
        scope = self.reviewed_scope()
        self.assertAlmostEqual(
            datetime.fromisoformat(scope["expires_at"]).timestamp(), ends + 604800, delta=2
        )
        self.assertEqual(scope["lights"], before["scope"]["lights"])  # permissions unchanged
        self.assertEqual(
            self.service.get_access("100", sid)["scope"]["expires_at"],
            before["scope"]["expires_at"],
        )  # nothing changes before Approve
        await self.click(100, self.press("Approve"))
        after = self.service.get_access("100", sid)["scope"]
        self.assertAlmostEqual(
            datetime.fromisoformat(after["expires_at"]).timestamp(), ends + 604800, delta=2
        )
        self.assertEqual(self.effects, [])
        self.assertEqual(self.errors, [])

    async def test_no_end_date_from_extend_removes_the_extend_button(self):
        sid = await self.activate()
        await self.click(100, "hs:X:" + sid)
        await self.click(100, self.press("No end date"))
        self.assertTrue(self.reviewed_scope().get("until_ended"))
        await self.click(100, self.press("Approve"))
        self.assertTrue(self.service.get_access("100", sid)["scope"].get("until_ended"))
        await self.click(100, "hs:v:" + sid)
        self.assertNotIn("Extend", self.buttons(self.bot.sent[-1]))
        await self.click(100, "hs:X:" + sid)  # an old Extend button
        self.assertIn("no end date already", self.bot.sent[-1][1])

    async def test_extending_access_that_has_ended_counts_from_now(self):
        sid = await self.activate()
        later = time.time() + 7200  # the hour of access has run out
        self.service.clock = lambda: later
        await self.click(100, "hs:X:" + sid)
        self.assertIn("30 more days", self.buttons(self.bot.sent[-1]))
        await self.click(100, self.press("1 more day"))
        self.assertAlmostEqual(
            datetime.fromisoformat(self.reviewed_scope()["expires_at"]).timestamp(),
            later + 86400,
            delta=2,
        )
        self.assertEqual(self.effects, [])

    async def test_extend_menu_cannot_reopen_access_that_was_ended_after_it_was_shown(self):
        sid = await self.activate()
        await self.click(100, "hs:X:" + sid)
        old_option = self.press("1 more day")
        review = self.service.prepare("100", "Alex", None, share_id=sid, action="revoke")
        self.service.approve("100", review["proposal_id"])
        await self.click(100, old_option)
        self.assertIn("Access changed", self.bot.sent[-1][1])
        await self.click(100, "hs:X:" + sid)
        await self.click(100, self.press("1 more day"))
        self.assertAlmostEqual(
            datetime.fromisoformat(self.reviewed_scope()["expires_at"]).timestamp(),
            self.service.clock() + 86400,
            delta=2,
        )
        self.assertEqual(self.service.get_access("100", sid)["status"], "revoked")

    async def test_owner_is_told_once_to_start_a_new_chat_after_the_tools_change(self):
        self.plugin.owner_tool_specs = [{"name": "guests_prepare_access", "description": "v1"}]
        await self.ready_owner()  # setup records the version it set up
        sent = len(self.bot.sent)
        await self.plugin.tools_changed_notice()
        self.assertEqual(len(self.bot.sent), sent)  # nothing to say right after setup
        self.plugin.owner_tool_specs = [{"name": "guests_prepare_access", "description": "v2"}]
        await self.plugin.tools_changed_notice()
        await self.plugin.tools_changed_notice()
        self.assertEqual(len(self.bot.sent), sent + 1)
        self.assertEqual(self.bot.sent[-1][0], 100)
        self.assertIn("/new", self.bot.sent[-1][1])

    async def test_a_setup_from_before_the_reminder_is_told_and_a_failed_send_is_retried(self):
        await self.ready_owner()
        self.plugin.ctx.state.set("owner_tools_seen", None)  # set up by an older version
        self.plugin.owner_tool_specs = [{"name": "guests_prepare_access", "description": "v2"}]
        object.__setattr__(self.bot, "fail_once", True)
        with self.assertRaises(Exception):
            await self.plugin.tools_changed_notice()
        await self.plugin.tools_changed_notice()
        self.assertIn("/new", self.bot.sent[-1][1])

    async def test_the_claim_card_uses_the_owners_name_and_shows_the_account(self):
        await self.activate()
        card = next(
            t for who, t, _ in self.bot.sent if who == 100 and "opened your invitation" in t
        )
        self.assertTrue(card.startswith("Someone opened your invitation for Alex."), card)
        self.assertIn("Telegram account: ", card)
        self.assertIn("Accept if this is Alex.", card)

    async def test_owner_adds_time_from_the_current_end_in_chat(self):
        await self.ready_owner()
        sid = await self.activate()
        end = timestamp(self.service.get_access("100", sid)["scope"]["expires_at"])
        with patch("hermes_guests.plugin.actor", return_value="100"):
            result = json.loads(
                await self.plugin.tool(
                    "guests_prepare_access", {"person": "Alex", "add_seconds": 3600}
                )
            )
        self.assertEqual(result["result"]["status"], "awaiting_owner_confirmation", result)
        pending = [
            p
            for p in self.service.ledger.snapshot()["proposals"].values()
            if p["status"] == "pending"
        ]
        self.assertEqual(timestamp(pending[-1]["scope"]["expires_at"]), end + 3600)
        self.assertTrue(
            any("Give Alex 1 hour more?" in t for who, t, _ in self.bot.sent if who == 100),
            [t for _, t, _ in self.bot.sent[-3:]],
        )

    async def test_asking_for_more_while_access_is_on_adds_to_the_end(self):
        await self.activate()
        await self.message(200, "/renew")
        self.assertIn("added to your current end time", self.bot.sent[-1][1])

    async def test_guest_access_card_is_plain_with_details_a_tap_away(self):
        await self.activate()
        await self.message(200, "/access")
        card = self.bot.sent[-1][1]
        self.assertTrue(card.startswith("Your access"), card)
        self.assertIn("Porch light", card)
        self.assertNotIn("Entity id", card)
        self.assertNotIn("Status:", card)
        await self.click(200, self.press("See permissions"))
        self.assertIn("Access for Alex", self.bot.sent[-1][1])

    async def test_a_length_out_of_range_can_be_corrected_in_the_same_prompt(self):
        await self.activate()
        await self.message(200, "/renew")
        await self.message(200, "31 days")
        self.assertIn("between a minute and 30 days", self.bot.sent[-1][1])
        await self.message(200, "6 hours")
        self.assertIn("6 hours more is with the owner", self.bot.sent[-1][1])

    async def test_cancel_closes_both_open_prompts(self):
        await self.activate()
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        await self.message(200, "/renew")
        await self.message(200, "/cancel")
        runs = len(self.runs)
        await self.message(200, "6 hours")
        self.assertEqual(len(self.runs), runs + 1)  # an ordinary message, not a request
        self.assertFalse(
            any(p.get("renewal") for p in self.service.ledger.snapshot()["proposals"].values())
        )

    async def test_the_newest_prompt_gets_the_answer(self):
        await self.activate()
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        await self.message(200, "/renew")
        await self.message(200, "6 hours")
        self.assertIn("6 hours more is with the owner", self.bot.sent[-1][1])
        self.assertEqual(self.request_cards(), [])  # not sent as a request for more access

    async def test_asking_for_more_access_after_renew_answers_that_question(self):
        await self.activate()
        await self.message(200, "/renew")
        await self.message(200, "/access")
        await self.click(200, self.press("Request more access"))
        await self.message(200, "the kitchen light too")
        await self.message(200, "6 hours")  # an ordinary message now, not a length
        self.assertFalse(
            any(p.get("renewal") for p in self.service.ledger.snapshot()["proposals"].values())
        )

    async def test_more_time_offers_only_lengths_that_fit(self):
        sid = await self.activate()
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["duration_seconds"] = 20 * 86400
            db["shares"][sid]["scope"]["expires_at"] = iso(time.time() + 20 * 86400)
        await self.message(200, "/renew")
        labels = [b.text for row in self.bot.sent[-1][2].inline_keyboard for b in row]
        self.assertNotIn("Same again (20 days)", labels)
        self.assertIn("1 day", labels)

    async def test_a_short_typed_length_is_still_possible_near_the_limit(self):
        sid = await self.activate()
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["duration_seconds"] = 30 * 86400
            db["shares"][sid]["scope"]["expires_at"] = iso(time.time() + 30 * 86400 - 1800)
        await self.message(200, "/renew")
        self.assertIn("How much more time", self.bot.sent[-1][1])
        self.assertIsNone(self.bot.sent[-1][2])  # no preset fits; a typed length still can

    async def test_adding_time_and_an_end_time_together_is_refused(self):
        await self.ready_owner()
        sid = await self.activate()
        with patch("hermes_guests.plugin.actor", return_value="100"):
            result = json.loads(
                await self.plugin.tool(
                    "guests_prepare_access",
                    {
                        "person": "Alex",
                        "share_id": sid,
                        "add_seconds": 3600,
                        "scope": {"expires_at": iso(time.time() + 7200)},
                    },
                )
            )
        self.assertFalse(result["ok"])
        self.assertIn("not both", result["error"])

    async def test_a_request_answered_with_setup_reaches_the_assistant_after_enable(self):
        self.plugin.owner_tools = lambda **_: {"ha_get_state": "Read state"}
        await self.message(100, "Let Adrian control the guest room lights for a day.")
        self.plugin.owner_tools = lambda **_: {
            "ha_get_state": "Read state",
            **{name: name for name in MANAGEMENT_TOOLS},
        }
        await self.click(100, self.press("Enable sharing"))
        with patch("hermes_guests.plugin.actor", return_value="100"):
            context = self.plugin.owner_turn_context(platform="telegram")["context"]
            again = self.plugin.owner_turn_context(platform="telegram")["context"]
        self.assertIn("Let Adrian control the guest room lights for a day.", context)
        self.assertNotIn("Adrian", again)  # handed over once

    async def test_a_worded_request_closes_the_time_prompt(self):
        await self.activate()
        await self.message(200, "/renew")
        await self.message(200, "Request access to the kitchen speaker too")
        await self.message(200, "6 hours")  # an ordinary message, not a length
        self.assertFalse(
            any(p.get("renewal") for p in self.service.ledger.snapshot()["proposals"].values())
        )

    async def test_a_late_length_is_not_sent_and_says_so(self):
        await self.activate()
        await self.message(200, "/renew")
        share, _ = self.plugin._asking_time["200"]
        self.plugin._asking_time["200"] = (share, time.time() - 1)  # ten minutes passed
        runs = len(self.runs)
        await self.message(200, "6 hours")
        self.assertIn("took longer than ten minutes", self.bot.sent[-1][1])
        self.assertEqual(len(self.runs), runs)

    async def test_requests_made_together_wait_on_one_setup_card(self):
        with patch("hermes_guests.plugin.actor", return_value="100"):
            first = asyncio.create_task(self.plugin.tool("guests_discover", {}))
            second = asyncio.create_task(self.plugin.tool("guests_discover", {}))
            for _ in range(200):
                if any("Set up guest sharing?" in t for _, t, _ in self.bot.sent):
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.1)
            cards = [t for _, t, _ in self.bot.sent if "Set up guest sharing?" in t]
            self.assertEqual(len(cards), 1)
            await self.click(100, self.press("Enable sharing"))
            results = await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertTrue(all(json.loads(r).get("ok", True) is not False for r in results), results)

    async def test_a_guest_sets_their_time_zone_with_a_command(self):
        sid = await self.activate()
        await self.message(200, "/timezone Vancouver")
        self.assertIn("Vancouver time", self.bot.sent[-1][1])
        self.assertEqual(
            self.service.zone_of(self.service.get_access("100", sid)), "America/Vancouver"
        )
        await self.message(200, "/timezone Narnia")
        self.assertIn("your city or time zone", self.bot.sent[-1][1])

    async def test_adding_time_never_switches_on_private_memory(self):
        await self.ready_owner()
        sid = await self.activate()
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["scope"]["guest_memory"] = False
        with patch("hermes_guests.plugin.actor", return_value="100"):
            await self.plugin.tool("guests_prepare_access", {"person": "Alex", "add_seconds": 3600})
        pending = [
            p
            for p in self.service.ledger.snapshot()["proposals"].values()
            if p["status"] == "pending"
        ]
        self.assertFalse(pending[-1]["scope"].get("guest_memory"))

    async def test_a_waiting_request_carries_on_even_if_the_ready_message_fails(self):
        with patch("hermes_guests.plugin.actor", return_value="100"):
            asked = asyncio.create_task(self.plugin.tool("guests_discover", {}))
            for _ in range(200):
                if any("Set up guest sharing?" in t for _, t, _ in self.bot.sent):
                    break
                await asyncio.sleep(0.01)

            async def fail(*args, **kwargs):
                raise RuntimeError("Telegram is down")

            self.plugin._ready_message = fail
            await self.click(100, self.press("Enable sharing"))
            result = await asyncio.wait_for(asked, 5)
        self.assertTrue(self.service.owner_ready())
        self.assertNotIn("isn't set up", result)

    async def test_renew_works_after_access_runs_out(self):
        sid = await self.activate()
        with self.service.ledger.transaction() as db:
            db["shares"][sid]["scope"]["expires_at"] = iso(time.time() - 60)
        await self.message(200, "/renew")
        self.assertIn("How much more time", self.bot.sent[-1][1])
        self.assertIn("Time starts when the owner approves", self.bot.sent[-1][1])

    async def test_activity_ranges_are_checked(self):
        await self.ready_owner()
        with patch("hermes_guests.plugin.actor", return_value="100"):
            for bad in (0, -5, "yesterday", True):
                result = json.loads(
                    await self.plugin.tool("guests_activity", {"scope": "all", "since_hours": bad})
                )
                self.assertFalse(result["ok"], bad)

    async def test_notices_still_go_out_when_maintenance_fails(self):
        await self.ready_owner()
        calls = []

        async def notices():
            calls.append(1)
            raise asyncio.CancelledError  # stop the loop after one pass

        self.plugin.configured_before = lambda: True
        self.service.maintenance = lambda *a: (_ for _ in ()).throw(OSError("damaged profile"))
        self.plugin.expiry_notices = notices
        with self.assertRaises(asyncio.CancelledError):
            await self.plugin.housekeeping()
        self.assertEqual(calls, [1])

    async def test_an_unattended_run_does_not_wait_for_setup(self):
        self.plugin.ctx.current_cron_execution = lambda: object()
        with patch("hermes_guests.plugin.actor", return_value="100"):
            result = json.loads(
                await asyncio.wait_for(self.plugin.tool("guests_prepare_access", {}), 2)
            )
        self.assertFalse(result["ok"])
        self.assertIn("only be set up by the owner", result["error"])
        self.assertFalse(any("Set up guest sharing?" in t for _, t, _ in self.bot.sent))

    async def test_each_shared_tool_needs_a_plain_explanation(self):
        await self.ready_owner()
        with patch("hermes_guests.plugin.actor", return_value="100"):
            result = json.loads(
                await self.plugin.tool(
                    "guests_prepare_access",
                    {
                        "person": "Alex",
                        "scope": {
                            "tools": [
                                {
                                    "name": "ha_get_state",
                                    "constraints": {"entity_id": {"equals": "light.porch"}},
                                }
                            ],
                            "duration_seconds": 3600,
                        },
                    },
                )
            )
        self.assertFalse(result["ok"])
        self.assertIn("presentation.operation for ha_get_state", result["error"])
        self.assertEqual(self.service.ledger.snapshot()["proposals"], {})

    async def test_activity_for_everyone_is_a_button_on_the_hub(self):
        await self.activate()
        await self.message(200, "Is the porch light on?")
        await self.message(100, "/guests")
        await self.click(100, self.press("Activity"))
        shown = self.bot.sent[-1][1]
        self.assertIn("Activity for everyone", shown)
        self.assertIn("Alex", shown)
        await self.click(200, "hs:h:activity")  # not the owner
        self.assertIn("Only the owner", self.bot.sent[-1][1])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
