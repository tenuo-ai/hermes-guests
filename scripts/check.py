"""Repeatable pre-human-test gate. Run with Hermes and .[test] installed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    print("Checking: " + " ".join(args), flush=True)
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def main():
    if importlib.util.find_spec("run_agent") is None:
        raise SystemExit(
            "Native Hermes is required for the readiness gate. Install it in this environment or set PYTHONPATH to its checkout."
        )
    run("scripts/check_public.py")
    run("-m", "ruff", "check", "hermes_guests", "tests", "scripts")
    run("-m", "ruff", "format", "--check", "hermes_guests", "tests", "scripts")
    run("-m", "unittest", "discover", "-s", "tests", "-v")
    run("experiments/released_guard.py")
    with tempfile.TemporaryDirectory(prefix="guests-wheel-") as directory:
        run("-m", "build", "--wheel", "--outdir", directory)
    print(
        "Automated readiness checks passed. Live Telegram and service acceptance are separate human tests."
    )


if __name__ == "__main__":
    main()
