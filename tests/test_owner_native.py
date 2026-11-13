"""Whole native owner-to-guest journey in a disposable process and home."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(importlib.util.find_spec("run_agent"), "Native Hermes source required")
class NativeOwnerFlowTests(unittest.TestCase):
    def test_git_plugin_owner_and_guest_use_adjacent_package(self):
        if not (Path(__file__).resolve().parent.parent / "plugin.yaml").is_file():
            self.skipTest("Git-directory journey is verified in source installation tests")
        self.run_flow(git_install=True)

    def test_owner_discovers_prepares_confirms_and_guest_uses_unknown_plugin(self):
        self.run_flow(git_install=False)

    def test_saved_tool_selection_gets_sharing_only_after_owner_enables_it(self):
        self.run_flow(git_install=True, saved_tools=True)

    def test_owner_and_guest_flow_with_both_plugins_enabled(self):
        self.run_flow(git_install=False, tenuo="signed")

    def test_unconfigured_other_plugin_preserves_normal_owner_and_guest_flow(self):
        self.run_flow(git_install=False, tenuo="unconfigured")

    def test_other_plugin_missing_required_warrant_still_blocks_owner_tools(self):
        self.run_flow(git_install=False, tenuo="missing")

    def test_whole_journey_on_a_host_that_serves_several_profiles(self):
        self.run_flow(git_install=False, several_profiles=True)

    def run_flow(self, *, git_install, tenuo=None, several_profiles=False, saved_tools=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                k: v
                for k, v in os.environ.items()
                if k in {"PATH", "PYTHONPATH", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"}
            }
            if git_install:
                env["GUESTS_FIXTURE_GIT"] = "1"
            if saved_tools:
                env["GUESTS_FIXTURE_SAVED_TOOLS"] = "1"
            if tenuo:
                env["GUESTS_FIXTURE_TENUO"] = tenuo
            if several_profiles:
                env["GUESTS_FIXTURE_SEVERAL_PROFILES"] = "1"
            env["HOME"] = str(root / "host")
            env["HERMES_HOME"] = str(root / "hermes")
            env["PYTHONPATH"] = (
                str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
            )
            result = subprocess.run(
                [sys.executable, "-m", "tests.owner_flow_fixture"],
                env=env,
                capture_output=True,
                text=True,
                timeout=80,
            )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-7000:])
            expected = (
                "GLOBAL_REQUIRED_WARRANT_BLOCKED"
                if tenuo == "missing"
                else "OWNER_GUEST_NATIVE_FLOW_OK"
            )
            self.assertIn(expected, result.stdout)


if __name__ == "__main__":
    unittest.main()
