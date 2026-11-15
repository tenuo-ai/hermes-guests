"""Manual native-model journey against synthetic tools and a recording Telegram bot.

Requires a disposable HERMES_HOME and an API-key provider configured in Hermes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.journey_cases import CASES, RESOURCES, SCHEMAS  # noqa: E402
from tests.test_telegram_journeys import RecordingBot  # noqa: E402


async def evaluate(case_name, output):
    from hermes_guests.inference import API_KEY_ROUTES

    provider = os.environ.get("HERMES_GUESTS_EVAL_PROVIDER", "openai")
    model = os.environ.get("HERMES_GUESTS_EVAL_MODEL")
    if provider not in API_KEY_ROUTES or not model:
        raise SystemExit("Set HERMES_GUESTS_EVAL_MODEL and choose a supported API-key provider.")
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    os.environ["TELEGRAM_ALLOWED_USERS"] = "100"
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["hermes-guests", "fixture-services"],
                    "entries": {"hermes-guests": {"settings": {"owner_id": "100"}}},
                },
                "platform_toolsets": {"telegram": ["hermes_guests", "fixture_services"]},
                "model": {"default": model, "provider": provider},
            }
        )
    )
    plugin_home = home / "plugins" / "fixture-services"
    plugin_home.mkdir(parents=True)
    (plugin_home / "plugin.yaml").write_text(
        "name: fixture-services\nversion: 0.1.0\nkind: standalone\ndescription: Synthetic services\n"
    )
    effects = home / "effects.jsonl"
    (plugin_home / "__init__.py").write_text(
        "import json\nfrom pathlib import Path\nSCHEMAS="
        + repr(SCHEMAS)
        + "\nRESOURCES="
        + repr(RESOURCES)
        + "\ndef call(tool,args):\n"
        + "    if tool == 'fixture_resources': return json.dumps(RESOURCES)\n"
        + "    with Path("
        + repr(str(effects))
        + ").open('a') as f: f.write(json.dumps({'tool':tool,'args':args})+'\\n')\n"
        + "    return json.dumps({'success':True,'results':['Synthetic result'],'state':'completed'})\n"
        + "def register(ctx):\n    for name,spec in SCHEMAS.items():\n        ctx.register_tool(name,'fixture_services',spec,lambda args,_name=name,**kw: call(_name,args))\n"
    )
    from hermes_guests.plugin import native_runtime

    runtime = native_runtime()
    from hermes_cli.plugins import get_plugin_manager
    from telegram import CallbackQuery, Chat, Message, Update, User
    from telegram.ext import Application

    manager = get_plugin_manager()
    manager.discover_and_load()
    factory = next(
        f for f, name in manager.get_telegram_handler_factories() if name == "hermes-guests"
    )
    plugin = factory.__self__
    bot = RecordingBot()
    app = Application.builder().bot(bot).build()
    await app.initialize()
    factory(app, None)

    async def message(who, text):
        m = Message(
            1000 + len(bot.sent),
            datetime.now(timezone.utc),
            Chat(who, "private"),
            from_user=User(who, "Owner" if who == 100 else "Alex", False),
            text=text,
        )
        m.set_bot(bot)
        await app.process_update(Update(1, message=m))

    async def click(who, data):
        ids = [
            i + 1
            for i, (_, _, markup) in enumerate(bot.sent)
            if markup
            and any(b.callback_data == data for row in markup.inline_keyboard for b in row)
        ]
        assert ids, "The expected recording-bot control was not delivered."
        m = Message(ids[-1], datetime.now(timezone.utc), Chat(who, "private"), text="Review")
        m.set_bot(bot)
        query = CallbackQuery("fixture", User(who, "Owner", False), "fixture", message=m, data=data)
        query.set_bot(bot)
        await app.process_update(Update(2, callback_query=query))

    report = {
        "case": case_name,
        "model": runtime["model"],
        "transport": "recording",
        "backend": "synthetic",
    }
    try:
        await message(100, "/guests_setup")
        markup = next(markup for _, _, markup in reversed(bot.sent) if markup)
        await click(100, markup.inline_keyboard[0][0].callback_data)
        assert plugin.service.owner_ready(), "Owner setup did not complete."
        from gateway.session_context import clear_session_vars, set_session_vars
        from run_agent import AIAgent

        tokens = set_session_vars(
            platform="telegram",
            chat_id="100",
            user_id="100",
            chat_type="dm",
            session_id="eval-owner",
        )
        try:
            agent = AIAgent(
                **runtime,
                session_id="eval-owner",
                enabled_toolsets=["hermes_guests", "fixture_services"],
                disabled_toolsets=[],
                max_iterations=10,
                run_budget_seconds=180,
                skip_context_files=True,
                load_soul_identity=False,
                skip_memory=True,
                skip_background_review=True,
                quiet_mode=True,
                platform="telegram",
                user_id="100",
                chat_id="100",
                chat_type="dm",
                cwd=str(home),
                save_trajectories=False,
            )
            owner_result = await asyncio.to_thread(
                agent.run_conversation,
                CASES[case_name]["request"],
                system_message=(ROOT / "hermes_guests/skills/guest-access/SKILL.md").read_text(),
            )
        finally:
            clear_session_vars(tokens)
        assert (
            owner_result.get("completed")
            and not owner_result.get("failed")
            and not owner_result.get("error")
        ), "The owner model turn did not complete successfully."
        report["owner_reply"] = owner_result.get("final_response", "")
        proposals = [
            p
            for p in plugin.service.ledger.snapshot()["proposals"].values()
            if p["status"] == "pending"
        ]
        assert len(proposals) == 1, "Expected one pending owner review."
        proposal = proposals[0]
        report["scope"] = proposal["scope"]
        report["review"] = next(
            text
            for _, text, markup in reversed(bot.sent)
            if markup
            and any(
                b.callback_data == "hs:p:" + proposal["id"]
                for row in markup.inline_keyboard
                for b in row
            )
        )
        assert not proposal["scope"]["lights"] and not proposal["scope"]["research"], (
            "Unexpected preset authority was proposed."
        )
        if case_name == "chat":
            assert proposal["scope"].get("chat_only") and not proposal["scope"]["tools"]
        else:
            assert {e["name"] for e in proposal["scope"]["tools"]} == set(
                CASES[case_name].get("tools", [CASES[case_name]["tool"]])
            )
            assert all(e.get("presentation") for e in proposal["scope"]["tools"]), (
                "The model omitted readable permission descriptions."
            )
        if "invitation_seconds" in CASES[case_name]:
            assert proposal["invitation_seconds"] == CASES[case_name]["invitation_seconds"]
            report["invitation_seconds"] = proposal["invitation_seconds"]
        await click(100, "hs:p:" + proposal["id"])
        link = next(
            text for who, text, _ in reversed(bot.sent) if who == 100 and "?start=guests_" in text
        )
        token = link.split("?start=guests_", 1)[1]
        await message(200, "/start guests_" + token)
        from hermes_guests.core import token_hash
        from hermes_guests.model import GuestsError

        await click(100, "hs:a:" + token_hash(token)[:24])
        record = plugin.service.guest("200")
        sid = record["id"]
        start = len(effects.read_text().splitlines()) if effects.exists() else 0
        if case_name != "chat":

            def arguments(tool, args):
                entry = next(e for e in record["scope"]["tools"] if e["name"] == tool)
                return {"request": [args]} if entry.get("alternatives") else args

            plugin.service.call(
                "200",
                sid,
                CASES[case_name]["tool"],
                arguments(CASES[case_name]["tool"], CASES[case_name]["args"]),
            )
            for tool, args in CASES[case_name].get("extra_allowed", []):
                plugin.service.call("200", sid, tool, arguments(tool, args))
            count = len(effects.read_text().splitlines())
            negatives = [CASES[case_name]["bad"], *CASES[case_name].get("extra_bad", [])]
            denials = [(CASES[case_name]["tool"], bad) for bad in negatives]
            denials += CASES[case_name].get("extra_denied", [])
            for tool, bad in denials:
                try:
                    plugin.service.call("200", sid, tool, arguments(tool, bad))
                except GuestsError:
                    pass
                else:
                    raise AssertionError("An out-of-scope request was allowed.")
                assert len(effects.read_text().splitlines()) == count, (
                    "Denied request produced an effect."
                )
            report["denied_calls"] = len(denials)
        before_guest = len(effects.read_text().splitlines()) if effects.exists() else 0
        await message(200, CASES[case_name]["guest"])
        await asyncio.gather(*list(plugin._guest_tasks.values()))
        report["guest_reply"] = next(text for who, text, _ in reversed(bot.sent) if who == 200)
        after = len(effects.read_text().splitlines()) if effects.exists() else 0
        if case_name == "chat":
            assert after == start, "Chat-only guest produced a service effect."
            await message(
                200, "Please remember that I prefer brief answers and my project codename is Cedar."
            )
            await asyncio.gather(*list(plugin._guest_tasks.values()))
            guest_home = plugin.runner.ensure_profile(sid)
            saved = "\n".join(path.read_text() for path in (guest_home / "memories").glob("*.md"))
            assert "Cedar" in saved, "The model claimed a preference without saving native memory."
            previous = plugin.service.get_access("100", sid)
            await message(200, "/new")
            await message(200, "What is my project codename and preferred reply length?")
            await asyncio.gather(*list(plugin._guest_tasks.values()))
            reply = next(text for who, text, _ in reversed(bot.sent) if who == 200)
            assert "cedar" in reply.lower(), "The fresh conversation did not recall guest memory."
            current = plugin.service.get_access("100", sid)
            assert current["warrant_id"] == previous["warrant_id"]
            assert current["scope"]["expires_at"] == previous["scope"]["expires_at"]
            assert current["context_epoch"] == previous["context_epoch"] + 1
            assert (len(effects.read_text().splitlines()) if effects.exists() else 0) == start
            report["memory_after_new"] = True
            report["personalized_reply"] = reply
        else:
            assert after > before_guest, "Guest did not perform the allowed operation."
            guest_calls = [
                json.loads(line) for line in effects.read_text().splitlines()[before_guest:]
            ]
            assert set(CASES[case_name].get("guest_tools", [CASES[case_name]["tool"]])) <= {
                call["tool"] for call in guest_calls
            }, "The guest did not perform every requested operation."
        if CASES[case_name].get("denied_guest"):
            await message(200, CASES[case_name]["denied_guest"])
            await asyncio.gather(*list(plugin._guest_tasks.values()))
            report["boundary_reply"] = next(
                text for who, text, _ in reversed(bot.sent) if who == 200
            )
            assert len(effects.read_text().splitlines()) == after, (
                "The conversational boundary request produced an effect."
            )
            await message(200, CASES[case_name]["guest"])
            await asyncio.gather(*list(plugin._guest_tasks.values()))
            assert len(effects.read_text().splitlines()) > after, (
                "The guest failed to resume an allowed task."
            )
            report["resumed_after_boundary"] = True
            after = len(effects.read_text().splitlines())
            previous = plugin.service.get_access("100", sid)
            tokens = set_session_vars(
                platform="telegram",
                chat_id="100",
                user_id="100",
                chat_type="dm",
                session_id="eval-owner",
            )
            try:
                changed = await asyncio.to_thread(
                    agent.run_conversation,
                    "Also let Alex read the Work calendar. Keep the existing end time, exclude private events and keep the twenty-event request limit.",
                    system_message=(
                        ROOT / "hermes_guests/skills/guest-access/SKILL.md"
                    ).read_text(),
                    conversation_history=owner_result["messages"],
                )
            finally:
                clear_session_vars(tokens)
            assert (
                changed.get("completed") and not changed.get("failed") and not changed.get("error")
            ), "The permission-change turn failed."
            pending = [
                p
                for p in plugin.service.ledger.snapshot()["proposals"].values()
                if p["status"] == "pending"
            ]
            assert len(pending) == 1 and pending[0]["share_id"] == sid, (
                "The model did not update the existing guest."
            )
            assert pending[0]["scope"]["expires_at"] == previous["scope"]["expires_at"], (
                "The model changed the approved end time."
            )
            await click(100, "hs:p:" + pending[0]["id"])
            current = plugin.service.get_access("100", sid)
            assert (
                current["warrant_id"] != previous["warrant_id"]
                and previous["warrant_id"] in current["old_warrants"]
            )
            await message(200, "Show me five Work calendar events, excluding private events.")
            await asyncio.gather(*list(plugin._guest_tasks.values()))
            assert len(effects.read_text().splitlines()) > after, (
                "The expanded permission did not work."
            )
            report["expanded_review"] = (
                current["text"] if "text" in current else pending[0]["scope"]
            )
            report["changed_existing_access"] = True
            tokens = set_session_vars(
                platform="telegram",
                chat_id="100",
                user_id="100",
                chat_type="dm",
                session_id="eval-owner",
            )
            try:
                ended = await asyncio.to_thread(
                    agent.run_conversation,
                    "End Alex's access now.",
                    system_message=(
                        ROOT / "hermes_guests/skills/guest-access/SKILL.md"
                    ).read_text(),
                    conversation_history=changed["messages"],
                )
            finally:
                clear_session_vars(tokens)
            assert ended.get("completed") and not ended.get("failed") and not ended.get("error"), (
                "The end-access turn failed."
            )
            pending = [
                p
                for p in plugin.service.ledger.snapshot()["proposals"].values()
                if p["status"] == "pending"
            ]
            assert (
                len(pending) == 1
                and pending[0]["action"] == "revoke"
                and pending[0]["share_id"] == sid
            )
            await click(100, "hs:p:" + pending[0]["id"])
            after = len(effects.read_text().splitlines())
            await message(200, "Show me the Family calendar.")
            assert len(effects.read_text().splitlines()) == after
            assert plugin.service.get_access("100", sid)["status"] == "revoked"
            report["revoked_after_owner_request"] = True

        report["status"] = "passed"
        report["guest_effects"] = after - start
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({"case": case_name, "status": "passed", "guest_effects": after - start}))
    except Exception as exc:
        report["status"] = "failed"
        report["failure"] = type(exc).__name__ + ": " + str(exc)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        raise
    finally:
        plugin.close()
        await app.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(evaluate(args.case, args.output))
