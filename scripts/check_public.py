"""Check that tracked files contain only content suitable for a public repository.

Flags personal email addresses, local paths, tailnet hostnames, credentials, and
links to repositories other than the public ones listed here. Usage:

    python scripts/check_public.py [--extra-patterns FILE]

FILE holds additional regular expressions, one per line, matched
case-insensitively. Blank lines and lines starting with # are ignored.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PUBLIC_REPOSITORIES = {"hermes-guests", "hermes-tenuo", "tenuo"}
ALLOWED_EMAILS = {"security@tenuo.ai", "git@github.com"}
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "example", "invalid", "test")
PLACEHOLDER_USERS = {"alex", "blair", "user", "you", "example", "runner"}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
LOCAL_PATH = re.compile(
    r"(?:/Users/|/home/|[A-Za-z]:[/\\]+Users[/\\]+)([A-Za-z0-9._-]+)", re.IGNORECASE
)
# Screenshots and recordings can't be checked as text. Review them before publishing.
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm", ".mov"}
TAILNET_HOST = re.compile(r"\b[A-Za-z0-9-]+\.[A-Za-z0-9-]+\.ts\.net\b")
REPOSITORY = re.compile(r"\btenuo-ai/([A-Za-z0-9_.-]+)", re.IGNORECASE)
SECRETS = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}"
    r"|\bxox[abp]-[A-Za-z0-9-]{10,}"
    r"|\b[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}"
)


def tracked_files():
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout
    return [ROOT / name for name in output.decode().split("\0") if name]


def email_allowed(address):
    address = address.lower()
    domain = address.rsplit("@", 1)[1]
    return (
        address in ALLOWED_EMAILS
        or domain.endswith("users.noreply.github.com")
        or any(domain == d or domain.endswith("." + d) for d in EXAMPLE_DOMAINS)
    )


def line_findings(line, extra):
    for match in EMAIL.finditer(line):
        if not email_allowed(match.group(0)):
            yield f"email address {match.group(0)}"
    for match in LOCAL_PATH.finditer(line):
        if match.group(1).lower() not in PLACEHOLDER_USERS:
            yield f"local path {match.group(0)}"
    for match in TAILNET_HOST.finditer(line):
        yield f"tailnet hostname {match.group(0)}"
    for match in REPOSITORY.finditer(line):
        name = match.group(1).lower().rstrip(".").removesuffix(".git")
        if name not in PUBLIC_REPOSITORIES:
            yield f"link to non-public repository tenuo-ai/{name}"
    if SECRETS.search(line):
        yield "credential-like value"
    for pattern in extra:
        if pattern.search(line):
            yield f"matches extra pattern {pattern.pattern!r}"


def check(paths, extra=()):
    findings = []
    for path in paths:
        if path.suffix.lower() in MEDIA_SUFFIXES and not path.is_symlink():
            continue
        try:
            # Git stores a symlink's target, so check that instead of following it.
            text = (
                str(path.readlink())
                if path.is_symlink()
                else path.read_bytes().decode("utf-8", errors="replace")
            )
        except (IsADirectoryError, FileNotFoundError):
            continue
        relative = path.relative_to(ROOT)
        for number, line in enumerate(text.splitlines(), 1):
            findings.extend(
                f"{relative}:{number}: {finding}" for finding in line_findings(line, extra)
            )
    return findings


def load_patterns(path):
    patterns = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            patterns.append(re.compile(line, re.IGNORECASE))
    return patterns


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--extra-patterns", help="File of additional regular expressions")
    args = parser.parse_args(argv)
    extra = load_patterns(args.extra_patterns) if args.extra_patterns else []
    findings = check(tracked_files(), extra)
    for finding in findings:
        print(finding)
    if findings:
        print(
            f"{len(findings)} finding(s). Remove the content or use a placeholder.", file=sys.stderr
        )
        return 1
    print("Public content check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
