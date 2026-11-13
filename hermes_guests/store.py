"""Use Hermes PluginState in production; FileState is a disposable local test adapter."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import fcntl
import json
import os
import tempfile
import threading
from pathlib import Path

from .model import GuestsError


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise GuestsError("Unsafe storage path.")
    os.chmod(path, 0o700)
    return path


def atomic_private(path, data):
    path = Path(path)
    private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".share-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class FileState:
    """Test/standalone adapter with the same get/set boundary as ctx.state."""

    def __init__(self, directory):
        self.data_dir = private_dir(directory)
        self.path = self.data_dir / "state.json"
        self._write_lock = threading.Lock()

    def get(self, key, default=None):
        if not self.path.exists():
            return copy.deepcopy(default)
        try:
            return json.loads(self.path.read_text()).get(key, copy.deepcopy(default))
        except (ValueError, OSError) as exc:
            raise GuestsError(
                "Saved access could not be verified; guest actions are paused."
            ) from exc

    def set(self, key, value):
        with self._write_lock:
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            data[key] = value
            atomic_private(self.path, json.dumps(data).encode())


class Ledger:
    def __init__(self, state):
        self.state = state
        self.root = private_dir(state.data_dir)
        self._context = contextvars.copy_context()
        self._thread_lock = threading.RLock()
        self._open = threading.local()

    def read(self):
        """The saved state for reading. Inside a transaction on this thread it is the state
        that transaction holds; taking the file lock a second time would wait forever."""
        held = getattr(self._open, "value", None)
        return held if held is not None else self.snapshot()

    @contextlib.contextmanager
    def transaction(self):
        with self._thread_lock, open(self.root / ".mutation.lock", "a+b") as lock:
            os.chmod(self.root / ".mutation.lock", 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            held = getattr(self._open, "value", None)
            try:
                value = self._context.copy().run(self.state.get, "access", None)
                if value is None:
                    value = {
                        "version": 1,
                        "shares": {},
                        "proposals": {},
                        "invites": {},
                        "setup": {},
                    }
                if not isinstance(value, dict) or value.get("version") != 1:
                    raise GuestsError("Saved access is unavailable; guest actions are paused.")
                self._open.value = value
                yield value
                self._context.copy().run(self.state.set, "access", value)
            finally:
                self._open.value = held
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def snapshot(self):
        with self.transaction() as value:
            return copy.deepcopy(value)
