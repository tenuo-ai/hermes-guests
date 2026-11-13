import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from hermes_guests.core import GuestsService
from hermes_guests.guest import NativeGuestRunner
from hermes_guests.inference import CODEX_URL, resolve_model
from hermes_guests.model import GuestsError, iso
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState
from hermes_guests.transport import LocalBroker


class ModelRoutingTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("run_agent"), "Native Hermes is unavailable")
    def test_native_route_reads_the_active_profiles_key(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        from hermes_guests.plugin import native_runtime

        model = {"provider": "openrouter", "default": "test-model"}
        with patch("hermes_cli.config.load_config_readonly", return_value={"model": model}):
            for key in ("first-profile-key", "second-profile-key"):
                scope = set_secret_scope({"OPENROUTER_API_KEY": key})
                try:
                    self.assertEqual(native_runtime()["api_key"], key)
                finally:
                    reset_secret_scope(scope)

    def test_api_key_and_local_routes(self):
        cases = (
            ("openai", "OPENAI_API_KEY", "codex_responses"),
            ("openrouter", "OPENROUTER_API_KEY", "chat_completions"),
            ("anthropic", "ANTHROPIC_API_KEY", "anthropic_messages"),
            ("google", "GOOGLE_API_KEY", "chat_completions"),
            ("deepseek", "DEEPSEEK_API_KEY", "chat_completions"),
            ("grok", "XAI_API_KEY", "codex_responses"),
            ("ollama", None, "chat_completions"),
            ("lmstudio", None, "chat_completions"),
        )
        for provider, env, mode in cases:
            with self.subTest(provider=provider):
                key = "sk-ant-api-synthetic" if provider == "anthropic" else "profile-key"
                secrets = {env: key} if env else {}
                route = resolve_model(
                    {"model": {"provider": provider, "default": "test-model"}}, secrets.get
                )
                self.assertEqual(route["api_mode"], mode)
                self.assertEqual(route["api_key"], key if env else "guests-no-key")

    def test_codex_and_fallback_routes_do_not_lookup_subscription_credentials(self):
        get_secret = Mock(return_value=None)
        config = {
            "model": {"provider": "nous", "default": "portal-model"},
            "fallback_providers": [{"provider": "openai-codex", "model": "codex-model"}],
        }
        route = resolve_model(config, get_secret)
        self.assertEqual(
            route,
            {"provider": "openai-codex", "model": "codex-model", "api_mode": "codex_responses"},
        )
        get_secret.assert_not_called()

    def test_unsupported_subscription_and_external_runtimes_fail_closed(self):
        for provider in ("nous", "claude-oauth", "xai-oauth", "qwen-oauth"):
            with self.subTest(provider=provider), self.assertRaises(GuestsError):
                resolve_model({"model": {"provider": provider, "default": "m"}}, Mock())
        for overrides in (
            {"openai_runtime": "codex_app_server"},
            {"base_url": "https://relay.example.com"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(GuestsError):
                resolve_model(
                    {"model": {"provider": "openai-codex", "default": "m", **overrides}},
                    Mock(),
                )
        with self.assertRaisesRegex(GuestsError, "Anthropic API key"):
            resolve_model(
                {"model": {"provider": "anthropic", "default": "m"}},
                lambda _: "sk-ant-oat-synthetic",
            )

    def test_custom_endpoint_cannot_inherit_an_unrelated_provider_key(self):
        get_secret = Mock(return_value="profile-key")
        config = {
            "model": {
                "provider": "openrouter",
                "default": "m",
                "base_url": "https://relay.example.com",
            }
        }
        with self.assertRaisesRegex(GuestsError, "key_env"):
            resolve_model(config, get_secret)
        get_secret.assert_not_called()
        config = {
            "model": {"provider": "custom:relay"},
            "providers": {
                "relay": {
                    "api": "https://relay.example.com/v1",
                    "default_model": "relay-model",
                    "key_env": "RELAY_API_KEY",
                    "transport": "anthropic_messages",
                }
            },
        }
        route = resolve_model(config, get_secret)
        self.assertEqual(route["model"], "relay-model")
        self.assertEqual(route["api_mode"], "anthropic_messages")
        get_secret.assert_called_once_with("RELAY_API_KEY")

    def test_a_named_providers_key_goes_only_to_its_own_endpoint(self):
        get_secret = Mock(return_value="relay-key")
        relay = {"base_url": "https://relay.example.com/v1", "key_env": "RELAY_API_KEY"}
        left_over = {"base_url": "https://other.example.com/v1"}
        for config in (
            {
                "model": {"provider": "custom:relay", "default": "m", **left_over},
                "providers": {"relay": relay},
            },
            {
                "model": {"provider": "custom:relay", "default": "m", **left_over},
                "custom_providers": [{"name": "Relay", **relay}],
            },
            {
                "model": {"provider": "nous", "default": "m"},
                "fallback_providers": [{"provider": "custom:relay", "model": "m", **left_over}],
                "providers": {"relay": relay},
            },
        ):
            with self.subTest(config=config):
                route = resolve_model(config, get_secret)
                self.assertEqual(route["base_url"], "https://relay.example.com/v1")
                self.assertEqual(route["api_key"], "relay-key")
        get_secret.reset_mock()
        route = resolve_model(
            {
                "model": {"provider": "openrouter", "default": "m", **left_over},
                "providers": {"openrouter": {"key_env": "MY_OPENROUTER_KEY"}},
            },
            get_secret,
        )
        self.assertEqual(route["base_url"], "https://openrouter.ai/api/v1")
        get_secret.assert_called_once_with("MY_OPENROUTER_KEY")

    def test_the_chosen_model_wins_over_a_providers_own_default(self):
        relay = {"base_url": "https://relay.example.com/v1", "key_env": "RELAY_API_KEY"}
        for config in (
            {
                "model": {"provider": "custom:relay", "default": "chosen-model"},
                "custom_providers": [{"name": "relay", "model": "old-default", **relay}],
            },
            {
                "model": {"provider": "custom:relay", "default": "chosen-model"},
                "providers": {"relay": {"default_model": "old-default", **relay}},
            },
            {
                "model": {"provider": "nous", "default": "portal-model"},
                "fallback_providers": [{"provider": "custom:relay", "model": "chosen-model"}],
                "providers": {"relay": {"model": "old-default", **relay}},
            },
        ):
            with self.subTest(config=config):
                route = resolve_model(config, lambda _: "relay-key")
                self.assertEqual(route["model"], "chosen-model")

    def test_a_choice_left_to_hermes_uses_the_provider_hermes_selects(self):
        secrets = {"OPENROUTER_API_KEY": "profile-key"}
        for model in ({"provider": "auto", "default": "m"}, {"default": "m"}, "m"):
            with self.subTest(model=model):
                route = resolve_model({"model": model}, secrets.get, lambda: "openrouter")
                self.assertEqual(route["provider"], "openrouter")
                self.assertEqual(route["base_url"], "https://openrouter.ai/api/v1")
                self.assertEqual(route["api_key"], "profile-key")
                self.assertEqual(route["model"], "m")

        route = resolve_model({"model": "m"}, {"OPENAI_API_KEY": "k"}.get, lambda: "openai-api")
        self.assertEqual(route["provider"], "openai")
        self.assertEqual(route["base_url"], "https://api.openai.com/v1")

        relay = {"base_url": "https://relay.example.com/v1", "key_env": "RELAY_API_KEY"}
        get_secret = Mock(return_value="relay-key")
        route = resolve_model(
            {"model": "m", "providers": {"openrouter": relay}}, get_secret, lambda: "openrouter"
        )
        self.assertEqual(route["base_url"], "https://relay.example.com/v1")
        get_secret.assert_called_once_with("RELAY_API_KEY")

        get_secret, identify = Mock(return_value="profile-key"), Mock(return_value="nous")
        with self.assertRaises(GuestsError):
            resolve_model({"model": "m"}, get_secret, identify)
        get_secret.assert_not_called()

        identify = Mock(return_value="nous")
        route = resolve_model(
            {"model": {"provider": "openrouter", "default": "m"}}, secrets.get, identify
        )
        self.assertEqual(route["provider"], "openrouter")
        route = resolve_model(
            {"model": {"default": "m", "base_url": "http://127.0.0.1:11434/v1"}},
            secrets.get,
            identify,
        )
        self.assertEqual(route["base_url"], "http://127.0.0.1:11434/v1")
        identify.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("run_agent"), "Native Hermes is unavailable")
    def test_native_route_follows_hermes_when_the_provider_is_left_to_it(self):
        from agent.secret_scope import reset_secret_scope, set_secret_scope

        from hermes_guests.plugin import native_runtime

        for name, provider in (("OPENROUTER_API_KEY", "openrouter"), ("OPENAI_API_KEY", "openai")):
            with (
                self.subTest(key=name),
                tempfile.TemporaryDirectory() as home,
                patch.dict("os.environ", {"HERMES_HOME": home}),
                patch("hermes_cli.config.load_config_readonly", return_value={"model": "m"}),
            ):
                scope = set_secret_scope({name: "profile-key"})
                try:
                    route = native_runtime()
                finally:
                    reset_secret_scope(scope)
                self.assertEqual(route["provider"], provider)
                self.assertEqual(route["api_key"], "profile-key")


def responses_sse(item):
    response = {
        "id": "resp_fixture",
        "object": "response",
        "created_at": 1,
        "model": "codex-fixture",
        "status": "in_progress",
        "output": [],
    }
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {
            "type": "response.completed",
            "response": {
                **response,
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        },
    ]
    wire = "".join(
        f"event: {event['type']}\ndata: {json.dumps(dict(event, sequence_number=index))}\n\n"
        for index, event in enumerate(events)
    )
    return SimpleNamespace(
        status=200, headers={"content-type": "text/event-stream"}, body=wire.encode()
    )


class CodexBrokerTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.root = Path(home.name)
        self.effects = []

        def dispatch(tool, args, envelope):
            self.effects.append((tool, args))
            return {"state": "on"}

        self.service = GuestsService(
            FileState(self.root / "control"),
            "100",
            catalog={"light.porch": {"name": "Porch light"}},
            dispatch=dispatch,
            tool_resolver=GUEST_SCHEMAS.get,
        )
        proposal = self.service.prepare(
            "100", "Alex", {"lights": ["light.porch"], "expires_at": iso(time.time() + 3600)}
        )
        invite = self.service.approve("100", proposal["proposal_id"])
        claim = self.service.claim(invite["token"], "200", "Alex")
        self.record = self.service.accept_claim("100", claim["claim_id"])
        self.sid = self.record["share_id"]
        self.broker = LocalBroker(self.service)
        self.addCleanup(self.broker.close)

    def test_wrong_model_token_tool_type_and_revocation_stop_model_dispatch(self):
        token = self.broker.open(
            "200", self.sid, codex_model="codex-fixture", revision=self.record["revision"]
        )

        def denied(expected, request_token, **overrides):
            body = {"model": "codex-fixture", "input": [], **overrides}
            request = Request(
                self.broker.url + "/responses",
                json.dumps(body).encode(),
                {"Authorization": "Bearer " + request_token},
            )
            with self.assertRaises(HTTPError) as exc:
                urlopen(request, timeout=5)
            self.assertEqual(exc.exception.code, expected)
            exc.exception.close()

        with patch("hermes_guests.transport.request_codex") as provider:
            provider.return_value = SimpleNamespace(
                status=200, headers={"Content-Type": "text/event-stream"}, body=b"data: ok\n\n"
            )
            history = [
                {"role": "user", "content": "Is the porch light on?"},
                {"role": "assistant", "content": "It is on."},
                {"role": "user", "content": [{"type": "input_text", "text": "Thanks."}]},
                {"type": "function_call_output", "call_id": "call_fixture", "output": "on"},
            ]
            request = Request(
                self.broker.url + "/responses",
                json.dumps({"model": "codex-fixture", "input": history}).encode(),
                {"Authorization": "Bearer " + token},
            )
            with urlopen(request, timeout=5) as reply:
                self.assertEqual(reply.read(), b"data: ok\n\n")
            self.assertEqual(
                provider.call_args.args[0]["input"],
                [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Is the porch light on?"}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "It is on."}],
                    },
                    *history[2:],
                ],
            )
            provider.reset_mock()
            denied(403, "wrong-turn")
            denied(400, token, model="unapproved-model")
            denied(400, token, tools=[{"type": "web_search"}])
            review = self.service.prepare("100", "Alex", None, share_id=self.sid, action="revoke")
            self.service.approve("100", review["proposal_id"])
            denied(403, token)
            provider.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("run_agent"), "Native Hermes is unavailable")
    def test_native_worker_runs_a_tool_and_resumes_through_host_codex_request(self):
        def profile(name):
            home = self.root / "profiles" / name
            home.mkdir(parents=True, exist_ok=True)
            return home

        runner = NativeGuestRunner(
            self.service,
            self.broker,
            lambda: {
                "model": "codex-fixture",
                "provider": "openai-codex",
                "api_mode": "codex_responses",
            },
            profile_factory=profile,
        )
        calls = [
            responses_sse(
                {
                    "type": "function_call",
                    "id": "fc_fixture",
                    "call_id": "call_fixture",
                    "name": "shared_ha_get_state",
                    "arguments": '{"entity_id":"light.porch"}',
                    "status": "completed",
                }
            ),
            responses_sse(
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": "The porch light is on.", "annotations": []}
                    ],
                }
            ),
        ]

        calls[0].headers = {}  # Live Codex streams can omit Content-Type.
        calls[1].headers = {"Content-Type": "text/event-stream; charset=utf-8"}

        def respond(*args, json, **kwargs):
            used_tool = any(item.get("type") == "function_call_output" for item in json["input"])
            return calls[0] if json.get("tools") and not used_tool else calls[1]

        with patch(
            "hermes_cli.plugin_provider_requests.credentialed_provider_request", side_effect=respond
        ) as provider:
            self.assertEqual(
                runner.run("200", self.sid, "Is the porch light on?"), "The porch light is on."
            )
        self.assertEqual(self.effects, [("ha_get_state", {"entity_id": "light.porch"})])
        main_calls = [r for r in provider.call_args_list if r.kwargs["json"].get("tools")]
        self.assertEqual(len(main_calls), 2)
        for request in provider.call_args_list:
            self.assertEqual(request.args, ("openai-codex", "POST", CODEX_URL))
            self.assertTrue(request.kwargs["json"]["stream"])
            self.assertFalse(request.kwargs["json"]["store"])
            messages = [item for item in request.kwargs["json"]["input"] if item.get("role")]
            self.assertTrue(messages)
            for item in messages:
                kind = "output_text" if item["role"] == "assistant" else "input_text"
                self.assertEqual({part["type"] for part in item["content"]}, {kind})
        followup = main_calls[-1].kwargs["json"]["input"]
        self.assertTrue(any(item.get("type") == "function_call_output" for item in followup))
        self.assertEqual(self.broker.tokens, {})
