"""Native Hermes agent/registry integration with a local scripted model and service fixture.

No Telegram, provider credentials, real home devices, or existing service changes.
"""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from hermes_guests.core import GuestsService
from hermes_guests.guest import NativeGuestRunner
from hermes_guests.model import iso
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState
from hermes_guests.transport import LocalBroker


class NativeAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util

        if importlib.util.find_spec("run_agent") is None:
            raise unittest.SkipTest(
                "Set PYTHONPATH to the pinned Hermes source for native integration tests."
            )

    def test_native_agent_uses_only_scoped_proxy_and_isolated_context(self):
        self.run_case(delegation=False)

    def test_native_agent_automatically_runs_narrowed_subagent(self):
        self.run_case(delegation=True)

    def test_unfamiliar_plugin_runs_through_native_agent_without_adapter(self):
        self.run_case(delegation=False, generic=True)

    def test_unfamiliar_plugin_automatically_delegates_through_native_agent(self):
        self.run_case(delegation=True, generic=True)

    def test_chat_only_native_agent_has_no_tools(self):
        self.run_case(delegation=False, chat_only=True)

    def test_access_whose_only_tool_is_now_never_shared_still_gets_a_reply_without_tools(self):
        self.run_case(delegation=False, generic=True, listed=True)

    def test_native_agent_uses_exact_grouped_tool_permissions(self):
        self.run_case(delegation=False, generic=True, grouped=True)

    def test_guest_memory_survives_new_without_owner_or_other_guest_context(self):
        self.run_case(delegation=False, chat_only=True, memory=True)

    def run_case(
        self,
        *,
        delegation,
        generic=False,
        chat_only=False,
        grouped=False,
        listed=False,
        memory=False,
    ):
        captures = []

        class Model(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                wire = json.dumps({"data": []}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(wire)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captures.append(body)
                messages = body.get("messages", [])
                has_tools = any(
                    t["function"]["name"] != "guest_memory" for t in body.get("tools", [])
                )
                used = any(m.get("role") == "tool" for m in messages)
                user_text = next(
                    (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
                    "",
                )
                memory_count = sum(m.get("role") == "tool" for m in messages)
                if memory and "Fresh memory check" not in str(user_text) and memory_count < 2:
                    target, fact = (
                        ("user", "Guest prefers concise answers.")
                        if memory_count == 0
                        else ("memory", "Guest calls their project Cedar.")
                    )
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "save_" + target,
                                "type": "function",
                                "function": {
                                    "name": "guest_memory",
                                    "arguments": json.dumps(
                                        {
                                            "target": target,
                                            "operations": [{"action": "add", "content": fact}],
                                        }
                                    ),
                                },
                            }
                        ],
                    }
                    finish = "tool_calls"
                elif has_tools and not used:
                    names = {t["function"]["name"] for t in body["tools"]}
                    do_delegate = delegation and "guests_delegate_task" in names
                    name = (
                        "guests_delegate_task"
                        if do_delegate
                        else ("shared_library_find" if generic else "shared_ha_get_state")
                    )
                    args = (
                        {
                            "task": "Check the porch light",
                            "scope": (
                                {
                                    "tools": [
                                        {
                                            "name": "library_find",
                                            "constraints": {
                                                "library": {"equals": "Movies"},
                                                "query": {"equals": "comedy"},
                                                "limit": {"equals": 2},
                                            },
                                        }
                                    ]
                                }
                                if generic
                                else {"lights": ["light.porch"], "research": False}
                            ),
                        }
                        if do_delegate
                        else (
                            {"library": "Movies", "query": "comedy", "limit": 2}
                            if generic
                            else {"entity_id": "light.porch"}
                        )
                    )
                    if grouped and not do_delegate:
                        args = {"request": [args]}
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_light",
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args),
                                },
                            }
                        ],
                    }
                    finish = "tool_calls"
                else:
                    message = {"role": "assistant", "content": "The porch light is on."}
                    finish = "stop"
                wire = json.dumps(
                    {
                        "id": "local_test",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "local-guests-test",
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                    }
                ).encode()
                if body.get("stream"):
                    delta = dict(message)
                    if "tool_calls" in delta:
                        delta["tool_calls"] = [
                            {"index": i, **call} for i, call in enumerate(delta["tool_calls"])
                        ]
                    chunks = [
                        {
                            "id": "local_test",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-guests-test",
                            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                        },
                        {
                            "id": "local_test",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": "local-guests-test",
                            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                        },
                    ]
                    wire = (
                        "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                        + "data: [DONE]\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

        model = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        thread = threading.Thread(target=model.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(model.server_close)
        self.addCleanup(model.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            import time

            root = Path(directory)
            effects = []

            def dispatch(tool, args, envelope):
                effects.append((tool, args, envelope))
                return {"entity_id": "light.porch", "state": "on"}

            generic_schema = {
                "name": "library_find",
                "description": "Find media titles.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "library": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["library", "query"],
                },
            }
            service = GuestsService(
                FileState(root / "control"),
                "100",
                catalog={"light.porch": {"name": "Porch light"}},
                dispatch=dispatch,
                tool_resolver=lambda n: (
                    generic_schema if n == "library_find" else GUEST_SCHEMAS.get(n)
                ),
            )
            p = service.prepare(
                "100",
                "Alex",
                {
                    "chat_only": chat_only,
                    "lights": [] if generic or chat_only else ["light.porch"],
                    "research": delegation and not generic,
                    "tools": [
                        {
                            "name": "library_find",
                            "constraints": {
                                "library": {"equals": "Movies"},
                                "query": {"any": True},
                                "limit": {"range": [1, 5]},
                            },
                        }
                    ]
                    + (
                        [
                            {
                                "name": "library_find",
                                "constraints": {
                                    "library": {"equals": "Books"},
                                    "query": {"equals": "math"},
                                    "limit": {"range": [1, 5]},
                                },
                            }
                        ]
                        if grouped
                        else []
                    )
                    if generic
                    else [],
                    "expires_at": iso(time.time() + 3600),
                },
            )
            inv = service.approve("100", p["proposal_id"])
            claim = service.claim(inv["token"], "200", "Alex")
            active = service.accept_claim("100", claim["claim_id"])
            broker = LocalBroker(service)
            self.addCleanup(broker.close)

            def make_profile(name):
                path = root / "profiles" / name
                path.mkdir(parents=True, exist_ok=True)
                return path

            runner = NativeGuestRunner(
                service,
                broker,
                lambda: {
                    "model": "local-guests-test",
                    "provider": "custom",
                    "api_mode": "chat_completions",
                    "base_url": f"http://127.0.0.1:{model.server_port}/v1",
                    "api_key": "local-test-not-a-secret",
                },
                profile_factory=make_profile,
            )
            if listed:  # after the access was given, its only tool goes on the list
                service.never_set("100", "tools", "library_find", True)
            # Unrelated homes contain secrets a guest must never receive.
            for name in ("owner", "other-guest"):
                private = root / name / "memories"
                private.mkdir(parents=True)
                (private / "USER.md").write_text(name + "-private-sentinel")
            result = runner.run("200", active["share_id"], "Is the porch light on?")
            if memory:
                home = runner.ensure_profile(active["share_id"])
                self.assertIn(
                    "Guest prefers concise answers", (home / "memories" / "USER.md").read_text()
                )
                self.assertIn("project Cedar", (home / "memories" / "MEMORY.md").read_text())
                runner.reset_context("200", active["share_id"])
                captures.clear()
                runner.run("200", active["share_id"], "Fresh memory check")
                prompts = json.dumps(captures)
                self.assertIn("Guest prefers concise answers", prompts)
                self.assertIn("project Cedar", prompts)
                self.assertNotIn("Is the porch light on?", prompts)
                self.assertNotIn("owner-private-sentinel", prompts)
                self.assertNotIn("other-guest-private-sentinel", prompts)
                self.assertEqual(effects, [])
                from hermes_guests.model import GUEST_MEMORY_CAPABILITY

                self.assertEqual(
                    sum(
                        e.get("tool") == GUEST_MEMORY_CAPABILITY
                        and e.get("decision") == "ALLOW"
                        and e.get("inputs", {}).get("operation") == "write"
                        for e in service.activity("100", active["share_id"])
                    ),
                    2,
                )
                self.assertNotIn(
                    "project Cedar", json.dumps(service.activity("100", active["share_id"]))
                )
                return
            self.assertIn("porch light is on", result)
            if chat_only or listed:
                self.assertEqual(effects, [])
                self.assertTrue(captures)
                self.assertTrue(
                    all(
                        {t["function"]["name"] for t in body.get("tools", [])} <= {"guest_memory"}
                        for body in captures
                    )
                )
                return
            self.assertEqual(
                [e[0] for e in effects],
                ["library_find" if generic else "ha_get_state"],
                repr(
                    [
                        {
                            "tools": b.get("tools"),
                            "roles": [m.get("role") for m in b.get("messages", [])],
                        }
                        for b in captures
                    ]
                ),
            )
            tool_names = {t["function"]["name"] for b in captures for t in b.get("tools", [])}
            self.assertIn("shared_library_find" if generic else "shared_ha_get_state", tool_names)
            self.assertNotIn("terminal", tool_names)
            self.assertNotIn("ha_list_entities", tool_names)
            self.assertNotIn("memory", tool_names)
            self.assertNotIn("session_search", tool_names)
            self.assertTrue((runner.ensure_profile(active["share_id"]) / "state.db").exists())
            self.assertNotIn("issuer.key", json.dumps(captures))
            self.assertEqual(len(effects[0][2]["chain"]), 2 if delegation else 1)
            if grouped:
                self.assertEqual(
                    effects[0][1], {"library": "Movies", "query": "comedy", "limit": 2}
                )
                self.assertEqual(effects[0][2]["argument_view"], "single_request")
            if delegation:
                child_turns = [
                    b
                    for b in captures
                    if b.get("tools")
                    and "guests_delegate_task" not in {t["function"]["name"] for t in b["tools"]}
                ]
                self.assertTrue(child_turns, "A real child native agent must run.")
                for b in child_turns:
                    names = {t["function"]["name"] for t in b["tools"]}
                    self.assertNotIn("shared_web_search", names)
                    self.assertNotIn("shared_web_extract", names)
                self.assertEqual(
                    service.delegates,
                    {},
                    "The child grant must be detached when its task finishes.",
                )
                self.assertTrue(
                    any(
                        e["kind"] == "delegated"
                        for e in service.activity("100", active["share_id"])
                    )
                )


if __name__ == "__main__":
    unittest.main()
