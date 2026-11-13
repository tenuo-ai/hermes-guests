"""A guest may only make the owner's tools reach the public internet.

Hermes checks web addresses for its own tools, and an owner can switch that check off to
reach devices on their own network. Guests do not inherit that choice: before a guest's
call is admitted, every web address in it is looked up, and every answer must be a public
address. The only exception is an address the owner pinned in the reviewed access.

This is a check before the call, of the addresses written in it. What the fetching tool
does afterwards is not seen here: a public page that redirects to a private address, or a
name that changes its answer before the tool's own lookup. Hermes's own tools re-check both
while fetching unless the owner switched that off; other plugins' tools are on their own.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import socket
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlsplit

LOOKUP_SECONDS = 5  # for one name
LOOKUP_BUDGET = 8  # for all the names in one call, well inside a guest turn's time limit
LOOKUP_WORKERS = 4
LOOKUP_THREADS = 16  # lookups still running in this process, however slowly they answer
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_TRANSLATED = ipaddress.ip_network("::ffff:0:0:0/96")
_NUMBER = re.compile(r"(0x[0-9a-f]*|[0-9]+)")
_LOCAL_ENDINGS = (
    ".local",
    ".localhost",
    ".internal",
    ".lan",
    ".home",
    ".home.arpa",
    ".intranet",
    ".corp",
    ".test",
)


_RUNNING = threading.BoundedSemaphore(LOOKUP_THREADS)


class Reading(str):
    """A host that a lenient program could take an input to name, beyond the address as it
    is written: through a quote, a tab or line break, or a sign-in name before an @.

    The address as written must be shown to be public. A reading is only a possibility, so
    it is refused on evidence: an address that is not public, a name kept for private
    networks, or a name that resolves to one. A reading that names nothing a resolver
    answers for is not evidence of anything, and ordinary text produces many of those."""


def lookup(host):
    """Every address `host` resolves to, as text. Raises OSError when it cannot be looked up
    in time.

    A lookup that has not answered in time cannot be stopped; its thread ends when the
    system's resolver gives up. Each one holds a place until then, and when every place is
    taken no further lookup is started, so slow names cannot pile threads up: they are
    simply not looked up, which means not public."""
    if not _RUNNING.acquire(timeout=LOOKUP_SECONDS):
        raise OSError("too many lookups are still running to look up " + host)
    found, failed = [], []

    def work():
        try:
            found.extend(
                info[4][0] for info in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            )
        except Exception as e:  # also a name no resolver can even encode
            failed.append(e)
        finally:
            _RUNNING.release()  # only when the lookup itself has ended

    worker = threading.Thread(target=work, daemon=True)
    try:
        worker.start()
    except BaseException:
        _RUNNING.release()
        raise
    worker.join(LOOKUP_SECONDS)
    if worker.is_alive() or failed or not found:
        raise OSError("could not look up " + host)
    return found


def is_public_address(text):
    """True for an address on the public internet. An IPv6 address that only wraps an IPv4
    one is judged by the IPv4 address inside it."""
    try:
        ip = ipaddress.ip_address(str(text).split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if inner is None and (ip in _NAT64 or ip in _TRANSLATED):
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:
            ip = inner
    # fec0::/10 was retired as "site-local" and is reported as global, but networks that
    # still route it use it for private services.
    return (
        bool(ip.is_global)
        and not ip.is_multicast
        and not getattr(ip, "is_site_local", False)
        # ::/8 and the other reserved blocks: among them the old forms that carry an IPv4
        # address inside, such as ::127.0.0.1 and ::5efe:10.0.0.1.
        and not ip.is_reserved
    )


def is_public_url(url, resolve=None):
    """True when `url` is plain http(s) and its host resolves only to public addresses.
    A host that cannot be looked up is not public, unless `url` is a `Reading`, which is
    refused only on evidence. `resolve` replaces the lookup."""
    reading = isinstance(url, Reading)
    try:
        parts = urlsplit(str(url).strip())
        host = parts.hostname
    except ValueError:
        return False
    if "\\" in parts.netloc:
        # Before the path begins, programs disagree about which host a backslash leaves.
        # Later in the address it is only part of the path or the query.
        return False
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not host
        or parts.username
        or parts.password
    ):
        return False
    host = host.rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
        return is_public_address(host)  # written as an address: no lookup needed
    except ValueError:
        pass
    if not reading and not all(c.isalnum() or c in "._-" for c in host):
        return False  # not a name any registry issues; programs disagree on what it means
    labels = host.split(".")
    if all(_NUMBER.fullmatch(label) for label in labels):
        # 0177.0.0.1, 0x7f.1, 2130706433: programs disagree about which address these are.
        return False
    if len(labels) == 1 or host.endswith(_LOCAL_ENDINGS):
        return False  # a name with no dot, or one kept for private networks
    try:
        answers = (resolve or lookup)(host)
    except OSError:
        # Not looked up, not opened: a proxy setting proves nothing about the route. For a
        # reading, a name nothing answers for is not evidence that it is private.
        return reading
    return all(is_public_address(a) for a in answers)


# A backslash is kept in the address: some parsers read it as a path separator and
# others as part of the host's prefix, so the whole of it has to be judged.
def public_verdicts(addresses, seconds=None):
    """{address: is it public} for all the addresses of one call. Each name is looked up
    once, a few at a time, and the whole call has one time budget: a name not answered
    within it is not public."""
    addresses = _unique(addresses)
    names = []

    def note(host):  # first pass: which names need a lookup at all
        names.append(host)
        raise OSError("not looked up yet")

    for address in addresses:
        is_public_url(address, resolve=note)
    names = list(dict.fromkeys(names))
    answers = {}
    if names:
        pool = ThreadPoolExecutor(max_workers=min(LOOKUP_WORKERS, len(names)))
        try:
            started = {pool.submit(lookup, name): name for name in names}
            done, _ = wait(started, timeout=LOOKUP_BUDGET if seconds is None else seconds)
            for future in done:
                if future.exception() is None:
                    answers[started[future]] = future.result()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)  # nothing queued runs later

    def known(host):
        if host not in answers:
            raise OSError("could not look up " + host)
        return answers[host]

    return {address: is_public_url(address, resolve=known) for address in addresses}


# The scan runs on what a guest sends, so every step of it takes time in proportion to the
# text and no more. A scheme is looked for only where a run of scheme characters begins,
# not again at each character of the run.
_ADDRESS = re.compile(r"(?<![a-z0-9+.\-])[a-z][a-z0-9+.\-]*://[^\s\"'<>]+", re.I)
# What can follow an address and still belong to its host. A host and a sign-in name fit
# well inside this many characters; anything longer is not read, and is refused.
REST_LIMIT = 512
_REST = re.compile(r"(?:\S|[\t\r\n]){0,%d}" % (REST_LIMIT + 1))
UNREADABLE = "too-long-to-inspect"  # not an address, so never a public one
# After a quote: only characters a host is written with, and at least one letter or digit.
_MORE_HOST = re.compile(r"(?=.*[^\W_])[\w.\-:\[\]%]+")
_BARE = re.compile(
    # Only where an address can start: at the beginning, or after a space, quote or bracket.
    r"(?<![^\s\"'(<\[{=,;])(?:file:(?!//)\S+|(?:https?|ftp|wss?|gopher):[/\\](?!/)\S*)",
    re.I,
)
_WRAPPING = re.compile(r"^<?\s*(?:url:)?\s*", re.I)
_AROUND = "".join(map(chr, range(0x21)))  # control characters and space
_BROKEN_UP = dict.fromkeys(map(ord, "\t\r\n"))  # what an address parser quietly takes out


def _host(address):
    """The host a standard parser takes `address` to name, or None."""
    try:
        return (urlsplit(address).hostname or "").rstrip(".").lower() or None
    except ValueError:
        return None


def _reading(text, written, certain=False):
    """What a parser given all of `text` would connect to; None when that is a host already
    found as written. It is a `Reading`, a possibility, unless `certain`: then it is where
    the program will connect, and it is judged like an address as written. Text no parser
    accepts is returned as it is, and so refused."""
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").rstrip(".").lower()
    except ValueError:
        return text
    # Before a space a resolver may stop reading; the name it would see is what counts.
    host = host.split()[0] if host.split() else ""
    if not host or host in written:
        return None
    address = f"{parts.scheme}://{'[' + host + ']' if ':' in host else host}/"
    return address if certain else Reading(address)


def _unique(found):
    """Each address once, in order. Where the same text is both an address as written and
    a reading, it is the address as written."""
    kept = {}
    for item in found:
        if item not in kept or (isinstance(kept[item], Reading) and not isinstance(item, Reading)):
            kept[item] = item
    return list(kept.values())


def _trimmed(address):
    """Without the punctuation of the sentence around it. A closing bracket stays when the
    address opens it, as in https://[2606:4700::1111]."""
    pairs = {")": "(", "]": "[", "}": "{"}
    counts = {c: address.count(c) for c in "()[]{}"}  # once, then kept up to date
    end = len(address)
    while end:
        last = address[end - 1]
        if not (
            last in ".,;:!?)]}\\"
            # Closing quotes, full stops and dashes of other scripts end a sentence, not an address.
            or (ord(last) > 127 and unicodedata.category(last).startswith("P"))
        ):
            break
        if last in pairs:
            if counts[pairs[last]] >= counts[last]:
                break
            counts[last] -= 1
        end -= 1
    return address[:end]


def web_addresses(value, depth=0):
    """Every address found in a call's inputs, however deeply nested and wherever it sits
    in a piece of text: `scheme://...` of any scheme, so that ones which cannot be checked
    are refused, and an address broken up with tabs or line breaks as a parser would read it."""
    if depth > 12:
        return ["nested-too-deeply"]  # not a public address, so the call is refused
    if isinstance(value, dict):
        return [
            u
            for pair in value.items()
            for inner in pair  # an address may be a key as well as a value
            for u in web_addresses(inner, depth + 1)
        ]
    if isinstance(value, (list, tuple, set)):
        return [u for inner in value for u in web_addresses(inner, depth + 1)]
    if not isinstance(value, str):
        return []
    return list(_scan(value))


@functools.lru_cache(maxsize=64)
def _scan(value):
    """The addresses in one piece of text. Kept for the texts seen most recently: a call's
    inputs are scanned before its admission and again inside it, and the second time must
    cost nothing while the saved state is locked."""
    found, written = [], set()
    for m in _ADDRESS.finditer(value):
        found.append(_trimmed(m.group(0)))
        written.add(_host(found[-1]))
        # A quote, tab or line break ends the address in ordinary text, but not for every
        # reader. While the host is still being written (no path yet), what follows may be
        # more of it.
        if re.search(r"[/?#]", m.group(0).split("://", 1)[1]):
            continue
        broken = value[m.end() : m.end() + 1] in ("\t", "\r", "\n")
        rest = _REST.match(value, m.end()).group(0)
        if len(rest) > REST_LIMIT:
            found.append(UNREADABLE)  # more than a host could be; not read, so not let through
            continue
        rest = rest.translate(_BROKEN_UP)
        more = re.split(r"[/?#]", rest)[0]  # what would still belong to the host
        if not rest:
            continue
        if not broken and ("@" in more or _MORE_HOST.fullmatch(more.lstrip("\"'<>"))):
            # Straight after a quote comes an @, or more of a name. A program that takes
            # everything up to the next space, as most do, connects there: that is where
            # this address goes, and it has to be shown to be public.
            found.append(_reading(m.group(0) + rest, written, certain=True))
        elif broken:
            # After a tab or line break in running text, only a parser handed the text
            # whole would join the two: a possibility, refused on evidence.
            found.append(_reading(m.group(0) + rest, written))
    # The input as a parser takes it: tabs and line breaks out, and the control characters
    # and spaces around it off, which parsers drop before they look for the scheme.
    whole = value.translate(_BROKEN_UP).strip(_AROUND)
    if whole != value.strip() and whole.count("://") > value.count("://"):
        # Taking the breaks out completed an address of some scheme: found as usual.
        for m in _ADDRESS.finditer(whole):
            found.append(_trimmed(m.group(0)))
            written.add(_host(found[-1]))
    # When the input begins with an address, a program may hand all of it to its parser.
    # The parser takes tabs and line breaks out, and takes whatever precedes an @ as a
    # sign-in name. If the input is nothing but one address, or the parser finds an @, the
    # host it arrives at is where the program connects, and must be shown to be public.
    # With words after the address and no @, joining them to the host is only a possibility.
    # (The old `<URL:...>` wrapping some openers still accept counts as around it too.)
    unwrapped = _WRAPPING.sub("", whole).strip(_AROUND)
    if _ADDRESS.match(unwrapped):
        try:
            signed_in = "@" in urlsplit(unwrapped).netloc
        except ValueError:
            signed_in = True
        # Closing punctuation after the address is the sentence's, as it is for one in text.
        found.append(
            _reading(
                _trimmed(unwrapped.rstrip(">")),
                written,
                certain=signed_in or not re.search(r"\s", unwrapped),
            )
        )
    # Forms with no `//` that programs still open: a local file, or a web scheme a browser
    # completes. None of them can be shown to be a public address.
    found += [_trimmed(m.group(0)) for m in _BARE.finditer(value)]
    # And when the whole input is such a form once it is read as a program reads it: breaks
    # taken out, and the old `<URL:...>` wrapping some openers still accept taken off.
    if _BARE.match(unwrapped):
        found.append(_trimmed(_BARE.match(unwrapped).group(0)))
    return tuple(_unique(u for u in found if u))


def pinned(entry, field, arguments):
    """Exact addresses approved for this input in a complete matching root rule."""
    from .permissions import permits, rule_options

    if not entry or not isinstance(arguments, dict) or field not in arguments:
        return set()
    named = set()
    for option in rule_options(entry):
        if set(arguments) - set(option):
            continue
        if entry.get("alternatives") and set(arguments) != set(option):
            continue
        if not all(permits(option[key], value) for key, value in arguments.items()):
            continue
        rule = option.get(field, {})
        named.update(web_addresses(rule.get("equals")))
        named.update(web_addresses(rule.get("one_of")))
    return named


def unapproved_addresses(entry, arguments, verdicts):
    """Non-public addresses without a root-approved exception for their actual input."""
    if not isinstance(arguments, dict):
        return {url for url in web_addresses(arguments) if not verdicts.get(url, False)}
    denied = {
        url for key in arguments for url in web_addresses(key) if not verdicts.get(url, False)
    }
    for field, value in arguments.items():
        allowed = pinned(entry, field, arguments)
        denied.update(
            url
            for url in web_addresses(value)
            if not verdicts.get(url, False) and url not in allowed
        )
    return denied
