"""A guest cannot point the owner's tools at addresses that are not on the public internet."""

import tempfile
import time
import unittest
from unittest.mock import patch

from hermes_guests import public_web
from hermes_guests.core import GuestsService
from hermes_guests.model import GuestsError, iso
from hermes_guests.public_web import is_public_address, is_public_url, web_addresses
from hermes_guests.store import FileState
from tests.tiers import slow

PAGE = {
    "name": "page_read",
    "description": "Read a web page.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}
ANSWERS = {
    "example.com": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    "homeassistant.local": ["192.168.1.20"],
    "router.lan": ["10.0.0.1"],
    "both.example": ["93.184.216.34", "10.0.0.5"],  # one public answer, one private
    "sixonly.example": ["fd00::20"],
    "mapped.example": ["::ffff:192.168.1.20"],
    "loop.example": ["127.0.0.1"],
    "meta.example": ["169.254.169.254"],
    "sitelocal.example": ["93.184.216.34", "fec0::1"],
}


def fake_lookup(host):
    if host not in ANSWERS:
        raise OSError("no such host")
    return ANSWERS[host]


def attacker_lookup(host):
    """As fake_lookup, and every name under attacker.example answers with a private address."""
    if "attacker.example" in host or "evil.example" in host:
        return ["192.168.1.20"]
    return fake_lookup(host)


def refused(text, lookup=attacker_lookup):
    """True when the scan finds something in `text` that is not public."""
    with patch.object(public_web, "lookup", lookup):
        found = web_addresses(text)
        return bool(found) and any(not is_public_url(u) for u in found)


class AddressTests(unittest.TestCase):
    def test_only_addresses_on_the_public_internet_are_public(self):
        for address in ("93.184.216.34", "2606:4700::1111"):
            self.assertTrue(is_public_address(address), address)
        for address in (
            "10.0.0.1",
            "192.168.1.1",
            "172.16.5.4",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "198.18.0.1",
            "::1",
            "fc00::1",
            "fe80::1%en0",
            "::ffff:10.0.0.1",  # an IPv4 address wrapped in IPv6
            "64:ff9b::a00:1",  # the same, through an address translator
            "2002:c0a8:101::1",  # and through a 6to4 prefix
            "not an address",
        ):
            self.assertFalse(is_public_address(address), address)

    def test_a_name_is_public_only_if_every_answer_is(self):
        with patch.object(public_web, "lookup", fake_lookup):
            self.assertTrue(is_public_url("https://example.com/a?b=1"))
            self.assertTrue(is_public_url("HTTPS://example.com./"))
            for url in (
                "http://homeassistant.local:8123/",
                "http://router.lan/admin",
                "https://both.example/",
                "http://sixonly.example/",
                "http://mapped.example/",
                "http://loop.example:8080/",
                "http://meta.example/latest/meta-data",
                "http://unknown.invalid/",  # cannot be looked up
                "http://192.168.1.1/",
                "http://0177.0.0.1/",  # numbers programs read differently
                "http://0x7f.1/",
                "http://2130706433/",
                "http://nas/",  # a name with no dot
                "http://printer.internal/",
                "http://[::1]/",
                "http://[::ffff:192.168.1.1]/",
                "https://example.com@10.0.0.1/",  # the real host is after the @
                "https://user:pw@example.com/",
                "ftp://example.com/",
                "file:///etc/hosts",
                "http:///nohost",
            ):
                self.assertFalse(is_public_url(url), url)

    def test_a_proxy_setting_does_not_excuse_a_name_that_cannot_be_looked_up(self):
        with (
            patch.object(public_web, "lookup", fake_lookup),
            patch.dict("os.environ", {"HTTPS_PROXY": "http://proxy:3128"}),
        ):
            self.assertFalse(is_public_url("http://unknown.invalid/"))
            self.assertFalse(is_public_url("http://router.lan/"))
            self.assertTrue(is_public_url("https://example.com/"))

    def test_an_address_nested_too_deeply_to_inspect_is_not_let_through(self):
        deep = "https://example.com/"
        for _ in range(20):
            deep = {"more": [deep]}
        with patch.object(public_web, "lookup", fake_lookup):
            self.assertTrue(any(not is_public_url(u) for u in web_addresses(deep)))

    @slow
    def test_addresses_inside_text_or_broken_up_are_still_found(self):
        self.assertEqual(
            web_addresses("Open http://127.0.0.1/admin now."), ["http://127.0.0.1/admin"]
        )
        self.assertTrue(refused("htt\np://127.0.0.1/private"))
        self.assertEqual(web_addresses("see\nhttp://example.com"), ["http://example.com"])
        self.assertEqual(
            web_addresses("rtsp://camera.lan/1"), ["rtsp://camera.lan/1"]
        )  # refused: not checkable
        self.assertEqual(web_addresses("no address here"), [])
        with patch.object(public_web, "lookup", fake_lookup):
            self.assertFalse(is_public_url("rtsp://camera.lan/1"))
            self.assertFalse(is_public_url("htt\np://127.0.0.1/private"))

    def test_a_backslash_cannot_hide_the_host_an_address_names(self):
        sneaky = "http://93.184.216.34\\@127.0.0.1/private"
        self.assertEqual(web_addresses("open " + sneaky), [sneaky])
        self.assertFalse(is_public_url(sneaky))
        self.assertFalse(is_public_url("http://93.184.216.34\\.evil.example/"))
        # Escaped text around an address does not become part of it.
        self.assertEqual(
            web_addresses('say \\"http://93.184.216.34/a\\"'), ["http://93.184.216.34/a"]
        )

    def test_a_quote_cannot_hide_the_host_an_address_names(self):
        for sneaky in (
            "http://93.184.216.34'@127.0.0.1/private",
            'http://93.184.216.34"@127.0.0.1/private',
            "http://93.184.216.34<x>@127.0.0.1/private",
        ):
            # What a parser would connect to is found, and it is not public.
            self.assertIn("http://127.0.0.1/", web_addresses("open " + sneaky), sneaky)
            self.assertTrue(refused("open " + sneaky), sneaky)
        # The rest of a host's name after a quote, with no @ at all.
        for quote in ("'", '"'):
            sneaky = "http://93.184.216.34" + quote + ".attacker.example/private"
            self.assertTrue(refused("open " + sneaky + " now"), quote)
            with patch.object(public_web, "lookup", lambda host: ["93.184.216.34"]):
                self.assertFalse(is_public_url(sneaky))  # as written, never public
        for ordinary in (
            "'http://93.184.216.34'",
            'say "http://93.184.216.34" please',
            "'http://93.184.216.34', 'http://93.184.216.35'",
            '{"a":"http://93.184.216.34","b":"x"}',
            "(see 'http://93.184.216.34').",
        ):
            self.assertTrue(
                all(
                    u.startswith("http://93.184.216.3") and u[-1].isdigit()
                    for u in web_addresses(ordinary)
                ),
                (ordinary, web_addresses(ordinary)),
            )
        # Quotes around ordinary addresses are still just quotes.
        self.assertEqual(web_addresses("'http://93.184.216.34/a'"), ["http://93.184.216.34/a"])
        self.assertEqual(
            web_addresses('["http://93.184.216.34","http://93.184.216.35/b?c=d@e"]'),
            ["http://93.184.216.34", "http://93.184.216.35/b?c=d@e"],
        )

    def test_an_address_of_any_scheme_completed_by_removing_breaks_is_found(self):
        self.assertEqual(web_addresses("rtsp:\n//127.0.0.1/stream"), ["rtsp://127.0.0.1/stream"])
        self.assertIn("http://127.0.0.1/x", web_addresses("http:/\t/127.0.0.1/x"))
        self.assertFalse(is_public_url("rtsp://127.0.0.1/stream"))

    def test_all_the_names_in_one_call_share_one_time_budget(self):
        import time as clock

        from hermes_guests.public_web import public_verdicts

        asked = []

        def slow(host):
            asked.append(host)
            clock.sleep(0.4)
            return ["93.184.216.34"]

        many = [f"http://host{i}.example/{j}" for i in range(20) for j in range(2)]
        with patch.object(public_web, "lookup", slow):
            started = clock.monotonic()
            verdicts = public_verdicts(many, seconds=0.2)
            self.assertLess(clock.monotonic() - started, 0.35)  # not 20 waits in a row
            self.assertEqual(set(verdicts), set(many))
            self.assertFalse(any(verdicts.values()))  # unanswered in time: not public
            clock.sleep(0.5)
            self.assertLessEqual(len(asked), 4)  # nothing queued was started afterwards
            asked.clear()
            same = public_verdicts(
                ["http://one.example/a", "http://one.example/b", "http://10.0.0.1/"], seconds=2
            )
            self.assertEqual(asked, ["one.example"])  # once per name, none for a plain address
            self.assertEqual(
                same,
                {
                    "http://one.example/a": True,
                    "http://one.example/b": True,
                    "http://10.0.0.1/": False,
                },
            )

    def test_a_tab_or_line_break_cannot_hide_the_host_inside_other_text(self):
        for gap in ("\t", "\n", "\r\n"):
            sneaky = "http://93.184.216.34" + gap + "@127.0.0.1/private"
            self.assertTrue(refused("please open " + sneaky + " for me"), repr(gap))
        # The rest of a host's name can follow a break too, with no @ at all.
        for gap in ("\t", "\n"):
            text = "open http://93.184.216.34" + gap + ".attacker.example/x for me"
            self.assertIn("http://93.184.216.34.attacker.example/", web_addresses(text), repr(gap))
            self.assertTrue(refused(text), repr(gap))
            # Also when the two halves are each a real name: a.co and a.com.
            self.assertTrue(refused("http://93.184.216.34" + gap + "evil.example"), repr(gap))
        # Once the path has begun, a line break cannot change the host any more.
        self.assertEqual(
            web_addresses("see http://93.184.216.34/page\nthen write to me at home"),
            ["http://93.184.216.34/page"],
        )
        # A space ends an address for every reader.
        self.assertEqual(
            web_addresses("see http://93.184.216.34 .attacker.example"), ["http://93.184.216.34"]
        )

    def test_ordinary_text_around_an_address_does_not_get_a_call_refused(self):
        for ordinary in (
            "Summarise https://example.com for me",
            "See “https://example.com”",
            "See ‘https://example.com’ please",
            "Read https://example.com…",
            "阅读 https://example.com。",
            "（https://example.com）",
            "«https://example.com»",
            "https://example.com–",
            "[the page](https://example.com)",
            "https://example.com\nand tell me what it says",
            "https://example.com\n\nSummarise it.",
            "Link: https://example.com\r\nThanks",
            "https://example.com and email me at alex@example.com",
            "see https://example.com\nme@example.com is my address",
            "compare https://example.com vs https://example.com/b",
        ):
            with patch.object(public_web, "lookup", fake_lookup):
                found = web_addresses(ordinary)
                self.assertTrue(found, ordinary)
                self.assertEqual([u for u in found if not is_public_url(u)], [], ordinary)
        # A reading is refused on evidence, and an address as written still has to be shown
        # to be public: the same unknown name is accepted as one and refused as the other.
        self.assertTrue(is_public_url(public_web.Reading("http://nothing.invalid/")))
        with patch.object(public_web, "lookup", fake_lookup):
            self.assertFalse(is_public_url("http://nothing.invalid/"))
            self.assertFalse(is_public_url(public_web.Reading("http://router.lan/")))
            self.assertFalse(is_public_url(public_web.Reading("http://loop.example/")))
            self.assertFalse(is_public_url(public_web.Reading("http://10.0.0.1/")))
        # Found both ways, the stricter judgement is the one that counts.
        both = public_web._unique([public_web.Reading("http://x.invalid/"), "http://x.invalid/"])
        self.assertEqual([type(u) for u in both], [str])

    def test_where_an_input_that_is_one_address_connects_must_be_shown_to_be_public(self):
        def nothing_resolves(host):
            raise OSError("this machine cannot look names up, as behind a resolving proxy")

        slow = OSError("timed out")

        def times_out(host):
            raise slow

        for lookup in (nothing_resolves, times_out):
            for whole in (
                "http://93.184.216.34'.attacker.example/private",
                'http://93.184.216.34".attacker.example/private',
                "http://93.184.216.34\n.attacker.example/private",
                "http://93.184.216.34\tattacker.example",
                "http://93.184.216.34 @intranet.attacker.example/x",
                "http://93.184.216.34'@intranet.attacker.example/x",
            ):
                self.assertTrue(refused(whole, lookup), (whole, lookup.__name__))
                self.assertTrue(refused({"url": whole}, lookup), (whole, lookup.__name__))
            # Inside prose, the quote forms name a host for anything that reads to the next
            # space, so they are refused as well.
            for prose in (
                "open http://93.184.216.34'.attacker.example/private now",
                "open http://93.184.216.34'@intranet.attacker.example/x now",
            ):
                self.assertTrue(refused(prose, lookup), (prose, lookup.__name__))
        # A line break in prose stays a possibility: nothing but a whole-input parser joins it.
        with patch.object(public_web, "lookup", fake_lookup):
            found = web_addresses("https://example.com\nand tell me what it says")
            self.assertEqual([u for u in found if not is_public_url(u)], [])

    def test_a_local_file_is_refused_however_an_opener_would_still_read_it(self):
        from urllib.request import Request

        for odd in (
            "file:\n/etc/passwd",
            "fi\nle:/etc/passwd",
            "<URL:file:/etc/passwd>",
            "URL:file:/etc/passwd",
            "url: file:/etc/passwd",
            "<file:/etc/passwd>",
            " \x0bfile:/etc/passwd",
        ):
            found = web_addresses(odd)
            self.assertTrue(found and any(not is_public_url(u) for u in found), repr(odd))
        # The standard opener really does read these as local files.
        for opened in ("file:\n/etc/passwd", "<URL:file:/etc/passwd>", "URL:file:/etc/passwd"):
            self.assertEqual(Request(opened).type, "file", repr(opened))
        # Ordinary text that only mentions a file is not an address.
        for plain in ("my file:\nnotes.txt", "see the URL: example", "the url:\nhttp is a scheme"):
            self.assertEqual(web_addresses(plain), [], repr(plain))

    def test_a_backslash_after_the_host_does_not_make_an_ordinary_address_suspect(self):
        with patch.object(public_web, "lookup", fake_lookup):
            for ordinary in (
                "https://example.com/a\\b",
                "https://example.com/?q=C:\\Users\\alex",
                "https://example.com/x#a\\b",
            ):
                found = web_addresses(ordinary)
                self.assertTrue(found and all(is_public_url(u) for u in found), ordinary)
            for sneaky in (
                "http://example.com\\@127.0.0.1/x",
                "http://example.com\\.router.lan/",
                "http://\\router.lan/",
            ):
                found = web_addresses(sneaky)
                self.assertTrue(found and any(not is_public_url(u) for u in found), sneaky)

    def test_forms_without_a_double_slash_that_programs_still_open_are_refused(self):
        for odd in (
            "file:/tmp/synthetic.txt",
            "FILE:tmp/x",
            "http:/127.0.0.1/x",
            "https:\\\\10.0.0.1",
        ):
            found = web_addresses("read " + odd + " now")
            self.assertEqual(len(found), 1, odd)
            self.assertFalse(is_public_url(found[0]), odd)
        for plain in (
            "a profile: /home/alex",
            "ratio 3:/4",
            "my file: notes.txt",
            "the http: prefix",
            "C:/Users/alex",
            "see http://93.184.216.34/file:/x",
        ):
            self.assertEqual(
                [u for u in web_addresses(plain) if not u.startswith("http://93.")], [], plain
            )

    def test_what_a_parser_strips_from_around_an_address_does_not_hide_where_it_goes(self):
        def nothing_resolves(host):
            raise OSError("cannot look names up")

        tricks = (
            "http://93.184.216.34\n.attacker.example/x",
            "http://93.184.216.34\tattacker.example",
            "http://93.184.216.34 @intranet.attacker.example/x",
        )
        for around in ("\x00", "\x01", "\x1f", " \x0b", "<", "URL:", "<URL:", "url: "):
            for trick in tricks:
                for lookup in (nothing_resolves, attacker_lookup):
                    self.assertTrue(refused(around + trick, lookup), (around, trick))
        # The same wrapping around an ordinary address changes nothing.
        with patch.object(public_web, "lookup", fake_lookup):
            for around in ("\x01", "<URL:", " "):
                found = web_addresses(around + "https://example.com/page")
                self.assertEqual([u for u in found if not is_public_url(u)], [], around)

    def test_old_address_forms_that_carry_a_private_address_inside_are_not_public(self):
        for address in ("::127.0.0.1", "::7f00:1", "::5efe:10.0.0.1", "::10.0.0.1", "100::1"):
            self.assertFalse(is_public_address(address), address)
            self.assertFalse(is_public_url("http://[" + address + "]/"), address)
        for address in ("2606:4700::1111", "2a00:1450:4001:81b::200e"):
            self.assertTrue(is_public_address(address), address)

    def test_retired_site_local_addresses_are_not_public(self):
        for address in ("fec0::1", "feff::abcd", "fec0::1%en0"):
            self.assertFalse(is_public_address(address), address)
        with patch.object(public_web, "lookup", fake_lookup):
            self.assertFalse(is_public_url("http://[fec0::1]/admin"))
            self.assertFalse(is_public_url("http://sitelocal.example/"))  # one of its answers

    def test_an_input_that_is_one_address_is_judged_as_a_parser_reads_all_of_it(self):
        for gap in (" ", "  ", "\x0b", "\x0c", "\u00a0", "\u2028", "\u3000", " \t"):
            for tail in ("@127.0.0.1/x", ":80@127.0.0.1/x", "@[::1]/x"):
                sneaky = "http://93.184.216.34" + gap + tail
                found = web_addresses(sneaky)
                self.assertTrue(found and any(not is_public_url(u) for u in found), repr(sneaky))
                self.assertTrue(any(not is_public_url(u) for u in web_addresses({"url": sneaky})))
        # An address with words after it, or an @ only later in its path, is ordinary.
        for ordinary in (
            "http://93.184.216.34 is the page I mean",
            "http://93.184.216.34/users/@alex",
            "http://93.184.216.34/a?to=alex@example.com",
        ):
            found = web_addresses(ordinary)
            self.assertTrue(all(is_public_url(u) for u in found), (ordinary, found))

    def test_the_scan_takes_time_in_proportion_to_the_text_and_no_more(self):
        import time as clock

        size = 64_000  # about the most one call can carry
        shapes = {
            "letters with no address": "a" * size,
            "dots and letters": "a." * (size // 2),
            "addresses joined by quotes": "a://b'" * (size // 6),
            "addresses joined by tabs": "a://b\t" * (size // 6),
            "addresses joined by angle brackets": "a://b<" * (size // 6),
            "a quote then one long word": "http://x'" + "y" * size,
            "a long run of closing brackets": "http://x/" + ")" * size,
            "a long run of full stops": "http://x/" + "." * size,
            "many short addresses": "a://b " * (size // 6),
            "a long scheme-like run": "a" * size + "://x",
        }
        for name, text in shapes.items():
            public_web._scan.cache_clear()
            started = clock.perf_counter()
            first = web_addresses(text)
            took = clock.perf_counter() - started
            # Each of these took between a few and a dozen seconds when a step was quadratic.
            self.assertLess(took, 1.0, name)
            started = clock.perf_counter()
            self.assertEqual(web_addresses({"k": [text]}), first, name)  # scanned once, kept
            self.assertLess(clock.perf_counter() - started, 0.05, name)
        # What follows an address beyond the length of any host is not read, and is refused.
        self.assertTrue(refused("http://93.184.216.34'" + "y" * 600 + "@127.0.0.1/x"))
        self.assertEqual(
            web_addresses("http://93.184.216.34/a" + ")" * 50), ["http://93.184.216.34/a"]
        )
        self.assertEqual(
            web_addresses("http://93.184.216.34/a_(b)" + ")" * 3), ["http://93.184.216.34/a_(b)"]
        )

    def test_lookups_that_never_answer_cannot_pile_up(self):
        import threading

        release, calls = threading.Event(), []

        def stuck(host, *args, **kwargs):
            calls.append(host)
            release.wait(5)
            return [(None, None, None, None, ("93.184.216.34", 0))]

        with (
            patch.object(public_web, "_RUNNING", threading.BoundedSemaphore(2)),
            patch.object(public_web, "LOOKUP_SECONDS", 0.05),
            patch.object(public_web.socket, "getaddrinfo", stuck),
        ):
            for name in ("one.example", "two.example", "three.example", "four.example"):
                with self.assertRaises(OSError):
                    public_web.lookup(name)  # none answers in time
            self.assertEqual(calls, ["one.example", "two.example"])  # the rest never started
            self.assertFalse(is_public_url("http://five.example/"))  # and so are not public
            release.set()
            for _ in range(100):  # the stuck lookups end and give their places back
                if public_web._RUNNING._value == 2:
                    break
                threading.Event().wait(0.01)
            self.assertEqual(public_web.lookup("six.example"), ["93.184.216.34"])

    def test_an_address_used_as_a_key_is_found(self):
        self.assertEqual(
            web_addresses({"http://127.0.0.1/admin": "read", "ok": [{"http://10.0.0.1/": 1}]}),
            ["http://127.0.0.1/admin", "http://10.0.0.1/"],
        )

    def test_a_bracket_that_belongs_to_the_address_is_kept(self):
        self.assertEqual(web_addresses("https://[2606:4700::1111]"), ["https://[2606:4700::1111]"])
        self.assertEqual(
            web_addresses("(see https://[2606:4700::1111].)"), ["https://[2606:4700::1111]"]
        )
        self.assertEqual(web_addresses("[http://127.0.0.1/a]"), ["http://127.0.0.1/a"])
        self.assertEqual(web_addresses("at http://[::1]/x)."), ["http://[::1]/x"])
        self.assertTrue(is_public_url("https://[2606:4700::1111]"))
        self.assertFalse(is_public_url("http://[::1]/x"))

    def test_addresses_are_found_wherever_they_are_in_a_call(self):
        found = web_addresses(
            {
                "a": ["x", {"b": " HTTP://one.example/ "}],
                "c": "https://two.example",
                "d": "ftp://no",
                "e": 5,
            }
        )
        # Other schemes are found as well, so that they are refused and not passed over.
        self.assertEqual(found, ["HTTP://one.example/", "https://two.example", "ftp://no"])


class GuestCallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.effects = []
        self.s = GuestsService(
            FileState(self.temp.name),
            "100",
            catalog={},
            dispatch=lambda tool, args, envelope: self.effects.append(args) or {"ok": True},
            tool_resolver={"page_read": PAGE}.get,
            clock=time.time,
        )
        patcher = patch.object(public_web, "lookup", fake_lookup)
        patcher.start()
        self.addCleanup(patcher.stop)

    def activate(self, rule):
        scope = {
            "tools": [{"name": "page_read", "constraints": {"url": rule}}],
            "expires_at": iso(time.time() + 3600),
        }
        p = self.s.prepare("100", "Alex", scope)
        claim = self.s.claim(self.s.approve("100", p["proposal_id"])["token"], "200", "Alex")
        return self.s.accept_claim("100", claim["claim_id"])["share_id"]

    def test_guest_cannot_reach_the_owners_network_through_a_shared_tool(self):
        sid = self.activate({"any": True})
        self.s.call("200", sid, "page_read", {"url": "https://example.com/news"})
        for url in (
            "http://homeassistant.local:8123/api/states",
            "http://192.168.1.1/",
            "https://both.example/",
            "http://unknown.invalid/",
        ):
            with self.assertRaises(GuestsError) as refused:
                self.s.call("200", sid, "page_read", {"url": url})
            self.assertIn("not on the public internet", str(refused.exception))
        self.assertEqual(self.effects, [{"url": "https://example.com/news"}])
        reasons = [
            r.get("reason") for r in self.s.activity("100", sid) if r["kind"] == "adapter_blocked"
        ]
        self.assertEqual(reasons, ["address-not-public"] * 4)

    def test_a_subtask_cannot_exempt_an_address_by_narrowing_itself_to_it(self):
        sid = self.activate({"any": True})
        private = "http://homeassistant.local:8123/api/states"
        child = self.s.delegate(
            "200",
            sid,
            {"tools": [{"name": "page_read", "constraints": {"url": {"equals": private}}}]},
            task="Read one page",
        )
        with self.assertRaises(GuestsError) as refused:
            self.s.call("200", sid, "page_read", {"url": private}, delegation=child["task_id"])
        self.assertIn("not on the public internet", str(refused.exception))
        self.assertEqual(self.effects, [])

    def test_an_address_the_access_fills_in_is_checked_too(self):
        sid = self.activate({"any": True})
        private = "http://homeassistant.local:8123/api/states"
        child = self.s.delegate(
            "200",
            sid,
            {"tools": [{"name": "page_read", "constraints": {"url": {"equals": private}}}]},
            task="Read one page",
        )
        with self.assertRaises(GuestsError) as refused:
            self.s.call(
                "200", sid, "page_read", {}, delegation=child["task_id"]
            )  # no address given: it is filled in
        self.assertIn("not on the public internet", str(refused.exception))
        self.assertEqual(self.effects, [])

    def test_an_address_inside_other_text_is_refused_and_shown_to_the_owner(self):
        sid = self.activate({"any": True})
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "page_read", {"url": "Open http://router.lan/admin please"})
        self.assertEqual(self.effects, [])
        from hermes_guests.plugin import activity_text

        shown = activity_text("Alex", self.s.activity("100", sid), "UTC")
        self.assertIn("page_read", shown)
        self.assertIn("router.lan", shown)
        self.assertIn("refused (not a public address)", shown)

    def test_an_address_the_owner_named_in_the_review_is_the_owners_choice(self):
        sid = self.activate({"equals": "http://homeassistant.local:8123/camera"})
        self.s.call("200", sid, "page_read", {"url": "http://homeassistant.local:8123/camera"})
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(GuestsError):
            self.s.call(
                "200", sid, "page_read", {"url": "http://homeassistant.local:8123/api/states"}
            )
        self.assertEqual(len(self.effects), 1)

    def activate_selections(self, schema, selections):
        self.s.tool_resolver = {schema["name"]: schema}.get
        p = self.s.prepare("100", "Alex", {"tools": selections, "duration_seconds": 3600})
        invitation = self.s.approve("100", p["proposal_id"])
        claim = self.s.claim(invitation["token"], "200", "Alex")
        return self.s.accept_claim("100", claim["claim_id"])["share_id"]

    def test_pin_in_a_note_does_not_exempt_an_unrestricted_destination(self):
        camera = "http://homeassistant.local:8123/camera"
        note = "Reference " + camera
        spec = {
            **PAGE,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"url": {"type": "string"}, "note": {"type": "string"}},
                "required": ["url", "note"],
            },
        }
        rules = {"url": {"any": True}, "note": {"equals": note}}
        sid = self.activate_selections(spec, [{"name": "page_read", "constraints": rules}])
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "page_read", {"url": camera, "note": note})
        self.assertEqual(self.effects, [])
        self.s.call("200", sid, "page_read", {"url": "https://example.com/", "note": note})
        self.assertEqual(len(self.effects), 1)
        child = self.s.delegate(
            "200",
            sid,
            {
                "tools": [
                    {
                        "name": "page_read",
                        "constraints": {"url": {"equals": camera}, "note": {"equals": note}},
                    }
                ]
            },
            task="Read the page",
        )
        with self.assertRaises(GuestsError):
            self.s.call(
                "200", sid, "page_read", {"url": camera, "note": note}, delegation=child["task_id"]
            )
        self.assertEqual(len(self.effects), 1)

    def test_pin_in_another_complete_rule_does_not_exempt_the_call(self):
        camera = "http://homeassistant.local:8123/camera"
        spec = {
            **PAGE,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"url": {"type": "string"}, "mode": {"type": "string"}},
                "required": ["url", "mode"],
            },
        }
        sid = self.activate_selections(
            spec,
            [
                {
                    "name": "page_read",
                    "constraints": {"url": {"equals": camera}, "mode": {"equals": "internal"}},
                },
                {
                    "name": "page_read",
                    "constraints": {"url": {"any": True}, "mode": {"equals": "public"}},
                },
            ],
        )
        self.s.call("200", sid, "page_read", {"request": [{"url": camera, "mode": "internal"}]})
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "page_read", {"request": [{"url": camera, "mode": "public"}]})
        self.assertEqual(len(self.effects), 1)
        self.s.call(
            "200",
            sid,
            "page_read",
            {"request": [{"url": "https://example.com/", "mode": "public"}]},
        )
        self.assertEqual(len(self.effects), 2)

    def test_exact_url_list_pin_stays_bound_to_its_input(self):
        camera = "http://homeassistant.local:8123/camera"
        spec = {
            **PAGE,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "url": {"type": "string"},
                    "references": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["url", "references"],
            },
        }
        sid = self.activate_selections(
            spec,
            [
                {
                    "name": "page_read",
                    "constraints": {
                        "url": {"any": True},
                        "references": {"equals": [camera]},
                    },
                }
            ],
        )
        self.s.call(
            "200", sid, "page_read", {"url": "https://example.com/", "references": [camera]}
        )
        self.assertEqual(len(self.effects), 1)
        with self.assertRaises(GuestsError):
            self.s.call("200", sid, "page_read", {"url": camera, "references": [camera]})
        self.assertEqual(len(self.effects), 1)

    def test_a_page_the_owner_named_for_the_page_reader_is_read_and_research_stays_public(self):
        from hermes_guests.schemas import GUEST_SCHEMAS

        self.s.tool_resolver = GUEST_SCHEMAS.get
        camera = "http://homeassistant.local:8123/camera"

        def give(guest, scope):
            p = self.s.prepare("100", "Guest " + guest, dict(scope, duration_seconds=3600))
            invitation = self.s.approve("100", p["proposal_id"])
            claim = self.s.claim(invitation["token"], guest, "Guest")
            return self.s.accept_claim("100", claim["claim_id"])["share_id"]

        named = give(
            "200",
            {"tools": [{"name": "web_extract", "constraints": {"urls": {"equals": [camera]}}}]},
        )
        self.s.call("200", named, "web_extract", {"urls": [camera]})
        self.assertEqual(self.effects, [{"urls": [camera]}])
        # A named page that ends in punctuation is still the named page.
        for n, page in enumerate(
            ("http://homeassistant.local:8123/report.", "http://router.lan/list,")
        ):
            other = give(
                str(210 + n),
                {"tools": [{"name": "web_extract", "constraints": {"urls": {"equals": [page]}}}]},
            )
            self.s.call(str(210 + n), other, "web_extract", {"urls": [page]})
            self.assertEqual(self.effects[-1], {"urls": [page]})
        del self.effects[1:]
        research = give("201", {"research": True})
        with self.assertRaises(GuestsError):
            self.s.call("201", research, "web_extract", {"urls": [camera]})
        self.assertEqual(len(self.effects), 1)


if __name__ == "__main__":
    unittest.main()
