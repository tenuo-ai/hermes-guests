"""Loopback-only, per-turn capability transport between isolated native guest agents and host."""

from __future__ import annotations

import contextvars
import hmac
import json
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .compat import in_own_profile
from .inference import codex_input, request_codex
from .model import GuestsError

CODEX_REQUEST_FIELDS = frozenset(
    {
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "include",
        "prompt_cache_key",
        "text",
    }
)
MAX_CODEX_REQUEST = 4 * 1024 * 1024


class LocalBroker:
    def __init__(self, service):
        self.service = service
        self.tokens = {}
        self.lock = threading.RLock()
        self.context = contextvars.copy_context()
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                if self.client_address[0] != "127.0.0.1":
                    self.send_error(403)
                    return
                if self.path == "/responses":
                    self.codex()
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 65536 or self.path not in {
                        "/call",
                        "/delegate",
                        "/memory",
                        "/zone",
                    }:
                        raise GuestsError("Invalid local request.")
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    with broker.lock:
                        binding = broker.tokens.get(token)
                    if not binding or not hmac.compare_digest(token, binding["token"]):
                        raise GuestsError("Local session is unavailable.")
                    body = json.loads(self.rfile.read(size))
                    if self.path == "/zone":
                        if binding["delegation"]:
                            raise GuestsError("Only the guest can set their time zone.")
                        result = broker.context.copy().run(
                            in_own_profile,
                            broker.service.set_guest_zone,
                            binding["actor"],
                            binding["share_id"],
                            body.get("time_zone"),
                        )
                    elif self.path == "/memory":
                        result = broker.context.copy().run(
                            in_own_profile,
                            broker.service.admit_memory,
                            binding["actor"],
                            binding["share_id"],
                            body.get("target"),
                            delegation=binding["delegation"],
                        )
                    elif self.path == "/call":
                        result = broker.context.copy().run(
                            in_own_profile,
                            broker.service.call,
                            binding["actor"],
                            binding["share_id"],
                            body["tool"],
                            body["args"],
                            delegation=binding["delegation"],
                        )
                    else:
                        child = broker.context.copy().run(
                            in_own_profile,
                            broker.service.delegate,
                            binding["actor"],
                            binding["share_id"],
                            body["scope"],
                            task=body["task"],
                            parent_delegation=binding["delegation"],
                        )
                        try:
                            if broker.service.runner is None:
                                raise GuestsError("Subagent execution isn't connected.")
                            result = broker.service.runner.run(
                                binding["actor"],
                                binding["share_id"],
                                body["task"],
                                delegation=child["task_id"],
                            )
                        finally:
                            broker.service.finish_task(child["task_id"])
                    response = {"ok": True, "result": result}
                except GuestsError as exc:
                    response = {"ok": False, "error": str(exc)}
                except Exception:
                    response = {
                        "ok": False,
                        "error": "The local action could not be completed. Access was not changed.",
                    }
                wire = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)

            def codex(self):
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_CODEX_REQUEST:
                        self.send_error(413)
                        return
                    token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                    with broker.lock:
                        binding = broker.tokens.get(token)
                    if not binding or not binding["codex_model"]:
                        self.send_error(403)
                        return
                    body = json.loads(self.rfile.read(size))
                    model = binding["codex_model"].removesuffix("-900k")
                    if (
                        not isinstance(body, dict)
                        or body.get("model") != model
                        or not isinstance(body.get("input"), list)
                        or not isinstance(body.get("tools", []), list)
                        or any(
                            not isinstance(tool, dict) or tool.get("type") != "function"
                            for tool in body.get("tools", [])
                        )
                    ):
                        self.send_error(400)
                        return
                    payload = {
                        key: value for key, value in body.items() if key in CODEX_REQUEST_FIELDS
                    }
                    payload.update(store=False, stream=True, input=codex_input(body["input"]))
                    reply = broker.context.copy().run(
                        in_own_profile, broker.codex_request, token, binding, payload
                    )
                except GuestsError:
                    self.send_error(403)
                    return
                except (ValueError, TypeError):
                    self.send_error(400)
                    return
                except Exception:
                    self.send_error(502)
                    return
                if not 200 <= reply.status < 300:
                    # Keep the status for Hermes retry handling. Provider headers and
                    # error bodies can contain account information.
                    status = reply.status if 400 <= reply.status < 600 else 502
                    self.send_error(status)
                    return
                content_type = (
                    next(
                        (
                            value
                            for key, value in reply.headers.items()
                            if key.casefold() == "content-type"
                        ),
                        "",
                    )
                    .split(";", 1)[0]
                    .strip()
                    .casefold()
                )
                # The Codex backend can omit Content-Type on a successful SSE response.
                headerless_stream = not content_type and reply.body.startswith(
                    (b"event: response.", b'data: {"type":"response.')
                )
                if content_type != "text/event-stream" and not headerless_stream:
                    self.send_error(502)
                    return
                self.send_response(reply.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(reply.body)))
                self.end_headers()
                self.wfile.write(reply.body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True, name="hermes-guests-broker"
        )
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:" + str(self.server.server_port)

    def open(self, actor, share_id, delegation=None, *, codex_model=None, revision=None):
        # Only the trusted transport may bind a process to a guest; the model cannot choose this token.
        self.service.get_access(actor, share_id)
        token = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[token] = {
                "token": token,
                "actor": str(actor),
                "share_id": share_id,
                "delegation": delegation,
                "codex_model": codex_model,
                "revision": revision,
                "deadline": time.monotonic() + 80,
            }
        return token

    def codex_request(self, token, binding, payload):
        record = self.service.admit_turn(
            binding["actor"], binding["share_id"], delegation=binding["delegation"]
        )
        with self.lock:
            remaining = binding["deadline"] - time.monotonic()
            if (
                self.tokens.get(token) is not binding
                or record["revision"] != binding["revision"]
                or remaining <= 0
            ):
                raise GuestsError("Local model access has ended.")
        return request_codex(payload, timeout=min(remaining, 60))

    def close_token(self, token):
        with self.lock:
            self.tokens.pop(token, None)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def rpc(url, token, operation, body):
    request = urllib.request.Request(
        url + "/" + operation,
        json.dumps(body).encode(),
        {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=100) as response:
        result = json.load(response)
    return result
