"""Trusted local access lifecycle. Models prepare; authenticated humans confirm."""

from __future__ import annotations

import base64
import copy
import hashlib
import logging
import secrets
import threading
import time
import uuid
from pathlib import Path

from hermes_tenuo import HermesGuard
from tenuo import (
    CEL,
    MAX_WARRANT_SIZE,
    OneOf,
    Range,
    SignedRevocationList,
    SigningKey,
    SrlBuilder,
    Warrant,
)

from .audit import Audit
from .model import (
    CHAT_ONLY_CAPABILITY,
    GUEST_MEMORY_CAPABILITY,
    MAX_ACCESS_SECONDS,
    GuestsError,
    digest,
    iso,
    length_text,
    review,
    scope,
    timestamp,
    when,
)
from .permissions import (
    compile_constraints,
    default_arguments,
    normalize_tools,
    tool_selections,
    validate_call,
)
from .store import Ledger, atomic_private

URL_RULE = 'value.size() >= 1 && value.size() <= 5 && value.all(u, u.startsWith("https://") || u.startsWith("http://"))'
SERVICE_TOOLS = {"ha_get_state", "ha_call_service", "web_search", "web_extract"}
# The owner's own signed grant lasts a week and is reissued for the same tools before it
# runs out, so the owner is not asked to repeat setup.
MORE_PER_DAY = 3  # declined requests for more after which a guest waits a day


def open_request(record):
    """A guest's unanswered request for more access, or {} (also for a marker in any other form)."""
    request = record.get("more_open")
    return request if isinstance(request, dict) and request.get("token") else {}


def spent(invitation):
    """True once an invitation has given someone access. It can never be replaced or reused,
    whatever happened to that access afterwards."""
    return invitation.get("status") == "used" or bool(invitation.get("share_id"))


def signed_permissions(warrant):
    """What a signed grant allows, in a form two grants can be compared by: each tool with
    each input's limit. The saved record is not authority; this is."""
    return sorted(
        (tool, sorted((field, str(limit)) for field, limit in (limits or {}).items()))
        for tool, limits in dict(warrant.capabilities).items()
    )


def same_key(one, other):
    """True when two public keys are the same key."""
    try:
        return bytes(one.to_bytes()) == bytes(other.to_bytes())
    except Exception:
        return False


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def load_key(path, *, create=True):
    path = Path(path)
    if path.is_symlink():
        raise GuestsError(
            "A saved key path is unsafe. Sharing is paused until the owner repairs it."
        )
    if path.exists():
        try:
            return SigningKey.from_bytes(base64.b64decode(path.read_bytes(), validate=True))
        except Exception as exc:
            raise GuestsError(
                "A saved identity could not be verified. Review or recover access before continuing."
            ) from exc
    if not create:
        raise GuestsError("A saved identity is missing. Owner-controlled recovery is required.")
    key = SigningKey.generate()
    atomic_private(path, base64.b64encode(key.secret_key_bytes()))
    return key


def preset_tools(access):
    """The input permissions shared by preset compilation and child narrowing."""
    result = []
    if access.get("lights"):
        entities = {"one_of": access["lights"]}
        result += [
            {"name": "ha_get_state", "constraints": {"entity_id": entities}},
            {
                "name": "ha_call_service",
                "constraints": {
                    "domain": {"equals": "light"},
                    "service": {"one_of": ["turn_on", "turn_off"]},
                    "entity_id": entities,
                },
            },
        ]
    if access.get("research"):
        result.append(
            {
                "name": "web_search",
                "constraints": {"query": {"any": True}, "limit": {"range": [1, 5]}},
            }
        )
    # Page extraction uses a CEL URL predicate, which generic JSON rules cannot
    # represent as a parent permission. It remains available via the research
    # preset; never project that predicate as unrestricted input access.
    return result


OWNER_SECONDS = 7 * 86400
OWNER_RENEW = 3 * 86400

STANDING_MARK = "guests_no_end_date"
# Who a grant is for is signed into it as a name that carries the Telegram ID, so that the
# person rests on what was approved and not on a field in the saved record. Like the mark
# above it names no tool and is refused if anything tries to call it. Grants issued before
# this existed carry no such name and are bound by their record alone, as they were.
GUEST_MARK = "guests_guest_"
GUEST_MARKS = (GUEST_MARK, "share_guest_")


def signed_guest(warrant):
    """The Telegram ID a grant was signed for, or None for a grant from before that was done."""
    return next(
        (
            name[len(prefix) :]
            for name in dict(warrant.capabilities)
            for prefix in GUEST_MARKS
            if name.startswith(prefix)
        ),
        None,
    )


def compile_scope(access, holder, issuer, *, now, parent=None, guest=None):
    seconds = int(timestamp(access["expires_at"]) - now)
    if seconds < 1:
        raise GuestsError("Access has expired.")
    builder = parent.grant_builder() if parent is not None else Warrant.mint_builder()
    builder = builder.holder(holder.public_key).ttl(seconds)
    if access.get("guest_memory") and parent is None:
        builder = builder.capability(
            GUEST_MEMORY_CAPABILITY,
            target=OneOf(["memory", "user"]),
            operation=OneOf(["read", "write"]),
        )
    if access.get("chat_only"):
        builder = builder.capability(CHAT_ONLY_CAPABILITY)
    for entry in preset_tools(access):
        builder = builder.capability(entry["name"], **compile_constraints(entry))
    if access["research"]:
        builder = builder.capability(
            "web_extract", urls=CEL(URL_RULE), char_limit=Range(2000, 20000)
        )
    for entry in access.get("tools", []):
        builder = builder.capability(entry["name"], **compile_constraints(entry))
    if access.get("until_ended") and parent is None:
        # Signed into the grant, so that continuing access with no end date rests on what
        # the owner approved and not on a field in the saved record. It names no tool and
        # is refused if anything tries to call it.
        builder = builder.capability(STANDING_MARK)
    if guest is not None and parent is None:
        builder = builder.capability(GUEST_MARK + str(guest))
    return builder.grant(issuer) if parent is not None else builder.mint(issuer)


class GuestsService:
    def __init__(
        self,
        state,
        owner_id,
        *,
        catalog=None,
        dispatch=None,
        clock=time.time,
        timezone="UTC",
        tool_resolver=None,
        review_seconds=600,
        invitation_seconds=86400,
        claim_seconds=86400,
        guest_data_retention_seconds=None,
        activity_retention_days=90,
        guest_names="yours",
    ):
        if not str(owner_id).isdigit():
            raise GuestsError("Sharing needs a verified Telegram owner before setup.")
        # A review is answered now or not at all. An invitation and the owner's account
        # check may wait for someone arriving at the weekend, but never past a fixed end.
        for name, seconds, longest in (
            ("review_seconds", review_seconds, 86400),
            ("invitation_seconds", invitation_seconds, MAX_ACCESS_SECONDS),
            ("claim_seconds", claim_seconds, MAX_ACCESS_SECONDS),
        ):
            if type(seconds) is not int or not 60 <= seconds <= longest:
                raise GuestsError(f"{name} must be between 60 and {longest} seconds.")
            setattr(self, name, seconds)
        if guest_data_retention_seconds is not None and (
            type(guest_data_retention_seconds) is not int
            or not 0 <= guest_data_retention_seconds <= 365 * 86400
        ):
            raise GuestsError("Guest data retention must be between zero and 365 days.")
        self.default_retention_seconds = guest_data_retention_seconds
        if guest_names not in {"yours", "telegram"}:
            raise GuestsError("guest_names must be yours or telegram.")
        # Whose name for a guest the owner sees: the one the owner gave them, or the name
        # on their Telegram account once they join.
        self.guest_names = guest_names
        self.tool_resolver = tool_resolver or (lambda _name: None)
        self.owner_id = str(owner_id)
        self.ledger = Ledger(state)
        self.root = self.ledger.root
        self.clock = clock
        self.zone = timezone
        self.catalog = catalog or self.ledger.snapshot()["setup"].get("catalog", {})
        self.dispatch = dispatch
        initialized = self.ledger.snapshot()["setup"].get("initialized", False)
        self.issuer = load_key(self.root / "control" / "issuer.key", create=not initialized)
        self.audit_key = load_key(self.root / "control" / "audit.key", create=not initialized)
        if type(activity_retention_days) is not int or not 1 <= activity_retention_days <= 3650:
            raise GuestsError("Activity retention must be between 1 and 3650 days.")
        self.audit = Audit(
            self.root, self.audit_key, self.issuer.public_key, activity_retention_days
        )
        self.guard = HermesGuard(
            trusted_roots=[self.issuer.public_key],
            require_session_warrant=True,
            on_denial="block",
        )
        self.delegates = {}
        self.in_flight = {}
        self.in_flight_lock = threading.Lock()
        self.runner = None
        with self.ledger.transaction() as db:
            setup = db["setup"]
            if setup and setup.get("owner_id") != self.owner_id:
                raise GuestsError(
                    "Saved owner identity differs; owner-controlled recovery is required."
                )
            setup.update(owner_id=self.owner_id, initialized=True)
            if not (self.root / "revocations.srl").exists():
                if initialized:
                    raise GuestsError("Current revocation data is missing. Guest access is paused.")
                atomic_private(
                    self.root / "revocations.srl",
                    SrlBuilder().version(1).build(self.issuer).to_bytes(),
                )
            self._refresh_srl(db)

    def _refresh_srl(self, db):
        srl = SignedRevocationList.from_bytes((self.root / "revocations.srl").read_bytes())
        srl.verify(self.issuer.public_key)
        if srl.version < db["setup"].get("srl_version", 0):
            raise GuestsError("Revocation data rolled back. Guest access is paused.")
        db["setup"]["srl_version"] = srl.version
        self.audit.authorizer.set_revocation_list(srl)
        self.audit.issuer.bind_authorizer(self.audit.authorizer)
        return srl

    def _revoke_warrant(self, db, warrant_id):
        srl = self._refresh_srl(db)
        new = (
            SrlBuilder()
            .from_existing(srl)
            .version(srl.version + 1)
            .revoke(warrant_id)
            .build(self.issuer)
        )
        atomic_private(self.root / "revocations.srl", new.to_bytes())
        self._refresh_srl(db)

    def owner_configured(self):
        """Remember that sharing was enabled even after identity recovery."""
        return self.owner_ready() or bool(self.ledger.snapshot()["setup"].get("trust_generation"))

    def owner_ready(self):
        setup = self.ledger.snapshot()["setup"]
        # Earlier versions recorded setup by minting a warrant for the owner.
        return bool(setup.get("sharing_enabled") or setup.get("owner_authority"))

    def _issue_owner(self, db, names):
        holder = load_key(self.root / "control" / "owner.key")
        builder = Warrant.mint_builder().holder(holder.public_key).ttl(OWNER_SECONDS)
        for name in names:
            builder = builder.capability(name)
        warrant = builder.mint(self.issuer)
        atomic_private(self.root / "warrants" / (str(warrant.id) + ".warrant"), warrant.to_bytes())
        previous = db["setup"].get("owner_authority")
        if previous:
            self._revoke_warrant(db, previous["warrant_id"])
        db["setup"]["owner_authority"] = {
            "warrant_id": str(warrant.id),
            "expires_at": timestamp(warrant.expires_at()),
            "artifact_hash": hashlib.sha256(warrant.to_bytes()).hexdigest(),
            "tools": sorted(names),
        }

    def _keep_owner(self, db):
        """Issue the owner's next signed grant when the current one is running out, for the
        same tools the owner accepted at setup. A tool enabled since then still needs
        /guests_setup, and so does a missing or altered key or grant."""
        authority = db["setup"].get("owner_authority")
        if not authority or not authority.get("tools"):
            return False
        if authority["expires_at"] - self.clock() > OWNER_RENEW:
            return False
        try:
            current = self._warrant(authority)
            if not current.verify(self.issuer.public_key.to_bytes()):
                return False
            key = load_key(self.root / "control" / "owner.key", create=False)
            # A different key in its place (a restore, a swap) is not continued: the owner
            # confirms setup again. Neither is a grant that was revoked.
            if not same_key(current.authorized_holder, key.public_key):
                return False
            if not same_key(current.issuer, self.issuer.public_key):
                return False
            if self._refresh_srl(db).is_revoked(str(current.id)):
                return False
            # The tools come from the signed grant the owner accepted at setup, not from
            # the saved list beside it, so nothing can be added by editing the record.
            accepted = sorted(str(name) for name in current.tools)
        except Exception:
            return False
        if not accepted:
            return False
        self._issue_owner(db, accepted)
        self.audit.event("owner_access_continued", share_id="owner", actor=self.owner_id)
        return True

    def setup_owner(self, actor, tools):
        self.owner(actor)
        if not isinstance(tools, dict) or not tools or len(tools) > 500:
            raise GuestsError("Review the owner's current enabled tools before setup.")
        with self.ledger.transaction() as db:
            self._issue_owner(db, list(tools))
            db["setup"]["sharing_enabled"] = True
        return {"status": "ready"}

    def check_owner(self, actor, tool, args, session_id, *, enforce=True):
        self.owner(actor)
        try:
            with (
                self.ledger.transaction() as db
            ):  # saved on its own, whatever the check below decides
                self._keep_owner(db)
        except Exception:  # the check below then asks the owner to set up again
            logging.getLogger(__name__).warning("Could not continue the owner's access")
        with self.ledger.transaction() as db:
            self._refresh_srl(db)
            authority = db["setup"].get("owner_authority")
            if not authority or authority["expires_at"] <= self.clock():
                return {
                    "action": "block",
                    "message": "Owner access needs native setup or renewal. Send /guests_setup.",
                }
            w = self._warrant(authority)
            key = load_key(self.root / "control" / "owner.key", create=False)
            sid = "owner:" + session_id
            self.guard.set_session_warrant(sid, w, key)
            result = self.guard.pre_tool_call(tool, args, session_id=sid)
            if enforce or result is None:
                self.audit.decision(
                    [w],
                    key,
                    tool,
                    args,
                    allowed=result is None,
                    share_id="owner",
                    actor=actor,
                    call_id=uuid.uuid4().hex,
                    now=self.clock(),
                )
            if result is not None:
                # A model that adds a prefix to a real tool's name gets the name back, not
                # advice to redo setup. Hermes hands over its own "tool_call" wrapper when the
                # name inside it matches no tool.
                asked = [str(tool)]
                if tool == "tool_call" and isinstance(args, dict):
                    calls = args.get("calls") if isinstance(args.get("calls"), list) else [args]
                    asked = [str(c.get("name", "")) for c in calls if isinstance(c, dict)]
                for name in asked:
                    bare = name.replace(":", ".").rsplit(".", 1)[-1]
                    if bare != name and bare in authority.get("tools", []):
                        return {
                            "action": "block",
                            "message": "No tool is named "
                            + name[:80]
                            + ". Call it by its exact name: "
                            + bare
                            + ".",
                        }
                if tool == "tool_call":
                    return {
                        "action": "block",
                        "message": "No tool has that name. Use tool_search to find the exact name, then call it without a prefix.",
                    }
                return {
                    "action": "block",
                    "message": "Your enabled tools changed. Send /guests_setup to review your current owner access.",
                }
            return result

    def owner(self, actor):
        if str(actor) != self.owner_id:
            raise GuestsError("Only the owner can manage shared access.")

    def normalize_scope(self, value, *, parent=None, saved=False, check_never=True):
        if not isinstance(value, dict):
            raise GuestsError("Choose the person's access and an end time.")
        value = copy.deepcopy(value)
        # New owner reviews include private guest learning. Saved warrants keep exactly
        # their signed permissions until the owner approves a replacement.
        value.setdefault("guest_memory", not saved and parent is None)
        bindings = value.pop("preset_schemas", {})
        if bindings and not saved:
            raise GuestsError(
                "Use discovered tools or a resource preset; saved schema bindings are not proposal inputs."
            )
        if not saved and parent is None and value.get("until_ended") and "expires_at" in value:
            raise GuestsError("Choose either an end time or access with no end date, not both.")
        requested = value.get("tools", [])
        if saved:
            for entry in requested:
                current = self.tool_resolver(entry["name"])
                from .permissions import validate_schema

                if not current or digest(validate_schema(current)) != entry["schema_digest"]:
                    raise GuestsError(
                        "A tool changed since this review. Prepare a new access review."
                    )
            requested = tool_selections(requested)
        from .never import NeverShare

        # Repair and renewal check that existing access is intact. The owner's list is not
        # part of that: it is applied to every call, and taking an item off restores access.
        never, self.left_out = (self.never() if check_never else NeverShare()), []
        if never and isinstance(requested, list):
            locked = []
            for entry in requested:
                if isinstance(entry, dict) and isinstance(entry.get("name"), str):
                    if never.blocks_tool(entry["name"]):
                        raise GuestsError(
                            entry["name"]
                            + " is on your never-share list, so it cannot be shared. Only the owner "
                            "changes that list, in /guests_setup or by confirming a change in this chat."
                            if not saved
                            else "A tool in this review is now on your never-share list. Prepare a new access review."
                        )
                    if (
                        not saved
                    ):  # a saved grant keeps its reviewed form; calls are checked when admitted
                        entry, gone = never.lock(entry, self.tool_resolver(entry["name"]))
                        self.left_out += gone
                locked.append(entry)
            requested = locked
        if never:
            lights = [e for e in value.get("lights") or [] if isinstance(e, str)]
            for entity in lights:
                # By its identifier and its name.
                named = [entity, self.catalog.get(entity, {}).get("name", "")]
                found = never.blocked_for("ha_call_service", named) or never.blocked_for(
                    "ha_get_state", named
                )
                if found:
                    raise GuestsError(
                        f"“{found}” is on your never-share list, so that light cannot be shared."
                    )
            # And everything the preset itself fixes or offers, each under the input the
            # tool receives it in: the review promises all of it (reading, on and off), so
            # none of it may be something a call would then be refused for.
            for entry in preset_tools({"lights": lights}) if lights else []:
                for field, rule in entry["constraints"].items():
                    for choice in [rule["equals"]] if "equals" in rule else rule.get("one_of", []):
                        found = never.blocked_for(entry["name"], {field: choice})
                        if found:
                            raise GuestsError(
                                f"“{found}” is on your never-share list for {entry['name']}, so "
                                "lights cannot be shared as a set. Share that tool by itself "
                                "with the inputs you choose."
                            )
            if value.get("research") and (
                never.blocks_tool("web_search") or never.blocks_tool("web_extract")
            ):
                raise GuestsError(
                    "Web search or page reading is on your never-share list, so research cannot be shared."
                )
            if value.get("lights") and (
                never.blocks_tool("ha_get_state") or never.blocks_tool("ha_call_service")
            ):
                raise GuestsError(
                    "The light tools are on your never-share list, so lights cannot be shared."
                )
        parent_tools = None
        if parent is not None:
            from .permissions import validate_schema

            parent_tools = list(parent.get("tools", []))
            names = (
                {
                    e["name"]
                    for e in requested
                    if isinstance(e, dict) and isinstance(e.get("name"), str)
                }
                if isinstance(requested, list)
                else set()
            )
            selected = [e for e in preset_tools(parent) if e["name"] in names]
            for entry in selected:
                actual = self.tool_resolver(entry["name"])
                expected = parent.get("preset_schemas", {}).get(entry["name"])
                if not actual or digest(validate_schema(actual)) != expected:
                    raise GuestsError(
                        "A preset tool changed. The owner needs to review access again."
                    )
            parent_tools += normalize_tools(selected, self.tool_resolver)
        value["tools"] = normalize_tools(requested, self.tool_resolver, parent=parent_tools)
        preset_names = ({"ha_get_state", "ha_call_service"} if value.get("lights") else set()) | (
            {"web_search", "web_extract"} if value.get("research") else set()
        )
        if preset_names & {e["name"] for e in value["tools"]}:
            raise GuestsError(
                "Choose either a resource preset or explicit permissions for the same tool."
            )
        # A length of time ("15 minutes") is what the owner approves, and it starts when the
        # grant is signed: on approval, or when the owner confirms a new guest's account.
        # The end shown in a review is where it would fall if approved then.
        duration = value.get("duration_seconds") if parent is None else None
        if saved and duration is not None:
            value.pop("duration_seconds")
            if not value.get("until_ended"):
                value["expires_at"] = iso(self.clock() + duration)
        normalized = scope(value, now=self.clock(), catalog=self.catalog, parent=parent)
        if duration is not None and not normalized.get("until_ended"):
            normalized["duration_seconds"] = duration
        current_bindings = {}
        from .permissions import validate_schema

        for name in preset_names:
            actual = self.tool_resolver(name)
            if not actual:
                raise GuestsError(
                    "A preset tool is unavailable. Connect it before preparing access."
                )
            current_bindings[name] = digest(validate_schema(actual))
        if saved and current_bindings != bindings:
            raise GuestsError(
                "A preset tool changed since the review. Discover and review access again."
            )
        normalized["preset_schemas"] = current_bindings
        return normalized

    NOTICE_MARKS = {
        "ending": "ending_notice",
        "ended": "ended_notice",
        "owner_ended": "owner_notice",
    }

    def due_notices(self, warn_seconds=180):
        """Notices not yet delivered: the guest's warning before access ends, the guest's
        notice that it ended, and the owner's. Each is sent once per grant, and only marked
        by ack_notice after it was delivered, so a failed send is tried again on the next
        pass. Access of five minutes or less gets no warning."""
        now, due = self.clock(), []
        for r in self.ledger.snapshot()["shares"].values():
            if r.get("status") != "active" or r["scope"].get("until_ended"):
                continue
            left = timestamp(r["scope"]["expires_at"]) - now
            grant = r["warrant_id"]
            if (
                0 < left <= warn_seconds
                and r.get("ending_notice") != grant
                # Short access (five minutes or less from when this grant started) gets
                # only the notice that it ended.
                and timestamp(r["scope"]["expires_at"]) - r.get("updated_at", 0) > 300
            ):
                due.append(("ending", copy.deepcopy(r)))
            elif left <= 0 and (r.get("notices") or left > -3600):
                # Kept until delivered; grants from before delivery was tracked only
                # within the first hour, so an upgrade doesn't announce long-past ends.
                for kind in ("ended", "owner_ended"):
                    if r.get(self.NOTICE_MARKS[kind]) != grant:
                        due.append((kind, copy.deepcopy(r)))
        return due

    def notice_still_due(self, kind, record):
        """Whether a notice read a moment ago still applies: the same grant, not since
        renewed or changed, and not already delivered."""
        r = self.ledger.snapshot()["shares"].get(record["id"])
        if kind == "ending" and timestamp(record["scope"]["expires_at"]) <= self.clock():
            return False  # too late to warn; the notice that it ended follows
        return bool(
            r
            and r["warrant_id"] == record["warrant_id"]
            and r.get("status") == "active"
            and r["scope"].get("expires_at") == record["scope"].get("expires_at")
            and r.get(self.NOTICE_MARKS[kind]) != record["warrant_id"]
        )

    def ack_notice(self, kind, record):
        """A notice from due_notices was delivered: it is not sent again for this grant."""
        with self.ledger.transaction() as db:
            r = db["shares"].get(record["id"])
            if r and r["warrant_id"] == record["warrant_id"]:
                r[self.NOTICE_MARKS[kind]] = record["warrant_id"]

    def default_invitation_seconds(self, access):
        """How long the invitation link works unless the owner says otherwise: until access
        ends when it has a fixed end ("until midnight"), else the configured window (a day
        by default). The guest's time only starts once the owner confirms them, so a late
        open costs nothing."""
        if (
            access.get("expires_at")
            and not access.get("duration_seconds")
            and not access.get("until_ended")  # its expires_at is only the next renewal
        ):
            left = int(timestamp(access["expires_at"]) - self.clock())
            return max(60, min(left, MAX_ACCESS_SECONDS))
        return self.invitation_seconds

    @staticmethod
    def _latest_start(access):
        """The last moment reviewed access can still begin. A length of time starts when it
        is signed, so only a fixed end time limits it."""
        if access.get("duration_seconds") is not None or access.get("until_ended"):
            return float("inf")
        return timestamp(access["expires_at"])

    def _fresh(self, record):
        # Access with no end date stays active; its signed grant is issued again as needed.
        return record.get("status") == "active" and (
            bool(record["scope"].get("until_ended"))
            or timestamp(record["scope"]["expires_at"]) > self.clock()
        )

    def _keep_standing(self, db, record):
        """Issue the next signed grant for access with no end date, when the current one is
        running out. Same person, same holder key, same permissions the owner reviewed:
        only the grant's own end moves. Never for ended, changed or time-limited access."""
        from .model import STANDING_RENEW, STANDING_SECONDS

        scope_now = record["scope"]
        if record.get("status") != "active" or not scope_now.get("until_ended"):
            return False
        if timestamp(scope_now["expires_at"]) - self.clock() > STANDING_RENEW:
            return False
        try:
            # Only access that still verifies is continued: the saved grant matches its
            # record, was issued to this guest's key, has not been revoked, and its tools
            # are as they were reviewed. Anything else is left for the owner to repair.
            current = self._warrant(record)
            if not current.verify(self.issuer.public_key.to_bytes()):
                return False
            holder = load_key(self.root / "control" / (record["id"] + ".key"), create=False)
            if not same_key(current.authorized_holder, holder.public_key):
                return False
            if not same_key(current.issuer, self.issuer.public_key):
                return False
            if self._refresh_srl(db).is_revoked(str(current.id)):
                return False
            self.normalize_scope(scope_now, saved=True, check_never=False)
            # Only the end may move: the saved permissions must compile to exactly what the
            # grant being replaced signed. That includes its no-end-date mark, so access
            # that was signed with a time limit is never continued, whatever the record says.
            access = dict(scope_now, expires_at=iso(self.clock() + STANDING_SECONDS))
            # The person it was signed for carries over as it is: a grant from before people
            # were signed in stays as it was, and is not signed for whoever the record names.
            person = signed_guest(current)
            probe = compile_scope(
                access, holder, SigningKey.generate(), now=self.clock(), guest=person
            )
            if signed_permissions(probe) != signed_permissions(current):
                return False
        except Exception:
            return False
        warrant = compile_scope(access, holder, self.issuer, now=self.clock(), guest=person)
        wire = warrant.to_bytes()
        atomic_private(self.root / "warrants" / (str(warrant.id) + ".warrant"), wire)
        replaced = record["warrant_id"]
        record["old_warrants"] = list(record.get("old_warrants", [])) + [replaced]
        record["scope"] = access
        record["warrant_id"] = str(warrant.id)
        record["artifact_hash"] = hashlib.sha256(wire).hexdigest()
        record["updated_at"] = self.clock()
        self.audit.event(
            "standing_access_continued",
            share_id=record["id"],
            actor=self.owner_id,
            revision=record["revision"],
            warrant_id=str(warrant.id),
        )
        # Subtasks were issued under the grant being replaced; they end with it.
        for key, value in list(self.delegates.items()):
            if value["share_id"] == record["id"]:
                self.guard.clear_session_warrant(key)
                self.delegates.pop(key, None)
        self._revoke_warrant(db, replaced)  # last: nothing after it can fail and undo the rest
        return True

    def _continue(self, share_id, actor):
        """Continue access with no end date in a step of its own. A request that is then
        refused must not leave the old grant revoked and the new one unsaved."""
        try:
            with self.ledger.transaction() as db:
                record = db["shares"].get(share_id)
                if record and record.get("guest_id") == str(actor):
                    self._keep_standing(db, record)
        except Exception:  # the request itself then reports what is wrong with the access
            logging.getLogger(__name__).warning("Could not continue access %s", share_id)

    def _record(self, db, share_id):
        record = db["shares"].get(share_id)
        if not record:
            raise GuestsError("Access was not found.")
        return record

    def _warrant(self, record):
        path = self.root / "warrants" / (record["warrant_id"] + ".warrant")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != record["artifact_hash"]:
            raise GuestsError("Saved access could not be verified. Guest actions are paused.")
        warrant = Warrant.from_bytes(raw)
        named = signed_guest(warrant)
        if named is not None and named != str(record.get("guest_id")):
            # The record says this grant is someone else's than the person it was signed for.
            raise GuestsError("Saved access could not be verified. Guest actions are paused.")
        return warrant

    def _same_account_or_end(self, record, action):
        """A change is signed for the account on the saved record, and deletion removes
        everything that account has. Neither is done when the access was signed for someone
        else: the record is not to be trusted, and repair cannot say whose it is. Ending it
        always works, and the person can then be invited again."""
        if action in {"replace", "delete"} and self._signed_for_another(record):
            raise GuestsError(
                "The saved access for "
                + str(record.get("label") or "this person")
                + " names a different account than it was approved for. Nothing was changed. "
                + (
                    "Deleting by account could remove someone else's data. "
                    if action == "delete"
                    else ""
                )
                + "End this access, then send a new invitation."
            )

    def _of_account(self, db, guest_id):
        """The access records that are one account's: those that name it, less any whose
        access was signed for someone else. Deleting a person's data goes by this, so a
        record that only names their account is not swept up with theirs."""
        return [
            r
            for r in db["shares"].values()
            if r["guest_id"] == guest_id and not self._signed_for_another(r)
        ]

    def _signed_for_another(self, record):
        """True when the saved grant was signed for a different person than the record names."""
        try:
            raw = (self.root / "warrants" / (record["warrant_id"] + ".warrant")).read_bytes()
            named = signed_guest(Warrant.from_bytes(raw))
        except Exception:  # a missing or unreadable grant is for repair to report, not this
            return False
        return named is not None and named != str(record.get("guest_id"))

    def prepare(
        self,
        actor,
        label,
        access,
        *,
        share_id=None,
        action="replace",
        invitation_seconds=None,
        revision=None,
        retention_seconds=None,
        renewal=None,
        more_seconds=None,
        guest_zone=None,
    ):
        self.owner(actor)
        if guest_zone is not None:
            guest_zone = self.valid_zone(guest_zone)
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise GuestsError("Give this person a short name.")
        if action not in {"replace", "revoke", "delete", "retention"}:
            raise GuestsError("Unknown access change.")
        if action == "retention":
            if share_id or (
                retention_seconds is not None
                and (
                    type(retention_seconds) is not int or not 0 <= retention_seconds <= 365 * 86400
                )
            ):
                raise GuestsError("Choose guest data retention between zero and 365 days.")
        elif action in {"revoke", "delete"} and not share_id:
            raise GuestsError("Choose whose access to end.")
        if invitation_seconds is not None:
            if (
                type(invitation_seconds) is not int
                or not 60 <= invitation_seconds <= MAX_ACCESS_SECONDS
            ):
                raise GuestsError("Choose an invitation window between a minute and 30 days.")
            if share_id or action == "revoke":
                raise GuestsError("Only new invitations need an acceptance window.")
        with self.ledger.transaction() as db:
            old = self._record(db, share_id) if share_id else None
            if old and revision is not None and old["revision"] != revision:
                # The caller built this change from an earlier state of the access.
                raise GuestsError(
                    old["label"] + "'s access changed in the meantime. Open the change again."
                )
            if old:
                # Said before a review is shown, not only when it is approved: a review
                # that can only be refused sends the owner round in a circle.
                self._same_account_or_end(old, action)
                label = old["label"]
                if old.get("data_deletion_requested_at") is not None and action == "replace":
                    raise GuestsError(
                        "This guest data was marked for deletion. Create a new invitation instead."
                    )
            normalized = self.normalize_scope(access) if action == "replace" else None
            if normalized:
                self._representable(normalized)
            pid = secrets.token_urlsafe(12)
            proposal = {
                "id": pid,
                "label": label.strip(),
                "scope": normalized,
                **({"retention_seconds": retention_seconds} if action == "retention" else {}),
                "action": action,
                "share_id": share_id,
                "revision": old["revision"] if old else 0,
                "valid_until": self.clock() + self.review_seconds,
                "status": "pending",
                **(
                    {
                        "invitation_seconds": invitation_seconds
                        or self.default_invitation_seconds(normalized)
                    }
                    if not share_id and action == "replace"
                    else {}
                ),
            }
            if more_seconds:  # time added to the current end, said as such on the review
                proposal["more_seconds"] = int(more_seconds)
            if guest_zone:
                proposal["guest_zone"] = guest_zone
            if old:
                # A review of someone's existing access is for that account and no other,
                # whatever the saved record names by the time it is approved.
                proposal["reviewed_for"] = str(old["guest_id"])
            left_out = list(getattr(self, "left_out", [])) if normalized else []
            if left_out:  # part of what the owner reviewed, so it is kept with the review
                proposal["left_out"] = left_out
            if renewal is not None:
                proposal["renewal"] = True  # asked for by the guest; counted against their limit
                proposal["renewal_asked"] = renewal  # when, so taking it back uncounts this one
            if old and action == "replace" and not self._fresh(old):
                # Their access has ended or run out. Approving switches it back on for the
                # same Telegram account, with no invitation and no account confirmation, so
                # the review has to say whose account that is.
                proposal["returns_access_to"] = {
                    "guest_id": str(old["guest_id"]),
                    "name": " ".join(str(old.get("telegram_name") or "").split())[:80],
                    "username": " ".join(str(old.get("telegram_username") or "").split())[:40],
                    "ended": old["status"] == "revoked",
                }
            if action == "delete":
                proposal["guest_revisions"] = {
                    r["id"]: r["revision"] for r in self._of_account(db, old["guest_id"])
                }
            if action == "retention":
                proposal["policy_revision"] = db["setup"].get("data_policy_revision", 0)
            # The order reviews were prepared in, so a newer one replaces an older one and
            # never the other way round, whatever order their cards are shown in.
            db["setup"]["proposal_seq"] = db["setup"].get("proposal_seq", 0) + 1
            proposal["seq"] = db["setup"]["proposal_seq"]
            proposal["digest"] = digest(proposal)
            db["proposals"][pid] = proposal
            text = self.proposal_review(proposal)
            return {
                "proposal_id": pid,
                "text": text,
                "expires_at": proposal["valid_until"],
                "left_out": left_out,
            }

    def proposal_review(self, proposal):
        if proposal["action"] == "retention":
            seconds = proposal["retention_seconds"]
            if seconds is None:
                return "Keep guest data until you delete it?\n\nConversations and private memories stay available for later owner-approved access. Ending access still blocks model use and shared services. Security and audit records are retained separately."
            return (
                "Guest data retention\n\nKeep conversations and private memories for "
                + self.retention_text(seconds)
                + " after access ends?\n\n"
                "Applies to existing ended profiles too. Shortening this can delete eligible data at the next cleanup. "
                "Security and audit records remain separate."
            )
        if proposal["action"] == "delete":
            return (
                "End access and delete data for " + proposal["label"] + "?\n\n"
                "• End all of this guest's access and subtasks.\n"
                "• Delete their conversations, saved preferences and guest profiles.\n\n"
                "This cannot be undone. Actions already in progress may still finish. "
                "Security and audit records are retained."
            )
        if proposal["action"] == "revoke":
            return (
                "End all access for "
                + proposal["label"]
                + "?\n\nThey will no longer be able to chat or use shared services. Their conversations and saved preferences follow your guest data retention setting."
            )
        # Only the access itself: the invitation and guest data are on the card.
        text = review(proposal["label"], proposal["scope"], self.catalog, self.zone)
        return self.returns_notice(proposal) + text

    @staticmethod
    def returns_notice(proposal):
        """Whose account a review switches access back on for. It stands at the top of
        every message that review can be approved from; empty for any other review."""
        back = proposal.get("returns_access_to")
        if not back:
            return ""
        who = " · ".join(
            part
            for part in (
                back.get("name"),
                "@" + back["username"] if back.get("username") else "",
                "ID " + back["guest_id"],
            )
            if part
        )
        return (
            "This switches access back on for the same Telegram account as before: "
            + who
            + ". Their access "
            + ("was ended" if back.get("ended") else "ran out")
            + ". No invitation is sent and the account is not confirmed again.\n\n"
        )

    def invitation_window_text(self, seconds, access=None):
        """How long the invitation link works, as a time people can read."""
        if access and seconds and self.clock() + seconds >= self._latest_start(access) - 120:
            return (
                "The invitation link works until access ends, "
                + when(self._latest_start(access), self.zone)
                + "."
            )
        return "The invitation link works for " + length_text(seconds) + " after you approve."

    def approve(self, actor, proposal_id):
        self.owner(actor)
        with self.ledger.transaction() as db:
            p = db["proposals"].get(proposal_id)
            if not p:
                raise GuestsError("This review is no longer available.")
            if p["status"] == "approved":
                result = copy.deepcopy(p["result"])
                if result["status"] == "active":
                    r = self._record(db, result["share_id"])
                    if not self._fresh(r) or r["revision"] != result.get("revision", r["revision"]):
                        raise GuestsError(
                            "That review was already applied and access has since changed. Check /guests."
                        )
                return result
            if p["status"] == "replaced":
                raise GuestsError(
                    "A newer review of "
                    + p["label"]
                    + "'s access replaced this one. Use the latest card."
                )
            if p["status"] == "superseded":
                raise GuestsError(
                    "You already approved a newer change to "
                    + p["label"]
                    + "'s access, so this one no longer applies."
                )
            if p["status"] != "pending" or p["valid_until"] <= self.clock():
                raise GuestsError("This review has ended. Ask Hermes to prepare it again.")
            # Digest binds exactly what the owner reviewed, excluding later result/status fields.
            check = {k: v for k, v in p.items() if k != "digest"}
            if digest(check) != p["digest"]:
                raise GuestsError("This review changed. Prepare a new review.")
            old = self._record(db, p["share_id"]) if p["share_id"] else None
            if old and old["revision"] != p["revision"]:
                raise GuestsError(
                    "Access changed since this review. Review the current permissions."
                )
            if old and not p.get("reviewed_for"):
                raise GuestsError(
                    "This review predates account confirmation. Prepare a new review."
                )
            if old and str(old["guest_id"]) != p["reviewed_for"]:
                raise GuestsError(
                    "The account for this access changed since this review. Nothing was changed. Prepare a new review."
                )
            if (
                old
                and p["action"] == "replace"
                and not self._fresh(old)
                and not p.get("returns_access_to")
            ):
                raise GuestsError(
                    "Access ended since this review. Prepare a new review to confirm which account gets access back."
                )
            back = p.get("returns_access_to")
            if back and (not old or str(old["guest_id"]) != str(back["guest_id"])):
                # The review named the account that gets access back. Nothing is signed
                # for any other account, whatever the saved record says now.
                raise GuestsError(
                    "The account for this access changed since this review. Prepare a new review to confirm which account gets access back."
                )
            if old:
                self._same_account_or_end(old, p["action"])
            if p["action"] == "retention":
                if p["policy_revision"] != db["setup"].get("data_policy_revision", 0):
                    raise GuestsError("Guest data retention changed. Review it again.")
                db["setup"]["data_policy_revision"] = p["policy_revision"] + 1
                db["setup"]["guest_data_retention_seconds"] = p["retention_seconds"]
                result = {
                    "status": "retention_updated",
                    "retention_seconds": p["retention_seconds"],
                }
            elif p["action"] == "delete":
                theirs = self._of_account(db, old["guest_id"])
                if {r["id"]: r["revision"] for r in theirs} != p["guest_revisions"]:
                    raise GuestsError("This guest’s access changed. Review deletion again.")
                deleted = []
                for record in theirs:
                    self._revoke_warrant(db, record["warrant_id"])
                    record.update(
                        status="revoked",
                        revoked_at=self.clock(),
                        data_deletion_requested_at=self.clock(),
                        revision=record["revision"] + 1,
                    )
                    # The guest's own words and Telegram name go with their data. Who it was
                    # (the ID) and what you called them stay, for the audit record.
                    for theirs in (
                        "more_open",
                        "more_asked",
                        "telegram_name",
                        "telegram_username",
                        "guest_zone",
                    ):
                        record.pop(theirs, None)
                    self._clear(record["id"])
                    deleted.append(record["id"])
                for invitation in db["invites"].values():
                    # An invitation that was accepted stays spent. Marking it otherwise
                    # would let its old review hand out a fresh link without a new review.
                    if invitation.get("guest_id") == old["guest_id"]:
                        invitation.pop("guest_label", None)
                        invitation.pop("guest_username", None)
                        if not spent(invitation):
                            invitation["status"] = "denied"
                for proposal in db["proposals"].values():
                    back = proposal.get("returns_access_to")
                    if back and back["guest_id"] == old["guest_id"]:
                        back.pop("name", None)
                        back.pop("username", None)
                        # Scrubbing changes the signed review content. Invalidate pending
                        # reviews rather than recomputing their approval digest.
                        if proposal["status"] == "pending":
                            proposal["status"] = "denied"
                result = {
                    "status": "deletion_requested",
                    "share_id": old["id"],
                    "guest_id": old["guest_id"],
                    "shares": deleted,
                }
            elif p["action"] == "revoke":
                self._revoke_warrant(db, old["warrant_id"])
                old.update(status="revoked", revoked_at=self.clock(), revision=old["revision"] + 1)
                with self.in_flight_lock:
                    pending = sum(sid == old["id"] for sid in self.in_flight.values())
                result = {
                    "status": "revoked",
                    "share_id": old["id"],
                    "label": old["label"],
                    "in_flight": pending,
                }
                self._clear(old["id"])
            elif old:
                result = self._activate(db, old["id"], old["guest_id"], p)
            else:
                invite = secrets.token_urlsafe(24)
                db["invites"][token_hash(invite)] = {
                    "proposal_id": proposal_id,
                    "expires_at": min(
                        self.clock() + p.get("invitation_seconds", self.invitation_seconds),
                        self._latest_start(p["scope"]),
                    ),
                    "status": "open",
                }
                result = {
                    "status": "invited",
                    "token": invite,
                    "label": p["label"],
                    "expires_at": iso(db["invites"][token_hash(invite)]["expires_at"]),
                }
            p["status"] = "approved"
            if p["share_id"]:
                # Other reviews of this person's access were prepared against what it was
                # before; approving them would be refused, so they stop being offered.
                for other in db["proposals"].values():
                    if (
                        other is not p
                        and other.get("share_id") == p["share_id"]
                        and other["status"] == "pending"
                    ):
                        other["status"] = "superseded"
            # Never persist the raw invitation; retries cannot leak/recreate one.
            p["result"] = {k: v for k, v in result.items() if k != "token"}
            self.audit.event(
                "owner_approved",
                actor=actor,
                share_id=p["share_id"] or "",
                action=p["action"],
                person=p["label"],
            )
            return result

    def retire_older(self, proposal_id):
        """A newer review of someone's access replaces any older one still waiting, so a
        correction never leaves two live cards. Returns the cards to retire:
        [(chat_id, message_id)]."""
        retired = []
        with self.ledger.transaction() as db:
            p = db["proposals"].get(proposal_id)
            if not p or p["status"] != "pending" or p["action"] != "replace":
                return retired
            cards = db["setup"].get("review_cards", {})
            for other in db["proposals"].values():
                same = (
                    other.get("share_id") == p["share_id"]
                    if p["share_id"]
                    else not other.get("share_id")
                    and other["label"].casefold() == p["label"].casefold()
                )
                if (
                    other is p
                    or not same
                    or other["status"] != "pending"
                    or other["action"] != "replace"
                    or other.get("seq", 0) >= p.get("seq", 0)  # only reviews prepared earlier
                ):
                    continue
                other["status"] = "replaced"
                card = cards.pop("proposal:" + other["id"], None)
                if card:
                    retired += [
                        (card["chat_id"], m) for m in [card["message_id"], *card.get("also", [])]
                    ]
        return retired

    def present_review(self, actor, kind, identifier, message_id, chat_id, also=False):
        """Remember which message carries the complete review; only its buttons count.
        `also` adds a second message that shows the same complete review (the preview)."""
        self.owner(actor)
        if str(chat_id) != self.owner_id:
            raise GuestsError("Send the review to the owner's private chat.")
        with self.ledger.transaction() as db:
            cards = db["setup"].setdefault("review_cards", {})
            key = kind + ":" + identifier
            if also and key in cards and cards[key]["chat_id"] == str(chat_id):
                cards[key]["also"] = (cards[key].get("also", []) + [int(message_id)])[-3:]
                return
            cards[key] = {
                "message_id": int(message_id),
                "chat_id": str(chat_id),
                "expires_at": self.clock()
                + (self.claim_seconds if kind == "claim" else self.review_seconds),
            }

    def check_review(self, actor, kind, identifier, message_id, chat_id):
        self.owner(actor)
        card = self.ledger.snapshot()["setup"].get("review_cards", {}).get(kind + ":" + identifier)
        if (
            not card
            or message_id not in [card["message_id"], *card.get("also", [])]
            or card["chat_id"] != str(chat_id)
            or card["expires_at"] <= self.clock()
        ):
            raise GuestsError(
                "Use the latest complete review sent by this bot. Send /guests to restore pending reviews."
            )

    def refresh_invitation(self, actor, proposal_id):
        """Retry delivery without widening/reapproving an already reviewed invitation."""
        self.owner(actor)
        with self.ledger.transaction() as db:
            p = db["proposals"].get(proposal_id)
            if not p or p["status"] != "approved" or p.get("share_id"):
                raise GuestsError("Prepare a new invitation for this person.")
            self.normalize_scope(p["scope"], saved=True)
            related = [
                (key, i) for key, i in db["invites"].items() if i["proposal_id"] == proposal_id
            ]
            if not related and not p.get("invitation_accepted"):
                # Upkeep has cleared this review's invitations away, so nothing here says
                # whether one of them was accepted. A link is not handed out on a guess.
                raise GuestsError("Prepare a new invitation for this person.")
            if p.get("invitation_accepted") or any(spent(i) for _, i in related):
                raise GuestsError(
                    "This invitation was already accepted. Inspect the guest's current access instead."
                )
            if any(
                i["status"] == "claimed" and i["claim_until"] > self.clock() for _, i in related
            ):
                raise GuestsError(
                    "A guest is waiting for your account confirmation. Accept or deny that request first."
                )
            for _, i in related:
                i["status"] = "superseded"
            token = secrets.token_urlsafe(24)
            db["invites"][token_hash(token)] = {
                "proposal_id": proposal_id,
                "expires_at": min(
                    self.clock() + p.get("invitation_seconds", self.invitation_seconds),
                    self._latest_start(p["scope"]),
                ),
                "status": "open",
            }
            return {
                "status": "invited",
                "token": token,
                "label": p["label"],
                "expires_at": iso(db["invites"][token_hash(token)]["expires_at"]),
            }

    @staticmethod
    def retention_text(seconds):
        if seconds is None:
            return "unlimited time (until you delete it)"
        if seconds == 0:
            return "0 days"
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if seconds % size == 0:
                amount = seconds // size
                return f"{amount} {unit}{'s' if amount != 1 else ''}"
        return f"{seconds} seconds"

    def data_settings(self, actor):
        self.owner(actor)
        return {
            "retention_seconds": self.ledger.read()["setup"].get(
                "guest_data_retention_seconds", self.default_retention_seconds
            )
        }

    def maintenance(self, actor):
        self.owner(actor)
        try:
            self.audit.upkeep(self.clock())
        except OSError:
            pass  # activity cleanup is retried on the next pass
        with self.ledger.transaction() as db:
            try:
                self._keep_owner(db)
            except Exception:
                pass  # Owner warrant continuity is independent of guest upkeep.
            for sid, r in db["shares"].items():
                try:
                    self._keep_standing(db, r)
                except Exception:  # a lost key or artifact is reported on use and by repair
                    pass
                if not self._fresh(r):
                    self._clear(sid)
            cards = db["setup"].get("review_cards", {})
            for key, card in list(cards.items()):
                if card["expires_at"] < self.clock():
                    cards.pop(key)
            # Expired proposals/invitations have no authority and need not fill native state.
            for key, p in list(db["proposals"].items()):
                if p["valid_until"] < self.clock() - 7 * 86400:
                    linked = any(
                        i["proposal_id"] == key
                        and max(i.get("expires_at", 0), i.get("claim_until", 0)) > self.clock()
                        for i in db["invites"].values()
                    )
                    if not linked:
                        db["proposals"].pop(key)
            for key, i in list(db["invites"].items()):
                if max(i.get("expires_at", 0), i.get("claim_until", 0)) < self.clock() - 7 * 86400:
                    db["invites"].pop(key)
        return self.runner.cleanup(actor) if self.runner else []

    @staticmethod
    def valid_zone(zone):
        """An IANA time zone name such as America/Vancouver, or GuestsError."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        zone = str(zone or "").strip()
        try:
            if not zone or len(zone) > 64:
                raise ValueError
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise GuestsError(
                "Use a time zone such as America/Vancouver or Europe/London."
            ) from exc
        return zone

    def zone_of(self, record):
        """The time zone for messages a guest reads: theirs if known, else the owner's."""
        zone = (record or {}).get("guest_zone")
        try:
            return self.valid_zone(zone) if zone else self.zone
        except GuestsError:
            return self.zone

    def set_guest_zone(self, actor, share_id, zone):
        """A guest says where they are. It changes only how times are shown to them."""
        zone = self.valid_zone(zone)
        with self.ledger.transaction() as db:
            r = self._record(db, share_id)
            if r["guest_id"] != str(actor) or not self._fresh(r):
                raise GuestsError("Only a guest with current access can set their time zone.")
            r["guest_zone"] = zone
        self.audit.event("guest_time_zone", share_id=share_id, actor=str(actor), zone=zone)
        return {"time_zone": zone}

    def deny(self, actor, proposal_id, *, declined=True):
        self.owner(actor)
        with self.ledger.transaction() as db:
            p = db["proposals"].get(proposal_id)
            if p and p["status"] == "pending":
                p["status"] = "denied"
                if declined and p.get("renewal") and p.get("share_id") in db["shares"]:
                    self._declined(db["shares"][p["share_id"]])
        return {"status": "denied"}

    def _declined(self, record):
        """The owner said no to a request for more. Only these count toward the daily limit:
        a guest whose requests are approved, or simply not answered, can ask again."""
        now = self.clock()
        record["declined"] = [t for t in record.get("declined", []) if t > now - 86400] + [now]

    def _declined_today(self, db, guest_id):
        day = self.clock() - 86400
        return sum(
            t > day
            for r in db["shares"].values()
            if r["guest_id"] == str(guest_id)
            for t in r.get("declined", [])
        )

    def claim(self, token, actor, label, *, username=""):
        if not str(actor).isdigit() or str(actor) == self.owner_id:
            raise GuestsError("Use the guest's own Telegram account.")
        with self.ledger.transaction() as db:
            inv = db["invites"].get(token_hash(token))
            if not inv or inv["status"] != "open" or inv["expires_at"] <= self.clock():
                raise GuestsError("This invitation has ended. Ask the owner for a new one.")
            if any(r["guest_id"] == str(actor) and self._fresh(r) for r in db["shares"].values()):
                raise GuestsError("You already have an access record. Ask the owner to update it.")
            proposal = db["proposals"][inv["proposal_id"]]
            if self._latest_start(proposal["scope"]) <= self.clock() + 5:
                raise GuestsError("The proposed access has expired.")
            inv.update(
                status="claimed",
                guest_id=str(actor),
                guest_label=str(label)[:80],
                guest_username=str(username)[:80],
                claim_until=min(
                    self.clock() + self.claim_seconds, self._latest_start(proposal["scope"])
                ),
            )
            return {
                "claim_id": token_hash(token),
                "guest_id": str(actor),
                "guest_label": str(label)[:80],
                "text": review(proposal["label"], proposal["scope"], self.catalog, self.zone),
            }

    def accept_claim(self, actor, claim_id, *, accept=True):
        self.owner(actor)
        with self.ledger.transaction() as db:
            if len(claim_id) == 24:
                matches = [k for k in db["invites"] if k.startswith(claim_id)]
                claim_id = matches[0] if len(matches) == 1 else ""
            inv = db["invites"].get(claim_id)
            if inv and inv["status"] == "used" and accept and inv.get("share_id"):
                r = self._record(db, inv["share_id"])
                if not self._fresh(r):
                    raise GuestsError(
                        "This guest's access has ended. Inspect current access before changing it."
                    )
                return {
                    "status": "active",
                    "share_id": r["id"],
                    "guest_id": r["guest_id"],
                    "revision": r["revision"],
                    "text": review(r["label"], r["scope"], self.catalog, self.zone),
                }
            if not inv or inv["status"] != "claimed" or inv["claim_until"] <= self.clock():
                raise GuestsError("This guest confirmation has ended.")
            if not accept:
                inv["status"] = "denied"
                return {"status": "denied", "guest_id": inv["guest_id"]}
            if any(
                r["guest_id"] == inv["guest_id"] and self._fresh(r) for r in db["shares"].values()
            ):
                raise GuestsError("This guest already has access. Update their existing record.")
            proposal = db["proposals"][inv["proposal_id"]]
            sid = "sh_" + uuid.uuid4().hex[:12]
            p = {**proposal}
            result = self._activate(db, sid, inv["guest_id"], p)
            db["shares"][sid]["telegram_name"] = inv["guest_label"]
            if self.guest_names == "telegram" and inv["guest_label"].strip():
                db["shares"][sid]["label"] = inv["guest_label"].strip()[:80]
            db["shares"][sid]["telegram_username"] = inv.get("guest_username", "")
            inv["status"] = "used"
            inv["share_id"] = sid
            # Kept on the review too: the invitation is cleared away in time, and the review
            # can outlast it.
            proposal["invitation_accepted"] = True
            return result

    def _activate(self, db, share_id, guest_id, proposal):
        if any(
            r["id"] != share_id and r["guest_id"] == str(guest_id) and self._fresh(r)
            for r in db["shares"].values()
        ):
            raise GuestsError(
                "This person already has active access. Update their current access instead of restoring an older grant."
            )
        if db["shares"].get(share_id, {}).get("data_deletion_requested_at") is not None:
            raise GuestsError("This guest data is being deleted. Create a new invitation.")
        access = self.normalize_scope(proposal["scope"], saved=True)
        duration = access.pop("duration_seconds", None) or proposal.get("more_seconds")
        self._representable(access, guest=guest_id)
        # This path is reachable only after the owner accepts a complete new review.
        # A lost guest holder can be replaced here; automatic repair never remints it.
        holder_path = self.root / "control" / (share_id + ".key")
        try:
            holder = load_key(holder_path, create=True)
        except GuestsError:
            if share_id not in db["shares"]:
                raise
            archive = self.root / "recovery" / (uuid.uuid4().hex + ".holder")
            from .store import private_dir

            private_dir(archive.parent)
            holder_path.rename(archive)
            holder = load_key(holder_path)

        warrant = compile_scope(access, holder, self.issuer, now=self.clock(), guest=guest_id)
        # Publication before active state: an interrupted issuance leaves an unused artifact.
        wire = warrant.to_bytes()
        path = self.root / "warrants" / (str(warrant.id) + ".warrant")
        atomic_private(path, wire)
        old = db["shares"].get(share_id)
        history = list(old.get("old_warrants", [])) if old else []
        if old:
            self._revoke_warrant(db, old["warrant_id"])
            history.append(old["warrant_id"])
        record = {
            "id": share_id,
            "guest_id": str(guest_id),
            "label": proposal["label"],
            "scope": access,
            "revision": old["revision"] + 1 if old else 1,
            "status": "active",
            "warrant_id": str(warrant.id),
            "artifact_hash": hashlib.sha256(wire).hexdigest(),
            "old_warrants": history,
            "updated_at": self.clock(),
            # How long the owner last chose, so a renewal can offer the same again.
            **(
                {"duration_seconds": duration}
                if duration
                else {"duration_seconds": old["duration_seconds"]}
                if old and old.get("duration_seconds")
                else {}
            ),
            # A change the owner approves answers an open request for more: what they have
            # now is the owner's answer, and they can ask again. Declines stay counted.
            **{
                k: old[k]
                for k in (
                    "more_asked",
                    "declined",
                    "guest_zone",
                    "renewals_asked",
                    "telegram_name",
                    "telegram_username",
                )
                if old and k in old
            },
            **({"guest_zone": proposal["guest_zone"]} if proposal.get("guest_zone") else {}),
            "notices": 1,  # end notices are retried until delivered
            # When the owner last approved this access; automatic reissue doesn't move it.
            "changed_at": self.clock(),
            "context_epoch": (old.get("context_epoch", 1) if old else 1)
            + (
                1
                if old
                and (
                    not set(old["scope"]["lights"]) <= set(access["lights"])
                    or (old["scope"]["research"] and not access["research"])
                    or old["scope"].get("tools", []) != access.get("tools", [])
                )
                else 0
            ),
        }
        db["shares"][share_id] = record
        self._clear(share_id)
        self.audit.event(
            "access_activated",
            share_id=share_id,
            actor=self.owner_id,
            revision=record["revision"],
            warrant_id=str(warrant.id),
        )
        return {
            "status": "active",
            "share_id": share_id,
            "guest_id": str(guest_id),
            "revision": record["revision"],
            "text": review(record["label"], access, self.catalog, self.zone),
        }

    def takeover_scope(self, names):
        """The access a person the bot already admits would get: the listed tools with their
        full input range and no end date. Prepared before the owner is asked, so the review
        shows exactly what is granted and `adopt` grants exactly what was reviewed."""
        from .permissions import example_entry, validate_schema

        tools = []
        for name in sorted(set(names)):
            spec = self.tool_resolver(name)
            if spec:
                tools.append(example_entry(validate_schema(spec)))
        if not tools:
            raise GuestsError("There are no shareable tools to carry over for this person.")
        access = self.normalize_scope({"tools": tools, "until_ended": True})
        self._representable(access)  # before the owner is asked, not after setup is saved
        return access

    def _representable(self, normalized, *, guest="99999999999999999999"):
        """Refuse access the runtime cannot sign within its size limit."""
        try:
            # Probe SDK representability with an untrusted throwaway root.
            # The real issuer never signs guest authority before confirmation.
            # Before an invitation is claimed, reserve room for a full 64-bit decimal
            # account ID. Activation checks the actual ID as well.
            probe = compile_scope(
                normalized,
                SigningKey.generate(),
                SigningKey.generate(),
                now=self.clock(),
                guest=guest,
            )
            if len(probe.to_bytes()) > MAX_WARRANT_SIZE:
                raise ValueError("selection exceeds the runtime size limit")
        except Exception as exc:
            raise GuestsError(
                "That selection is too large or uses a restriction this runtime cannot express. Choose fewer tools or simpler input choices."
            ) from exc

    def adopt(self, actor, guest_id, label, names=None, scope=None):
        """Give a person the bot already admits their own scoped access with no end date.
        Only at owner setup, after the owner has reviewed who is taken over and with what:
        `scope` is that reviewed grant and is refused if a tool has changed since. The
        person keeps the listed tools; everything else, including the tools that act on
        this machine, is no longer theirs."""
        self.owner(actor)
        guest_id = str(guest_id)
        if not guest_id.isdigit() or guest_id == self.owner_id:
            raise GuestsError("Choose another person's numeric Telegram ID.")
        access = (
            self.normalize_scope(scope, saved=True)
            if scope is not None
            else self.takeover_scope(names or [])
        )
        with self.ledger.transaction() as db:
            mine = [r for r in db["shares"].values() if r["guest_id"] == guest_id]
            current = next((r for r in mine if self._fresh(r)), None)
            if current:
                return {"status": "active", "share_id": current["id"], "guest_id": guest_id}
            if mine:
                # Their own access ran out or was ended. Taking them over would hand it
                # back, wider and with no end date; that takes a new review of its own.
                latest = max(mine, key=lambda r: r.get("updated_at", 0))
                return {"status": "ended", "share_id": latest["id"], "guest_id": guest_id}
            share_id = "sh_" + uuid.uuid4().hex[:12]
            result = self._activate(
                db, share_id, guest_id, {"label": str(label)[:80] or guest_id, "scope": access}
            )
        self.audit.event(
            "member_adopted", share_id=result["share_id"], actor=self.owner_id, guest=guest_id
        )
        return result

    def _clear(self, share_id):
        if self.runner and hasattr(self.runner, "cancel_share"):
            self.runner.cancel_share(share_id)
        self.guard.clear_session_warrant(share_id)
        for key, value in list(self.delegates.items()):
            if value["share_id"] == share_id:
                self.guard.clear_session_warrant(key)
                self.delegates.pop(key, None)

    def list_access(self, actor):
        db = self.ledger.snapshot()
        return [
            {
                **r,
                "effective_status": r["status"]
                if self._fresh(r)
                else ("expired" if r["status"] == "active" else r["status"]),
            }
            for r in db["shares"].values()
            if str(actor) == self.owner_id or r["guest_id"] == str(actor)
        ]

    def get_access(self, actor, share_id):
        for r in self.list_access(actor):
            if r["id"] == share_id:
                return r
        raise GuestsError("Access was not found.")

    def guest(self, actor):
        records = self.list_access(actor)
        own = [r for r in records if r["guest_id"] == str(actor)]
        if not own:
            raise GuestsError(
                "You don't have access yet. If the owner sent you an invitation link, tap it. Otherwise ask them for one."
            )
        return max(own, key=lambda r: r["updated_at"])

    def renewal(self, actor, share_id, seconds=None):
        """A guest asks for more time: by default the length the owner last chose, from
        when the owner approves. The owner can change it before approving."""
        record = self.get_access(actor, share_id)
        if record["guest_id"] != str(actor):
            raise GuestsError("Only the guest may request renewal.")
        if record["status"] == "revoked":
            raise GuestsError("This access was revoked. Ask the owner for a new invitation.")
        if record["status"] == "active" and record["scope"].get("until_ended"):
            raise GuestsError("Your access has no end date, so there is nothing to renew.")
        # Checked before anything is recorded, so a corrected request can follow at once.
        if seconds is None:
            seconds = record.get("duration_seconds") or 86400
        if type(seconds) is not int or not 60 <= seconds <= 30 * 86400:
            raise GuestsError("Ask for between a minute and 30 days.")
        with self.ledger.transaction() as db:
            r = self._record(db, share_id)
            # The limits are the person's, not one access's: someone who has had access
            # more than once still has the old buttons, and they are not extra allowances.
            theirs = [x for x in db["shares"].values() if x["guest_id"] == r["guest_id"]]
            if max(x.get("renewal_requested", 0) for x in theirs) > self.clock() - 300:
                raise GuestsError("A renewal request was already sent recently.")
            waiting = [
                p
                for p in db["proposals"].values()
                if p.get("share_id") in {x["id"] for x in theirs}
                and p.get("renewal")
                and p["status"] == "pending"
                and p["valid_until"] > self.clock()
            ]
            if waiting:
                raise GuestsError("The owner has not answered your last request for more time yet.")
            day = self.clock() - 86400
            if self._declined_today(db, r["guest_id"]) >= MORE_PER_DAY:
                raise GuestsError(
                    "The owner said no a few times today. Try again tomorrow, or talk to them directly."
                )
            asked = self.clock()
            r["renewals_asked"] = [at for at in r.get("renewals_asked", []) if at > day] + [asked]
            r["renewal_requested"] = asked
        access = {**record["scope"]}
        # More time never changes private memory: saved scopes leave out a false flag,
        # which a new review would otherwise read as switched on.
        access["guest_memory"] = bool(record["scope"].get("guest_memory"))
        if not access.get("until_ended"):
            end = timestamp(access["expires_at"]) if access.get("expires_at") else 0
            if self._fresh(record) and end > self.clock():
                # Still on: more time is added to the current end, so nothing is lost by
                # asking early. A fixed new end, unchanged by when the owner answers.
                access["expires_at"] = iso(end + seconds)
            else:
                access["duration_seconds"] = seconds
                access.pop("expires_at", None)
        access.pop("preset_schemas", None)
        access["tools"] = tool_selections(access.get("tools", []))
        try:
            return self.prepare(
                self.owner_id,
                record["label"],
                access,
                share_id=share_id,
                renewal=asked,
                more_seconds=seconds if access.get("expires_at") else None,
                # Built from this revision: a change made meanwhile needs a fresh request.
                revision=record["revision"],
            )
        except Exception:
            # No review came of it, so the owner was asked nothing: it is not one of the
            # few requests a guest gets in a day.
            with self.ledger.transaction() as db:
                self._request_not_made(self._record(db, share_id), asked)
            raise

    @staticmethod
    def _request_not_made(record, asked=None):
        """Uncount the request made at `asked`, leaving any made since as they are."""
        times = list(record.get("renewals_asked", []))
        if asked in times:
            times.remove(asked)
        elif asked is None:  # a review saved before requests were matched to their time
            times = times[:-1]
        record["renewals_asked"] = times
        if times:
            record["renewal_requested"] = max(times)
        else:
            record.pop("renewal_requested", None)

    def withdraw_renewal(self, share_id, proposal_id):
        """The owner could not be told about a request for more time: it is taken back. Its
        review is closed, nothing is left waiting, and it does not count against the guest."""
        with self.ledger.transaction() as db:
            p = db["proposals"].get(proposal_id)
            if (
                not p
                or not p.get("renewal")
                or p.get("share_id") != share_id
                or p["status"] != "pending"
            ):
                return
            p["status"] = "denied"
            self._request_not_made(self._record(db, share_id), p.get("renewal_asked"))

    def _as_called(self, scope, tool, args):
        """A call as it will run under `scope`: the tool and its inputs with the values the
        access fills in. The same steps admission applies."""
        entries = {e["name"]: e for e in scope.get("tools", [])}
        if scope.get("research"):
            tool, args = self.normalize_call(tool, args)
        if tool in entries:
            args = default_arguments(entries[tool], args)
        return tool, args

    def normalize_call(self, tool, args):
        if tool not in SERVICE_TOOLS or not isinstance(args, dict):
            return tool, args
        args = dict(args)
        if tool == "web_search":
            args.setdefault("limit", 5)
        if tool == "web_extract":
            args.setdefault("char_limit", 12000)
        return tool, args

    def admit_chat(self, actor, share_id):
        """Verify the internal conversation entitlement without executing a tool."""
        self._continue(share_id, actor)  # access with no end date, as for calls and subtasks
        with self.ledger.transaction() as db:
            record = self._record(db, share_id)
            if record["guest_id"] != str(actor) or not self._fresh(record):
                raise GuestsError("Your access has ended. Ask the owner to renew it.")
            self._refresh_srl(db)
            warrant = self._warrant(record)
            holder = load_key(self.root / "control" / (share_id + ".key"), create=False)
            self.guard.set_session_warrant(share_id, warrant, holder)
            if self.guard.pre_tool_call(CHAT_ONLY_CAPABILITY, {}, session_id=share_id):
                raise GuestsError("Chat access could not be verified. Ask the owner to repair it.")
            self.audit.event("chat_admitted", share_id=share_id, actor=actor)

    def admit_turn(self, actor, share_id, *, delegation=None):
        """Reject inactive or unverifiable access before runtime lookup or any model call."""
        self._continue(share_id, actor)
        with self.ledger.transaction() as db:
            record = self._record(db, share_id)
            if record["guest_id"] != str(actor) or not self._fresh(record):
                raise GuestsError("Your access has ended. Ask the owner to authorize you again.")
            try:
                self._refresh_srl(db)
                warrant = self._warrant(record)
                holder = load_key(self.root / "control" / (share_id + ".key"), create=False)
                self.audit.authorizer.verify(warrant)
                if not same_key(warrant.authorized_holder, holder.public_key):
                    raise GuestsError("The guest identity does not match its access.")
                if delegation:
                    child = self.delegates.get(delegation)
                    if (
                        not child
                        or child["share_id"] != share_id
                        or child["revision"] != record["revision"]
                    ):
                        raise GuestsError("This subtask has ended.")
                    self.audit.authorizer.verify_chain([warrant, child["warrant"]])
                    if not same_key(child["warrant"].authorized_holder, child["holder"].public_key):
                        raise GuestsError("The subtask identity does not match its access.")
            except Exception as exc:
                self.audit.event("model_admission_denied", share_id=share_id, actor=actor)
                raise GuestsError(
                    "Your access could not be verified. Ask the owner to review it again."
                ) from exc
            self.audit.event("model_admitted", share_id=share_id, actor=actor)
            return copy.deepcopy(record)

    def admit_memory(self, actor, share_id, target, *, delegation=None, operation="write"):
        """Authorize a native write to this guest's own profile; never dispatch owner memory.

        Only the target is audited. Preference text stays in the guest profile.
        """
        if delegation or target not in {"memory", "user"} or operation not in {"read", "write"}:
            raise GuestsError("Only the guest may update their own private memory.")
        self._continue(share_id, actor)
        with self.ledger.transaction() as db:
            record = self._record(db, share_id)
            if record["guest_id"] != str(actor) or not self._fresh(record):
                raise GuestsError("Your access has ended. Private memory was not changed.")
            self._refresh_srl(db)
            warrant = self._warrant(record)
            holder = load_key(self.root / "control" / (share_id + ".key"), create=False)
            args = {"target": target, "operation": operation}
            self.guard.set_session_warrant(share_id, warrant, holder)
            allowed = (
                self.guard.pre_tool_call(GUEST_MEMORY_CAPABILITY, args, session_id=share_id) is None
            )
            self.audit.decision(
                [warrant],
                holder,
                GUEST_MEMORY_CAPABILITY,
                args,
                allowed=allowed,
                share_id=share_id,
                actor=actor,
                call_id=uuid.uuid4().hex,
                now=self.clock(),
            )
            if not allowed:
                raise GuestsError("Private memory needs an updated owner-approved access review.")
        return {"allowed": True}

    def call(self, actor, share_id, tool, args, *, delegation=None, call_id=None):
        """Atomically admit a signed call; don't hold lifecycle state during service I/O.

        Revocation stops new admissions. An already admitted request may finish.
        """
        args = dict(args) if isinstance(args, dict) else args
        call_id = call_id or uuid.uuid4().hex
        if tool in {STANDING_MARK, "share_no_end_date", GUEST_MEMORY_CAPABILITY} or str(
            tool
        ).startswith(GUEST_MARKS):
            raise GuestsError("That action is outside your approved access.")
        # Web addresses are looked up before the saved state is locked: a slow name server
        # must not hold up everyone else's requests. They are taken from the call as it
        # will run, with the inputs the access fills in, and anything not looked up here
        # is refused at admission.
        from .public_web import pinned, public_verdicts, unapproved_addresses, web_addresses

        try:
            before = self.ledger.snapshot()["shares"].get(share_id) or {}
            child = self.delegates.get(delegation) if delegation else None
            _, expected = self._as_called((child or before).get("scope") or {}, tool, args)
        except Exception:
            expected = args
        # One time budget for the whole call, each name looked up once.
        looked_up = public_verdicts(web_addresses(expected) + web_addresses(args))
        self._continue(share_id, actor)
        try:
            with self.ledger.transaction() as db:
                r = self._record(db, share_id)
                if r["guest_id"] != str(actor) or not self._fresh(r):
                    self.audit.event(
                        "admission_denied",
                        share_id=share_id,
                        actor=actor,
                        reason="inactive-or-wrong-identity",
                    )
                    raise GuestsError(
                        "Your access has ended or changed. Ask the owner to renew it."
                    )
                if tool == CHAT_ONLY_CAPABILITY:
                    self.audit.event(
                        "adapter_blocked",
                        share_id=share_id,
                        actor=actor,
                        reason="non-dispatchable-entitlement",
                    )
                    raise GuestsError("Chat-only access has no executable actions.")
                self._refresh_srl(db)
                from .permissions import validate_schema

                for name, expected in r["scope"].get("preset_schemas", {}).items():
                    actual = self.tool_resolver(name)
                    if not actual or digest(validate_schema(actual)) != expected:
                        self.audit.event(
                            "adapter_blocked",
                            share_id=share_id,
                            actor=actor,
                            reason="preset-schema-changed",
                        )
                        raise GuestsError(
                            "A connected tool changed. The owner needs to review access again."
                        )
                parent = self._warrant(r)
                holder = load_key(self.root / "control" / (share_id + ".key"), create=False)
                chain = [parent]
                if delegation:
                    d = self.delegates.get(delegation)
                    if not d or d["share_id"] != share_id or d["revision"] != r["revision"]:
                        raise GuestsError("This delegated task has ended.")
                    chain, holder = [parent, d["warrant"]], d["holder"]
                active_scope = d["scope"] if delegation else r["scope"]
                entries = {e["name"]: e for e in active_scope.get("tools", [])}
                if active_scope.get("research"):
                    tool, args = self.normalize_call(tool, args)
                if tool in entries and not entries[tool].get("alternatives"):
                    args = default_arguments(entries[tool], args)
                # A grouped tool is called with its inputs inside a one-item `request`; rules
                # for one input are matched against that operation. The envelope itself
                # stays as it is for verification and signing.
                request = args.get("request") if isinstance(args, dict) else None
                operation = (
                    request[0]
                    if (entries.get(tool) or {}).get("alternatives")
                    and isinstance(args, dict)
                    and set(args) == {"request"}
                    and isinstance(request, list)
                    and len(request) == 1
                    and isinstance(request[0], dict)
                    else None
                )
                approved = {e["name"]: e for e in r["scope"].get("tools", [])}
                if unapproved_addresses(
                    approved.get(tool), operation if operation is not None else args, looked_up
                ):
                    # A guest cannot point the owner's tools at the owner's own network,
                    # whatever the owner allows for themselves. An address the owner named
                    # in the review is the owner's choice.
                    self.audit.event(
                        "adapter_blocked",
                        share_id=share_id,
                        actor=actor,
                        call_id=call_id,
                        reason="address-not-public",
                        tool=tool,
                        inputs=" ".join(str(args).split())[:200],
                    )
                    raise GuestsError(
                        "That address is not on the public internet, so it is not opened for guests."
                    )
                never = self.never()
                # The operation carries every input, so it is what the list is applied to;
                # the envelope's own `request` is not one of the tool's inputs.
                if never and (
                    never.blocks_tool(tool)
                    or never.blocked_for(tool, args if operation is None else operation)
                ):
                    # The owner's list is checked on every call, so it also holds for
                    # free-text inputs and for access granted before the list changed.
                    self.audit.event(
                        "never_share_blocked",
                        share_id=share_id,
                        actor=actor,
                        call_id=call_id,
                        tool=tool,
                        inputs=" ".join(str(args).split())[:200],
                    )
                    raise GuestsError(
                        "The owner does not share that. Do not look for it another way or in other "
                        "words; tell the person it is not shared."
                    )
                w = chain[-1]
                session = delegation or share_id
                self.guard.set_session_warrant(
                    session, w, holder, parent_warrant=chain[:-1] or None
                )
                denial = self.guard.pre_tool_call(
                    tool, args, session_id=session, tool_call_id=call_id
                )
                allowed = denial is None
                from .model import card_words

                self.audit.decision(
                    chain,
                    holder,
                    tool,
                    args,
                    allowed=allowed,
                    share_id=share_id,
                    actor=actor,
                    call_id=call_id,
                    now=self.clock(),
                    said=card_words(
                        r["scope"], tool, operation if operation is not None else args, self.catalog
                    ),
                )
                if not allowed:
                    raise GuestsError("That action is outside your approved access.")
                # The warrant travels outside model arguments. Non-Tenuo handlers may ignore it.
                proof_time = int(self.clock())
                envelope = {
                    "version": 1,
                    "tool": tool,
                    "arguments": args,
                    "chain": [base64.b64encode(x.to_bytes()).decode() for x in chain],
                    "proof": base64.b64encode(w.sign(holder, tool, args, proof_time)).decode(),
                    "timestamp": proof_time,
                }
                if tool in entries and entries[tool].get("alternatives"):
                    # This is the same whole request the SDK verified and the proof binds.
                    request = args.get("request")
                    if (
                        not isinstance(request, list)
                        or len(request) != 1
                        or not isinstance(request[0], dict)
                    ):
                        self.audit.event(
                            "adapter_blocked",
                            share_id=share_id,
                            actor=actor,
                            call_id=call_id,
                            reason="invalid-request-envelope",
                        )
                        raise GuestsError(
                            "The operation request needs one complete argument object."
                        )
                    args = copy.deepcopy(request[0])
                    envelope["argument_view"] = "single_request"
                if tool in entries:
                    try:
                        validate_call(entries[tool], args, self.tool_resolver)
                    except GuestsError:
                        self.audit.event(
                            "adapter_blocked",
                            share_id=share_id,
                            actor=actor,
                            call_id=call_id,
                            reason="schema-or-input-changed",
                        )
                        raise
                if tool == "web_extract":
                    from tenuo import UrlSafe

                    # Research reads public pages only. A page the owner named exactly when
                    # sharing this tool by itself is the owner's choice, as it is above.
                    # (Compared as the scan reads them, which leaves off a closing full stop
                    # or comma, so a named page that ends in one is still the named page.)
                    named = pinned(approved.get(tool), "urls", args)
                    if not isinstance(args.get("urls"), list) or not all(
                        UrlSafe().satisfies(u)
                        or (
                            isinstance(u, str)
                            and web_addresses(u)
                            and set(web_addresses(u)) <= named
                        )
                        for u in args["urls"]
                    ):
                        self.audit.event(
                            "adapter_blocked",
                            share_id=share_id,
                            actor=actor,
                            call_id=call_id,
                            reason="non-public-url",
                        )
                        raise GuestsError("Research can read public web pages only.")
                if self.dispatch is None:
                    raise GuestsError("The service isn't connected.")
                with self.in_flight_lock:
                    self.in_flight[call_id] = share_id
                self.audit.event(
                    "dispatch_admitted", share_id=share_id, actor=actor, call_id=call_id
                )
            try:
                result = self.dispatch(tool, args, envelope)
            except Exception:
                self.audit.event(
                    "outcome", share_id=share_id, actor=actor, call_id=call_id, outcome="failed"
                )
                raise GuestsError("The service call failed. Access was not changed.")
            # Service errors are not successful actions, even when authorization allowed them.
            bad = isinstance(result, dict) and (
                result.get("error") or result.get("success") is False
            )
            self.audit.event(
                "outcome",
                share_id=share_id,
                actor=actor,
                call_id=call_id,
                outcome="failed" if bad else "returned",
            )
            from .results import guest_result

            return guest_result(tool, result)

        finally:
            with self.in_flight_lock:
                self.in_flight.pop(call_id, None)

    def delegate(self, actor, share_id, requested, *, task, parent_delegation=None):
        """Agent-selected narrowing never asks the owner and never widens authority."""
        if not isinstance(task, str) or not 1 <= len(task) <= 4000:
            raise GuestsError("Give the subtask a bounded goal.")
        self._continue(share_id, actor)
        with self.ledger.transaction() as db:
            r = self._record(db, share_id)
            if r["guest_id"] != str(actor) or not self._fresh(r):
                raise GuestsError("The guest's access has ended.")
            if r["scope"].get("chat_only"):
                raise GuestsError("Chat-only access cannot delegate tools.")
            if parent_delegation:
                raise GuestsError("Nested delegation is limited to one hop in this MVP.")
            if sum(x["share_id"] == share_id for x in self.delegates.values()) >= 4:
                raise GuestsError("The active subtask limit has been reached.")
            if not isinstance(requested, dict):
                raise GuestsError("Choose a narrower tool scope for the subtask.")
            requested = {
                **requested,
                "expires_at": requested.get(
                    "expires_at", iso(min(timestamp(r["scope"]["expires_at"]), self.clock() + 300))
                ),
            }
            access = self.normalize_scope(requested, parent=r["scope"])
            if timestamp(access["expires_at"]) > self.clock() + 300:
                raise GuestsError("A subtask can run for at most five minutes.")
            parent = self._warrant(r)
            parent_key = load_key(self.root / "control" / (share_id + ".key"), create=False)
            holder = SigningKey.generate()
            try:
                child = compile_scope(access, holder, parent_key, now=self.clock(), parent=parent)
            except Exception as exc:
                self.audit.event(
                    "delegation_denied",
                    share_id=share_id,
                    actor=actor,
                    reason="unrepresentable-narrowing",
                )
                raise GuestsError(
                    "The runtime cannot prove this child restriction. Choose simpler narrowing or do the task with your existing approved tools; do not request user approval."
                ) from exc
            did = "task_" + uuid.uuid4().hex[:12]
            self.delegates[did] = {
                "share_id": share_id,
                "revision": r["revision"],
                "scope": access,
                "warrant": child,
                "holder": holder,
                "task": task,
            }
            self.audit.event(
                "delegated", share_id=share_id, actor=actor, task_id=did, warrant_id=str(child.id)
            )
            return {"task_id": did, "scope": access, "warrant_id": str(child.id)}

    def finish_task(self, task_id):
        previous = self.delegates.pop(task_id, None)
        if previous:
            self.audit.event("delegation_finished", share_id=previous["share_id"], task_id=task_id)
        self.guard.clear_session_warrant(task_id)

    def note_more_request(self, actor, share_id, words=""):
        """A guest asks the owner for more access: one open request at a time and a few a
        day, so an owner cannot be worn down. Nothing about the access changes here. The
        request is saved with its words, so its card can be shown again, and with a token,
        so only its own buttons answer it."""
        with self.ledger.transaction() as db:
            r = self._record(db, share_id)
            if r.get("guest_id") != str(actor) or not self._fresh(r):
                raise GuestsError("Only a guest with current access can ask for more.")
            if open_request(r):
                raise GuestsError("The owner has not answered your last request yet.")
            now = self.clock()
            asked = [t for t in r.get("more_asked", []) if t > now - 86400]
            if self._declined_today(db, r["guest_id"]) >= MORE_PER_DAY:
                raise GuestsError(
                    "The owner said no a few times today. Try again tomorrow, or talk to them directly."
                )
            token = secrets.token_urlsafe(6)
            r["more_asked"] = asked + [now]
            r["more_open"] = {"token": token, "words": str(words)[:300], "at": now}
            self.audit.event("guest_asked_for_more", share_id=share_id, actor=str(actor))
            return {"label": r["label"], "token": token}

    def withdraw_more_request(self, share_id, token):
        """The owner could not be told about a request: it is taken back, and does not
        count against the guest."""
        with self.ledger.transaction() as db:
            r = db["shares"].get(share_id)
            if r and open_request(r).get("token") == token:
                r.pop("more_open")
                r["more_asked"] = list(r.get("more_asked", []))[:-1]

    def answer_more_request(self, actor, share_id, token, *, declined=False):
        """The owner answered one request, either way. Only that request's own buttons
        can do this; an older card answers nothing. Returns the access record."""
        self.owner(actor)
        with self.ledger.transaction() as db:
            r = self._record(db, share_id)
            if open_request(r).get("token") != token:
                raise GuestsError("That request was already answered.")
            r.pop("more_open")
            if declined:
                self._declined(r)
            return copy.deepcopy(r)

    def open_requests(self, actor):
        """Requests for more access the owner has not answered: [(record, request)]."""
        self.owner(actor)
        return [
            (copy.deepcopy(r), dict(r["more_open"]))
            for r in self.ledger.snapshot()["shares"].values()
            if isinstance(r.get("more_open"), dict) and self._fresh(r)
        ]

    def never(self):
        """The owner's never-share list as it stands now."""
        from .never import NeverShare

        saved = self.ledger.read()["setup"].get("never_share") or {}
        return NeverShare(saved.get("tools", []), saved.get("values", []), saved.get("scoped", []))

    def never_set(self, actor, kind, item, on, tool=None, field=None):
        """Add or remove a tool, a value, or (kind "scoped") a value for one tool or one of
        its inputs. Only the owner; takes effect at once, also for access that already exists."""
        from .never import MAX_ITEMS, checked_item, fold

        self.owner(actor)
        shown = checked_item(kind, item)
        if kind == "scoped" and on:  # taking a rule off never needs the tool to still exist
            schema = self.tool_resolver(str(tool or ""))
            if not schema:
                raise GuestsError("Choose one of your shareable tools for that rule.")
            known = (schema.get("function", schema).get("parameters") or {}).get("properties") or {}
            if field and field not in known:
                raise GuestsError(
                    f"{tool} has no input called {field}. Its inputs: "
                    + ", ".join(sorted(known))
                    + "."
                )
        with self.ledger.transaction() as db:
            saved = db["setup"].setdefault("never_share", {"tools": [], "values": []})
            for part in ("tools", "values", "scoped"):
                saved.setdefault(part, [])
            if kind == "scoped":
                rule = {"tool": str(tool), "field": str(field or ""), "value": shown}
                mine = (fold(rule["tool"]), rule["field"], fold(shown))
                items = [
                    r
                    for r in saved["scoped"]
                    if (fold(r["tool"]), r.get("field") or "", fold(r["value"])) != mine
                ]
            else:
                rule = shown
                items = [x for x in saved[kind] if fold(x) != fold(shown)]
            if on:
                if len(saved["tools"]) + len(saved["values"]) + len(saved["scoped"]) >= MAX_ITEMS:
                    raise GuestsError("The never-share list is full. Remove something first.")
                items.append(rule)
            saved[kind] = items
            self.audit.event(
                "never_share_changed",
                share_id="owner",
                actor=str(actor),
                change=("add " if on else "remove ") + kind,
            )
            return copy.deepcopy(saved)

    def never_remove_word(self, actor, value, tool, field, reviewed):
        """Remove only the rules shown in the owner's review, in a single ledger update."""
        from .never import fold

        self.owner(actor)
        with self.ledger.transaction() as db:
            if not reviewed or self.never().word_locations(value, tool, field) != reviewed:
                raise GuestsError("The never-share list changed. Ask again to review the removal.")
            saved = db["setup"]["never_share"]
            word = fold(value)
            if ("", "") in reviewed:
                saved["values"] = [entry for entry in saved["values"] if fold(entry) != word]
            saved["scoped"] = [
                rule
                for rule in saved.get("scoped", [])
                if (rule.get("tool"), rule.get("field") or "") not in reviewed
                or fold(rule.get("value")) != word
            ]
            self.audit.event(
                "never_share_changed", share_id="owner", actor=str(actor), change="remove word"
            )

    def activity(self, actor, share_id=None, *, scope="guest", limit=50, since=None):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GuestsError("Choose between 1 and 1000 recent activity events.")
        if not isinstance(scope, str) or scope not in {"guest", "owner", "all"}:
            raise GuestsError("Choose guest, owner or all activity.")
        if scope in {"owner", "all"}:
            self.owner(actor)
            return self.audit.list(
                None, limit, actor=self.owner_id if scope == "owner" else None, since=since
            )
        self.get_access(actor, share_id)
        return self.audit.list(share_id, limit, since=since)

    def repair(self, actor):
        self.owner(actor)
        # Recover missing files from the already-trusted live key, never generate
        # a replacement identity or overwrite a conflicting saved key silently.
        for filename, trusted in (("issuer.key", self.issuer), ("audit.key", self.audit_key)):
            path = self.root / "control" / filename
            if not path.exists() and not path.is_symlink():
                atomic_private(path, base64.b64encode(trusted.secret_key_bytes()))
            else:
                saved = load_key(path, create=False)
                if saved.public_key.to_bytes() != trusted.public_key.to_bytes():
                    raise GuestsError(
                        "Saved and active sharing identities differ. Restore the matching backup or explicitly recover the sharing identity."
                    )
        result = {"restored": [], "paused": []}
        with self.ledger.transaction() as db:
            for r in db["shares"].values():
                if self._fresh(r):
                    try:
                        w = self._warrant(r)
                        self.normalize_scope(r["scope"], saved=True, check_never=False)
                        if self._refresh_srl(db).is_revoked(str(w.id)):
                            raise GuestsError("The saved warrant has been revoked.")
                        load_key(self.root / "control" / (r["id"] + ".key"), create=False)
                        result["restored"].append(r["id"])
                    except Exception:
                        r["status"] = "unavailable"
                        result["paused"].append(r["id"])
                self._clear(r["id"])
        return result
