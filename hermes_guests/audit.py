"""Signed local decision evidence plus separately correlated service outcomes."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from tenuo import Authorizer, verify_receipt
from tenuo.receipts import FileReceiptSink
from tenuo_core import ReceiptIssuer

from .store import atomic_private, private_dir


class Audit:
    def __init__(self, root, key, trusted_root):
        self.root = private_dir(Path(root) / "audit")
        self.events = self.root / "activity.jsonl"
        self.segment = uuid.uuid4().hex
        self.receipt_path = self.root / (self.segment + ".receipts.jsonl")
        atomic_private(self.receipt_path, b"")
        self.sink = FileReceiptSink(self.receipt_path)
        self.authorizer = Authorizer(trusted_roots=[trusted_root], clock_tolerance_secs=0)
        self.issuer = ReceiptIssuer(key)
        self.issuer.bind_authorizer(self.authorizer)
        self.lock = threading.RLock()

    def event(self, kind, *, share_id="", actor="", **values):
        import time

        item = {
            "at": time.time(),
            "kind": kind,
            "share_id": share_id,
            "actor": str(actor),
            **values,
        }
        with self.lock:
            if self.events.exists() and self.events.stat().st_size > 5_000_000:
                # Keep bounded readable activity; receipt segments remain separate evidence.
                previous = self.root / "activity.previous.jsonl"
                os.replace(self.events, previous)
            fd = os.open(self.events, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps(item, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        return item

    def decision(self, chain, holder, tool, args, *, allowed, share_id, actor, call_id, now):
        """The Rust authorizer independently verifies the exact signed call for evidence."""
        pop = chain[-1].sign(holder, tool, args, int(now))
        with self.lock:
            verified = None
            failure = None
            try:
                verified = self.authorizer.check_chain(chain, tool, args, pop)
            except Exception as exc:
                failure = exc
            if allowed and failure is not None:
                raise RuntimeError(
                    "Receipt verification disagrees with authorization."
                ) from failure
            if not allowed and verified is not None:
                raise RuntimeError(
                    "Refusal cannot be signed as a policy denial for an allowed call."
                )
            if allowed:
                wire = self.issuer.issue_receipt(verified, tool, True, int(now), call_id, None)
            else:
                wire = self.issuer.issue_denial_receipt(
                    chain,
                    tool,
                    args,
                    int(now),
                    call_id,
                    "scope-denied",
                    pop,
                )
            verify_receipt(wire)
            self.sink.handle(wire)
            from .permissions import SECRET_FIELDS

            def safe(value, depth=0):
                if depth > 3:
                    return "[details omitted]"
                if isinstance(value, dict):
                    return {
                        str(k): (
                            "[redacted]" if SECRET_FIELDS.search(str(k)) else safe(v, depth + 1)
                        )
                        for k, v in list(value.items())[:20]
                    }
                if isinstance(value, list):
                    return [safe(v, depth + 1) for v in value[:20]]
                if isinstance(value, str):
                    return value[:500]
                return value

            return self.event(
                "decision",
                share_id=share_id,
                actor=actor,
                call_id=call_id,
                tool=tool,
                inputs=safe(args) if share_id != "owner" else "[owner inputs omitted]",
                warrant_id=str(chain[-1].id),
                decision="ALLOW" if allowed else "DENY",
                receipt_segment=self.receipt_path.name,
            )

    def list(self, share_id, limit=50, *, actor=None):
        rows = []
        with self.lock:
            for path in (self.root / "activity.previous.jsonl", self.events):
                if path.exists():
                    for line in path.read_text().splitlines():
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if (share_id is None or record.get("share_id") == share_id) and (
                            actor is None or str(record.get("actor")) == str(actor)
                        ):
                            rows.append(record)
        return rows[-min(max(limit, 1), 100) :]
