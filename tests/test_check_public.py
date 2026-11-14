import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_public.py"
spec = importlib.util.spec_from_file_location("check_public", SCRIPT)
check_public = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_public)


def findings(line, extra=()):
    return list(check_public.line_findings(line, extra))


class PublicContentTests(unittest.TestCase):
    def test_flags_internal_content(self):
        # Built from parts so this file passes the check it tests.
        for line in (
            "mail " + "someone" + "@" + "personal-mail.net",
            "path /" + "Users/" + "someone/project",
            "host box.tail1234" + ".ts.net",
            "see tenuo-ai/" + "private-planning",
            "key ghp_" + "a" * 30,
            "see https://github.com/Tenuo-AI/" + "private-planning",
            "c:\\" + "users\\someone\\file",
            '{"path": "C:\\\\' + 'Users\\\\someone\\\\file"}',
        ):
            self.assertTrue(findings(line), line)

    def test_allows_public_content(self):
        for line in (
            "Report to security@tenuo.ai",
            "alex@example.com",
            "C:/Users/alex",
            "hermes plugins install tenuo-ai/hermes-guests --enable",
            "https://github.com/tenuo-ai/tenuo/blob/main/SECURITY.md",
            "https://github.com/tenuo-ai/hermes-guests.git",
            "git clone git@github.com:tenuo-ai/hermes-guests.git",
            "https://github.com/Tenuo-AI/Hermes-Guests",
            "Clone https://github.com/tenuo-ai/hermes-guests.git.",
        ):
            self.assertEqual(findings(line), [], line)

    def test_checks_symlink_targets(self):
        with tempfile.TemporaryDirectory(dir=check_public.ROOT) as directory:
            link = Path(directory) / "link"
            link.symlink_to("/" + "home/someone/private.txt")
            self.assertTrue(check_public.check([link]))

    def test_scans_files_that_are_not_utf8(self):
        with tempfile.TemporaryDirectory(dir=check_public.ROOT) as directory:
            path = Path(directory) / "data.csv"
            path.write_bytes(("caf\u00e9,someone" + "@" + "personal-mail.net").encode("cp1252"))
            self.assertTrue(check_public.check([path]))

    def test_skips_media_but_not_text_or_media_symlinks(self):
        secret = "someone" + "@" + "personal-mail.net"
        with tempfile.TemporaryDirectory(dir=check_public.ROOT) as directory:
            root = Path(directory)
            (root / "shot.PNG").write_bytes(b"\x89PNG " + secret.encode())
            (root / "clip.mp4").write_bytes(secret.encode())
            (root / "notes.txt").write_text(secret)
            (root / "link.png").symlink_to("/" + "home/someone/shot.png")
            found = check_public.check(sorted(root.iterdir()))
        self.assertEqual(
            sorted(line.split(":", 1)[0].rsplit("/", 1)[1] for line in found),
            ["link.png", "notes.txt"],
        )

    def test_extra_patterns(self):
        pattern = check_public.re.compile("codename", check_public.re.IGNORECASE)
        self.assertTrue(findings("Project CODENAME", [pattern]))

    def test_repository_is_clean(self):
        self.assertEqual(check_public.check(check_public.tracked_files()), [])


if __name__ == "__main__":
    unittest.main()
