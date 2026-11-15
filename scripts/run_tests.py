"""Run the test suite with each test file in its own process, several at once.

    python scripts/run_tests.py            # everything, in parallel
    python scripts/run_tests.py --fast     # skip the slow tier (tests/tiers.py)
    python scripts/run_tests.py menu cards # only files whose names contain these words

Files share no state, so running them side by side gives the same result as
`python -m unittest discover -s tests`, in about the time of the slowest file."""

import argparse
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(module, env):
    began = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-m", "unittest", "tests." + module],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    return module, done.returncode, done.stderr, time.monotonic() - began


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("only", nargs="*", help="run only files whose names contain these")
    parser.add_argument("--fast", action="store_true", help="skip the slow tier")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()
    modules = sorted(
        p.stem
        for p in (ROOT / "tests").glob("test_*.py")
        if not args.only or any(word in p.stem for word in args.only)
    )
    env = dict(os.environ, **({"HERMES_GUESTS_FAST": "1"} if args.fast else {}))
    began = time.monotonic()
    total = skipped = 0
    failed = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for module, code, err, seconds in pool.map(lambda m: run(m, env), modules):
            ran = re.search(r"^Ran (\d+) test", err, re.M)
            total += int(ran.group(1)) if ran else 0
            skips = re.search(r"skipped=(\d+)", err)
            skipped += int(skips.group(1)) if skips else 0
            if code:
                failed.append(module)
                print(f"--- {module} failed ({seconds:.1f}s)")
                print(
                    "\n".join(
                        line
                        for line in err.splitlines()
                        if re.match(r"(ERROR|FAIL):|\w*Error\b|AssertionError", line)
                    )[:4000]
                )
    print(
        f"{total} tests in {len(modules)} files, {skipped} skipped, "
        f"{time.monotonic() - began:.1f}s: " + ("FAILED " + ", ".join(failed) if failed else "OK")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
