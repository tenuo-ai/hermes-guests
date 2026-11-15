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
    def __init__(self, root, key, trusted_root, retention_days=90):
        self.root = private_dir(Path(root) / "audit")
        # Readable activity is kept by age, so a busy week does not push out last month.
        self.retention_seconds = int(retention_days) * 86400
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
                self._close_current(item["at"])
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

    def files(self):
        """Readable activity, oldest first: the file from older versions, closed files,
        then the current one."""
        closed = sorted(
            (p for p in self.root.glob("activity.*.jsonl") if p.name.split(".")[1].isdigit()),
            key=lambda p: int(p.name.split(".")[1]),
        )
        return [self.root / "activity.previous.jsonl", *closed, self.events]

    def _close_current(self, now):
        """Close the current file. Closed files are kept, named by when they were closed,
        until they age out. Receipt segments remain separate evidence."""
        stamp = int(now)
        while (self.root / f"activity.{stamp}.jsonl").exists():
            stamp += 1  # never overwrite: two closes in one second get the next name
        os.replace(self.events, self.root / f"activity.{stamp}.jsonl")
        self.prune(now)

    def upkeep(self, now):
        """Apply retention by age however quiet the log is: the current file is closed
        once its first event is a day old, and closed files are removed after the
        retention period. Reading also leaves out anything older."""
        with self.lock:
            try:
                with open(self.events) as stream:
                    first = json.loads(stream.readline() or "{}").get("at")
            except (OSError, ValueError):
                first = None
            if isinstance(first, (int, float)) and first < now - 86400:
                self._close_current(now)
            else:
                self.prune(now)

    def prune(self, now):
        """Remove closed files whose newest event is older than the retention period."""
        for path in self.files()[:-1]:
            try:
                if path.exists() and self._newest(path) < now - self.retention_seconds:
                    path.unlink()
            except OSError:
                pass

    def _newest(self, path):
        """When the newest event in a closed file happened, from the events themselves:
        a restored or copied file keeps its age. Read once per version of the file; the
        file time is used only when no event can be read."""
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        cache = self.__dict__.setdefault("_newest_cache", {})
        if key in cache:
            return cache[key]
        newest = None
        with open(path, "rb") as stream:
            for line in stream:
                try:
                    at = json.loads(line).get("at")
                except (ValueError, AttributeError):
                    continue
                if isinstance(at, (int, float)):
                    newest = at if newest is None else max(newest, at)
        result = newest if newest is not None else stat.st_mtime
        if len(cache) > 1000:
            cache.clear()
        cache[key] = result
        return result

    def list(self, share_id, limit=50, *, actor=None, since=None):
        """Matching events, oldest first: the `limit` newest by when they happened, not by
        file name, so a restored file can't push newer events out. Files whose newest
        event is before the range are skipped unread."""
        import heapq
        import time

        limit = min(max(limit, 1), 1000)
        oldest = time.time() - self.retention_seconds
        since = oldest if since is None else max(since, oldest)
        newest = []
        with self.lock:
            # Newest files first, by their newest event: once the result is full, a file
            # whose newest event is older than everything kept cannot add to it.
            ranked = []
            for index, path in enumerate(self.files()):  # oldest first, as written
                try:
                    if path.exists():
                        top = float("inf") if path == self.events else self._newest(path)
                        ranked.append((top, index, path))
                except OSError:
                    continue
            for top, index, path in sorted(ranked, key=lambda item: item[0], reverse=True):
                if top < since or (len(newest) >= limit and top < newest[0][0]):
                    continue
                try:
                    lines = path.read_text().splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines):
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    at = record.get("at", 0)
                    if (
                        isinstance(at, (int, float))
                        and at >= since
                        and (share_id is None or record.get("share_id") == share_id)
                        and (actor is None or str(record.get("actor")) == str(actor))
                    ):
                        # Equal times keep the order they were written: file, then line.
                        item = (at, (index, number), record)
                        if len(newest) < limit:
                            heapq.heappush(newest, item)
                        elif item[:2] > newest[0][:2]:
                            heapq.heapreplace(newest, item)
        return [record for _, _, record in sorted(newest, key=lambda i: i[:2])]
