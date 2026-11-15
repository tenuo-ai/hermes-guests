"""Native Hermes guest execution in a separate process and fresh profile, not a new model loop."""

from __future__ import annotations

import contextvars
import copy
import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from .compat import in_own_profile
from .inference import CODEX_PROVIDER, NATIVE_MODES
from .model import GuestsError
from .store import atomic_private


def installed_as_package():
    """True where Hermes can load Hermes Guests by its entry point, with no folder of its own."""
    from importlib.metadata import entry_points

    try:
        return any(e.name == "hermes-guests" for e in entry_points(group="hermes_agent.plugins"))
    except Exception:
        return False


class NativeGuestRunner:
    def __init__(
        self,
        service,
        broker,
        runtime_resolver,
        *,
        profile_factory=None,
        profile_resolver=None,
        profile_deleter=None,
    ):
        self.service, self.broker = service, broker
        self.resolve_runtime = runtime_resolver
        self.profile_factory = profile_factory
        self.profile_resolver = profile_resolver
        self.profile_deleter = profile_deleter
        self.context = contextvars.copy_context()
        self.profiles = {}
        self.busy = set()
        self.processes = {}
        self.closed = False
        self.lock = threading.Lock()
        self.profile_lock = threading.RLock()
        service.runner = self

    def ensure_profile(self, share_id):
        with self.service.ledger.transaction() as db, self.profile_lock:
            record = self.service._record(db, share_id)
            if (
                not self.service._fresh(record)
                or record.get("data_deletion_requested_at") is not None
            ):
                raise GuestsError("This access has ended. The guest profile cannot be opened.")
            return self._ensure_profile(share_id)

    def _ensure_profile(self, share_id):
        if share_id in self.profiles:
            return self.profiles[share_id]
        name = "guests-" + share_id.replace("_", "-")
        created = False
        if self.profile_factory:
            home = Path(self.profile_factory(name))
        else:
            from .compat import guest_profile

            home, created = guest_profile(
                name,
                no_alias=True,
                no_skills=True,
                description="Hermes Guests isolated guest",
                soul="You help a guest only within the access supplied by Hermes Guests.",
            )
        if home.is_symlink() or (home / "memories").is_symlink():
            raise GuestsError("The guest profile has an unsafe storage path.")
        if any((home / "memories" / name).is_symlink() for name in ("MEMORY.md", "USER.md")):
            raise GuestsError("Guest memory must stay inside its own profile.")
        marker = home / ".hermes-guests-profile"
        if marker.exists() and marker.read_text() != share_id:
            raise GuestsError("The guest profile belongs to another access record.")
        if not marker.exists():
            # Hermes can initialize a session DB as soon as it publishes a new
            # profile. Only profiles this call created can bypass the adoption check.
            if not created and (
                (home / "state.db").exists()
                or any((home / "memories" / name).exists() for name in ("USER.md", "MEMORY.md"))
            ):
                raise GuestsError("A profile with existing private state cannot be adopted.")
            atomic_private(marker, share_id.encode())
        config = {
            "plugins": {"enabled": ["hermes-guests"]},
            "gateway": {"standalone": True},
            "memory": {"memory_enabled": True, "user_profile_enabled": True, "provider": "builtin"},
            "background_review": {"enabled": False},
            "compression": {"enabled": False},
            "toolsets": ["hermes_guests_guest"],
            "tools": {"tool_search": {"enabled": "off"}},
            "agent": {"max_iterations": 8},
        }
        import yaml

        atomic_private(home / "config.yaml", yaml.safe_dump(config).encode())
        atomic_private(home / "gateway.parked", b"Managed guest worker; no independent gateway.\n")
        # The worker finds Hermes Guests by its package entry point wherever Hermes Guests is installed as
        # a package: with pip, and with Hermes's own installer, which installs it into the
        # one environment every profile shares. A link in the guest's plugin folder would
        # then be a second installation of the same plugin, which Hermes refuses and
        # answers by switching the plugin off in the guest profile. Only a plain source
        # checkout, which has no entry point, is linked in, and never the owner's plugin
        # folder.
        from .store import private_dir

        project = Path(__file__).resolve().parent.parent
        link = private_dir(home / "plugins") / "hermes-guests"
        if installed_as_package():
            if link.is_symlink():
                link.unlink()
        elif (project / "plugin.yaml").is_file():
            if link.is_symlink():
                if link.resolve() != project:
                    link.unlink()  # this profile is Hermes Guests' own: follow the code when it moves
                    link.symlink_to(project, target_is_directory=True)
            elif link.exists():
                raise GuestsError("The guest plugin path belongs to another installation.")
            else:
                link.symlink_to(project, target_is_directory=True)
        self.profiles[share_id] = home
        return home

    def data_status(self, record):
        if record.get("profile_deleted_at") is not None:
            return "Guest conversations, memories and profile have been deleted."
        if record.get("data_deletion_requested_at") is not None:
            return "Access ended. Guest data deletion is pending until workers stop and the owned profile can be removed."
        seconds = self.service.data_settings(self.service.owner_id)["retention_seconds"]
        if seconds is None:
            return "Guest conversations and private memories are kept until you delete them. Ended access cannot use the model or services."
        return (
            "Guest conversations and memories are kept for "
            + self.service.retention_text(seconds)
            + " after access ends."
        )

    def model_route(self):
        try:
            runtime = self.context.copy().run(in_own_profile, self.resolve_runtime)
        except GuestsError:
            raise
        except Exception as exc:
            raise GuestsError(
                "Your Hermes model isn't connected for sharing. Reconnect it through Hermes's normal model setup, then send /guests_setup."
            ) from exc
        allowed = {"model", "base_url", "api_key", "provider", "api_mode"}
        if not isinstance(runtime, dict) or set(runtime) - allowed or not runtime.get("model"):
            raise GuestsError("Choose a working native Hermes API model before sharing.")
        if runtime.get("api_mode") is not None and runtime["api_mode"] not in NATIVE_MODES:
            raise GuestsError(
                "This model uses an external agent runtime that Hermes Guests cannot isolate. Choose a native API model for sharing; your existing permissions are unchanged."
            )
        if runtime.get("provider") == CODEX_PROVIDER and (
            runtime.get("api_key") or runtime.get("base_url")
        ):
            raise GuestsError("Sharing requires the Hermes-hosted Codex connection.")
        return runtime

    def check_readiness(self):
        self.model_route()
        import importlib.util

        if importlib.util.find_spec("run_agent") is None:
            raise GuestsError(
                "The native Hermes agent runtime is missing. Update Hermes before enabling sharing."
            )
        return {"status": "ready"}

    def run(self, actor, share_id, query, *, delegation=None):
        record = self.service.admit_turn(actor, share_id, delegation=delegation)
        if record["scope"].get("chat_only"):
            self.service.admit_chat(actor, share_id)
        if record["scope"].get("guest_memory") and not delegation:
            for target in ("memory", "user"):
                self.service.admit_memory(actor, share_id, target, operation="read")
        if not isinstance(query, str) or not 1 <= len(query) <= 12000:
            raise GuestsError("Send a message of at most 12,000 characters.")
        active_key = (share_id, delegation)
        with self.lock:
            if self.closed:
                raise GuestsError("Sharing is restarting. Please try again shortly.")
            if active_key in self.busy:
                raise GuestsError("I'm still working on your previous message. Please wait.")
            if len(self.busy) >= 8:
                raise GuestsError("Sharing is busy. Please try again shortly.")
            self.busy.add(active_key)
        token = None
        proc = None
        try:
            home = self.context.copy().run(in_own_profile, self.ensure_profile, share_id)
            runtime = self.model_route()
            codex_model = runtime["model"] if runtime.get("provider") == CODEX_PROVIDER else None
            token = self.broker.open(
                actor, share_id, delegation, codex_model=codex_model, revision=record["revision"]
            )
            if codex_model:
                runtime = {
                    **runtime,
                    "provider": "custom",
                    "base_url": self.broker.url,
                    "api_key": token,
                }
            delegated = self.service.delegates.get(delegation) if delegation else None
            access = delegated["scope"] if delegated else record["scope"]
            from .permissions import proxy_schema
            from .schemas import GUEST_SCHEMAS

            never = self.service.never()  # a tool listed since the grant is not shown to the guest
            schemas = {
                e["name"]: proxy_schema(e)
                for e in access.get("tools", [])
                if not never.blocks_tool(e["name"])
            }
            for name, schema in GUEST_SCHEMAS.items():
                if never.blocks_tool(name):
                    continue
                if (name.startswith("ha_") and access.get("lights")) or (
                    name.startswith("web_") and access.get("research")
                ):
                    schemas[name] = copy.deepcopy(schema)
                    if name.startswith("ha_"):
                        schemas[name]["parameters"]["properties"]["entity_id"]["enum"] = access[
                            "lights"
                        ]
            payload = {
                "home": str(home),
                "actor": str(actor),
                "share_id": share_id,
                "query": query,
                "session_id": delegation
                or ("guests-" + share_id + "-c" + str(record.get("context_epoch", 1))),
                "broker_url": self.broker.url,
                "broker_token": token,
                "runtime": runtime,
                "scope": access,
                "catalog": {
                    k: v for k, v in self.service.catalog.items() if k in access.get("lights", [])
                },
                "tool_schemas": schemas,
                "delegation": bool(delegation),
                # Times the model can say in the reader's own clock, and when this access
                # last changed, so refusals from before a renewal are not taken as current.
                "zone": self.service.zone_of(record),
                "zone_is_theirs": bool(record.get("guest_zone")),
                "now": self.service.clock(),
                "changed_at": record.get("changed_at") or record.get("updated_at"),
                "guest_memory": bool(record["scope"].get("guest_memory")) and not delegation,
            }
            # API keys and turn tokens travel only through the worker pipe.
            keep = {
                "PATH",
                "TMPDIR",
                "TEMP",
                "TMP",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "PYTHONPATH",
            }
            env = {k: v for k, v in os.environ.items() if k in keep}
            project = Path(__file__).resolve().parent.parent
            if (project / "plugin.yaml").is_file():
                env["PYTHONPATH"] = str(project) + os.pathsep + env.get("PYTHONPATH", "")
            env.update(HOME=str(home), HERMES_HOME=str(home), HERMES_GUESTS_WORKER="1")
            project = str(Path(__file__).resolve().parent.parent)
            env["PYTHONPATH"] = project + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.Popen(
                [sys.executable, "-m", "hermes_guests.worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                cwd=home,
            )
            with self.lock:
                self.processes[active_key] = proc
                if self.closed:
                    proc.terminate()
            current = self.service.get_access(actor, share_id)
            if not self.service._fresh(current) or current["revision"] != record["revision"]:
                proc.terminate()
            try:
                stdout, _stderr = proc.communicate(json.dumps(payload), timeout=95)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            if proc.returncode:
                # The guest is told only that the turn failed; the owner's log says why.
                logging.getLogger(__name__).warning(
                    "Guest worker for %s exited with %s: %s",
                    share_id,
                    proc.returncode,
                    " ".join((_stderr or "").split())[-800:],
                )
                current = self.service.get_access(actor, share_id)
                if not self.service._fresh(current) or current["revision"] != record["revision"]:
                    raise GuestsError(
                        "Your access changed while I was working. Use /access to check it."
                    )
                raise GuestsError(
                    "The guest agent couldn't complete this turn. Ask the owner to check sharing readiness."
                )
            result = json.loads(stdout)
            # Never deliver a stale model response after access ended or changed.
            current = self.service.get_access(actor, share_id)
            if not self.service._fresh(current) or current["revision"] != record["revision"]:
                raise GuestsError(
                    "Your access changed while I was working. Please send the request again."
                )
            if not result.get("ok"):
                raise GuestsError(result.get("error", "The guest agent couldn't finish."))
            return str(result["text"])[:12000]
        except subprocess.TimeoutExpired:
            raise GuestsError("This turn reached its time limit. No access was changed.")
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            if token:
                self.broker.close_token(token)
            with self.lock:
                self.busy.discard(active_key)
                self.processes.pop(active_key, None)

    def cancel_share(self, share_id):
        with self.lock:
            for (sid, _), proc in list(self.processes.items()):
                if sid == share_id and proc.poll() is None:
                    proc.terminate()

    def close(self):
        with self.lock:
            self.closed = True
            for proc in self.processes.values():
                if proc.poll() is None:
                    proc.terminate()

    def reset_context(self, actor, share_id):
        with self.service.ledger.transaction() as db:
            record = self.service._record(db, share_id)
            if record["guest_id"] != str(actor):
                raise GuestsError("This isn't your conversation.")
            record["context_epoch"] = record.get("context_epoch", 1) + 1

    def cleanup(self, actor, *, grace_seconds=None):
        """Remove only owned, idle, ended profiles; never recreate a profile to delete it."""
        from .model import timestamp

        self.service.owner(actor)
        removed = []
        if grace_seconds is None:
            grace_seconds = self.service.data_settings(actor)["retention_seconds"]
        for record in self.service.list_access(actor):
            sid = record["id"]
            ended = record.get("revoked_at", 0) or timestamp(record["scope"]["expires_at"])
            wait = 0 if record.get("data_deletion_requested_at") is not None else grace_seconds
            if wait is None:
                continue
            if self.service._fresh(record) or self.service.clock() < ended + wait:
                continue
            with self.service.ledger.transaction() as db:
                current = self.service._record(db, sid)
                current_end = current.get("revoked_at", 0) or timestamp(
                    current["scope"]["expires_at"]
                )
                wait = 0 if current.get("data_deletion_requested_at") is not None else grace_seconds
                if self.service._fresh(current) or self.service.clock() < current_end + wait:
                    continue
                with self.profile_lock, self.lock:
                    if any(key[0] == sid for key in self.busy):
                        continue
                    name = "guests-" + sid.replace("_", "-")
                    if self.profile_resolver:
                        home = Path(self.profile_resolver(name))
                    elif self.profile_factory:
                        home = self.profiles.get(sid)
                        if home is None:
                            continue
                    else:
                        from .compat import guest_profile_dir

                        home = self.context.copy().run(in_own_profile, guest_profile_dir, name)
                    if not home.exists() and not home.is_symlink():
                        self.profiles.pop(sid, None)
                        current.setdefault("profile_deleted_at", self.service.clock())
                        continue
                    marker = home / ".hermes-guests-profile"
                    if (
                        home.is_symlink()
                        or marker.is_symlink()
                        or not marker.is_file()
                        or marker.read_text() != sid
                    ):
                        self.service.audit.event(
                            "cleanup_blocked", share_id=sid, reason="profile-ownership"
                        )
                        continue
                    if self.profile_deleter:
                        self.profile_deleter(name)
                    else:
                        from .compat import delete_guest_profile

                        self.context.copy().run(in_own_profile, delete_guest_profile, name)
                    self.profiles.pop(sid, None)
                    current["profile_deleted_at"] = self.service.clock()
                self.service.audit.event("profile_removed", share_id=sid)
                removed.append(sid)
        return removed
