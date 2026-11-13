"""Isolated native owner-to-guest flow; only invoked by the integration test."""

import asyncio
import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telegram import CallbackQuery, Chat, Message, Update, User
from telegram.ext import Application

from tests.test_telegram_journeys import RecordingBot


async def main():
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    several = os.environ.get("GUESTS_FIXTURE_SEVERAL_PROFILES") == "1"
    if several:
        # A host that serves more than one profile keeps each profile's settings in that
        # profile's own file and refuses a read that is not made on a profile's behalf.
        (home / ".env").write_text("TELEGRAM_ALLOWED_USERS=100\n")
    else:
        os.environ["TELEGRAM_ALLOWED_USERS"] = "100"
    ha_plugin = os.environ.get("GUESTS_FIXTURE_HOMEASSISTANT")
    saved_tools = os.environ.get("GUESTS_FIXTURE_SAVED_TOOLS") == "1"
    installed = (home / "plugins" / "hermes-guests").is_dir()
    state = {"phase": "owner", "step": 0, "guest_requests": [], "ha_reads": []}
    service_tool = "ha_get_state" if ha_plugin else "library_find"
    guest_args = (
        {"entity_id": "light.porch"}
        if ha_plugin
        else {
            "library": "Movies",
            "query": "comedy",
            "limit": 2,
        }
    )
    expires = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
    tools = [
        {
            "name": "library_find",
            "presentation": {
                "resource": "library",
                "labels": {"query": "Search terms", "limit": "Results per search"},
            },
            "constraints": {
                "library": {"equals": "Movies"},
                "query": {"any": True},
                "limit": {"range": [1, 5]},
            },
        }
    ]
    if ha_plugin:
        tools = [
            {
                "name": service_tool,
                "presentation": {
                    "operation": "Check whether the light is on",
                    "resource": "entity_id",
                    "resource_labels": {"light.porch": "Porch light"},
                },
                "constraints": {"entity_id": {"equals": "light.porch"}},
            }
        ]
    plan = [
        ("tool_search", {"queries": ["share prepare access"]}),
        ("tool_describe", {"names": ["guests_discover", "guests_prepare_access"]}),
        (
            "tool_call",
            {
                "calls": [
                    {
                        "name": "guests_discover",
                        "arguments": {"query": "home assistant" if ha_plugin else "library"},
                    }
                ]
            },
        ),
        (
            "tool_call",
            {
                "calls": [
                    {
                        "name": "guests_prepare_access",
                        "arguments": {
                            "person": "Alex",
                            "scope": {"tools": tools, "expires_at": expires},
                        },
                    }
                ]
            },
        ),
    ]

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if ha_plugin and self.path == "/api/states/light.porch":
                state["ha_reads"].append(self.path)
                wire = json.dumps(
                    {
                        "entity_id": "light.porch",
                        "state": "on",
                        "attributes": {"friendly_name": "Porch light"},
                    }
                ).encode()
            else:
                wire = json.dumps({"data": []}).encode()

            self.send_response(200)
            self.end_headers()
            self.wfile.write(wire)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            message = {"role": "assistant", "content": "Review Alex's access in the card above."}
            finish = "stop"
            if body.get("tools"):
                if state["phase"] == "owner" and state["step"] < len(plan):
                    name, args = plan[state["step"]]
                    state["step"] += 1
                elif state["phase"] == "guest" and not any(
                    m.get("role") == "tool" for m in body.get("messages", [])
                ):
                    name, args = (
                        "shared_" + service_tool,
                        guest_args,
                    )
                    state["guest_requests"].append(body)
                else:
                    name = None
                if name:
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "fixture_" + str(state["step"]),
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                        ],
                    }
                    finish = "tool_calls"
                elif state["phase"] == "guest":
                    message = {
                        "role": "assistant",
                        "content": (
                            "The porch light is on."
                            if ha_plugin
                            else "Found Example Comedy in Movies."
                        ),
                    }
            if body.get("stream"):
                delta = dict(message)
                if "tool_calls" in delta:
                    delta["tool_calls"] = [
                        {"index": i, **c} for i, c in enumerate(delta["tool_calls"])
                    ]
                chunks = [
                    {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "model": "local-owner",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    },
                    {
                        "id": "fixture",
                        "object": "chat.completion.chunk",
                        "model": "local-owner",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    },
                ]
                wire = (
                    "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
                ).encode()
                kind = "text/event-stream"
            else:
                wire = json.dumps(
                    {
                        "id": "fixture",
                        "object": "chat.completion",
                        "model": "local-owner",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    }
                ).encode()
                kind = "application/json"
            self.send_response(200)
            if not body.get("stream") or os.environ.get("GUESTS_FIXTURE_HEADERLESS_SSE") != "1":
                self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    runtime = {
        "model": "local-owner",
        "provider": "custom",
        "api_mode": "chat_completions",
        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "api_key": "fixture-not-a-secret",
    }
    import yaml

    config_path = home / "config.yaml"
    before = yaml.safe_load(config_path.read_text()) if installed else {}
    if installed:
        assert "hermes-guests" in before["plugins"]["enabled"]
    configuration = {
        "plugins": {
            "enabled": ["hermes-guests", "fixture-media"],
            "entries": {"hermes-guests": {"settings": {"owner_id": "100"}}},
        },
        "platform_toolsets": {"telegram": ["hermes_guests", "fixture_media"]},
        "model": {"default": "local-owner", "provider": "custom"},
    }
    if ha_plugin:
        configuration["plugins"]["enabled"].append("homeassistant")
        configuration["platform_toolsets"]["telegram"].append("homeassistant")
        shutil.copytree(ha_plugin, home / "plugins" / "homeassistant")
        with (home / ".env").open("a") as env_file:
            env_file.write(
                f"HASS_URL=http://127.0.0.1:{server.server_port}\nHASS_TOKEN=fixture-token\n"
            )
        os.environ["HASS_URL"] = f"http://127.0.0.1:{server.server_port}"
        os.environ["HASS_TOKEN"] = "fixture-token"
    coexistence = os.environ.get("GUESTS_FIXTURE_TENUO")
    if coexistence:
        configuration["plugins"]["enabled"].append("hermes-tenuo")
        entry = {} if coexistence == "unconfigured" else {"require_warrant": True}
        if coexistence == "signed":
            import base64

            from tenuo import SigningKey, Warrant

            issuer, holder = SigningKey.generate(), SigningKey.generate()
            builder = Warrant.mint_builder().holder(holder.public_key).ttl(600)
            for name in (
                "guests_discover",
                "guests_prepare_access",
                "guests_list_access",
                "guests_status",
                "guests_activity",
                "guests_data",
                "guests_repair",
                "guests_never",
                "tool_search",
                "tool_describe",
                "tool_call",
                "library_find",
            ):
                builder = builder.capability(name)
            os.environ["TENUO_SIGNING_KEY"] = base64.b64encode(holder.secret_key_bytes()).decode()
            entry.update(
                warrant=builder.mint(issuer).to_base64(),
                trusted_root=base64.b64encode(issuer.public_key.to_bytes()).decode(),
                signing_key_env="TENUO_SIGNING_KEY",
            )
        configuration["plugins"]["entries"]["hermes-tenuo"] = entry
    if saved_tools:
        configuration["platform_toolsets"]["telegram"].remove("hermes_guests")
        configuration["platform_toolsets"]["cli"] = ["web"]
        configuration["known_plugin_toolsets"] = {"telegram": ["hermes_guests"]}
    if installed and saved_tools:
        assert before["platform_toolsets"]["cli"] == ["web"]
        assert ("homeassistant" if ha_plugin else "fixture_media") in before["platform_toolsets"][
            "telegram"
        ]
        configuration["platform_toolsets"] = before["platform_toolsets"]
        if "hermes_guests" in configuration["platform_toolsets"]["telegram"]:
            saved_tools = False
    config_path.write_text(yaml.safe_dump(configuration))
    source = home / "plugins" / "fixture-media"
    source.mkdir(parents=True)
    (source / "plugin.yaml").write_text(
        "name: fixture-media\nversion: 0.1.0\nkind: standalone\ndescription: Local recording media service\n"
    )
    schema = {
        "name": "library_find",
        "description": "Find titles in a media library.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "library": {"type": "string"},
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 3},
            },
            "required": ["library", "query"],
        },
    }
    effects = home / "media-effects.jsonl"
    (source / "__init__.py").write_text(
        "import json\nfrom pathlib import Path\ndef call(args,**kw):\n    with Path("
        + repr(str(effects))
        + ').open("a") as f:f.write(json.dumps(args)+"\\n")\n    return json.dumps({"titles":["Example Comedy"]})\ndef register(ctx):\n    ctx.register_tool("library_find","fixture_media",'
        + repr(schema)
        + ",call)\n"
    )
    if os.environ.get("GUESTS_FIXTURE_GIT") == "1":
        (home / "plugins" / "hermes-guests").symlink_to(
            Path(__file__).resolve().parent.parent, target_is_directory=True
        )

    if several:
        from tui_gateway.launch_profile_policy import activate_multi_profile_hosting

        activate_multi_profile_hosting()

    import hermes_guests.plugin as guests_module

    guests_module.native_runtime = lambda: dict(runtime)
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load()
    factory = next(
        f for f, name in manager.get_telegram_handler_factories() if name == "hermes-guests"
    )
    plugin = factory.__self__
    if (home / "plugins" / "hermes-guests").is_dir():
        assert plugin.__class__.__module__ != "hermes_guests.plugin", (
            "Git loader fell back to installed package"
        )
    import sys

    sys.modules[plugin.__class__.__module__].native_runtime = lambda: dict(runtime)
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
        assert ids, "No real confirmation card was delivered"
        m = Message(ids[-1], datetime.now(timezone.utc), Chat(who, "private"), text="Review")
        m.set_bot(bot)
        q = CallbackQuery("fixture", User(who, "Person", False), "fixture", message=m, data=data)
        q.set_bot(bot)
        await app.process_update(Update(2, callback_query=q))

    try:
        if ha_plugin or saved_tools:
            await message(100, "Hello")
            assert any(
                "Set up guest sharing" in button.text
                for _, _, markup in bot.sent
                if markup
                for row in markup.inline_keyboard
                for button in row
            )
            await click(100, "hs:h:setup")
        else:
            await message(100, "/guests_setup")
        setup = next(markup for who, text, markup in reversed(bot.sent) if markup)
        if saved_tools:
            assert "adds Hermes Guests to your Telegram tools" in bot.sent[-1][1]
        await click(100, setup.inline_keyboard[0][0].callback_data)
        assert plugin.service.owner_ready(), [(who, text) for who, text, _ in bot.sent]
        if installed and (home / "owner-state-sentinel").exists():
            assert (home / "owner-state-sentinel").read_text() == "keep me\n"
        from hermes_cli.config import read_raw_config
        from hermes_cli.tools_config import _get_platform_tools

        config = read_raw_config()
        if saved_tools:
            expected = configuration["platform_toolsets"]["telegram"]
            assert config["platform_toolsets"]["telegram"] == [*expected, "hermes_guests"]
            assert config["platform_toolsets"]["cli"] == ["web"]
        from gateway.session_context import clear_session_vars, set_session_vars
        from run_agent import AIAgent

        tokens = set_session_vars(
            platform="telegram",
            chat_id="100",
            user_id="100",
            chat_type="dm",
            session_id="owner-fixture",
        )
        scope = None
        if several:
            # Hermes runs the owner's own turns on the profile's behalf.
            from agent.secret_scope import build_profile_secret_scope, set_secret_scope

            scope = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
        try:
            agent = AIAgent(
                **runtime,
                session_id="owner-fixture",
                enabled_toolsets=sorted(_get_platform_tools(config, "telegram")),
                disabled_toolsets=[],
                max_iterations=8,
                run_budget_seconds=40,
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
            await asyncio.to_thread(
                agent.run_conversation,
                "Let Alex check the porch light for one hour."
                if ha_plugin
                else "Let Alex search Movies for one hour. Keep everything else private.",
            )
        finally:
            if scope is not None:
                from agent.secret_scope import reset_secret_scope

                reset_secret_scope(scope)
            clear_session_vars(tokens)
        proposals = plugin.service.ledger.snapshot()["proposals"]
        if coexistence == "missing":
            assert not proposals, proposals
            assert not effects.exists(), "An owner tool ran despite the required global warrant"
            print("GLOBAL_REQUIRED_WARRANT_BLOCKED")
            return
        assert len(proposals) == 1, (state, [(who, text) for who, text, _ in bot.sent])
        p = next(iter(proposals.values()))
        assert p["status"] == "pending" and p["scope"]["tools"][0]["name"] == service_tool
        assert not plugin.service.list_access("100")
        await click(100, "hs:p:" + p["id"])
        invitation = next(
            text for who, text, _ in reversed(bot.sent) if who == 100 and "?start=guests_" in text
        ).split("?start=guests_", 1)[1]
        await message(200, "/start guests_" + invitation)
        from hermes_guests.core import token_hash

        await click(100, "hs:a:" + token_hash(invitation)[:24])
        state["phase"] = "guest"
        await message(
            200, "Is the porch light on?" if ha_plugin else "Find me two comedies in Movies"
        )
        await asyncio.gather(*list(plugin._guest_tasks.values()))
        if ha_plugin:
            assert state["ha_reads"] == ["/api/states/light.porch"], state["ha_reads"]
            assert any(who == 200 and "The porch light is on." in text for who, text, _ in bot.sent)
            print("SIMULATED_HOME_ASSISTANT_FLOW_OK")
        else:
            assert effects.exists(), [(who, text) for who, text, _ in bot.sent]
            assert json.loads(effects.read_text().strip()) == {
                "library": "Movies",
                "query": "comedy",
                "limit": 2,
            }
            assert any(who == 200 and "Found Example Comedy" in text for who, text, _ in bot.sent)
        names = {t["function"]["name"] for body in state["guest_requests"] for t in body["tools"]}
        assert ("shared_" + service_tool) in names and not {
            "terminal",
            "read_file",
            "guests_prepare_access",
            "tool_call",
        } & names
        print("OWNER_GUEST_NATIVE_FLOW_OK")
    finally:
        plugin.close()
        await asyncio.sleep(0)
        await app.shutdown()
        await asyncio.to_thread(server.shutdown)
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
