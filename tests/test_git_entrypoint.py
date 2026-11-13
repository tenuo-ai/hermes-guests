"""A Git directory entrypoint must work without an installed Hermes Guests distribution."""

import importlib.util
import sys
import unittest
from pathlib import Path


class GitEntrypointTests(unittest.TestCase):
    def test_host_loads_the_package_beside_the_git_manifest(self):
        root = Path(__file__).resolve().parent.parent
        if not (root / "plugin.yaml").exists():
            self.skipTest("Git entrypoint is verified in source installation tests")
        name = "fixture_share_git_entry"
        spec = importlib.util.spec_from_file_location(
            name, root / "__init__.py", submodule_search_locations=[str(root)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(
            lambda: [
                sys.modules.pop(k, None)
                for k in list(sys.modules)
                if k == name or k.startswith(name + ".")
            ]
        )
        spec.loader.exec_module(module)
        calls = []

        class Context:
            def __getattr__(self, attribute):
                if attribute.startswith("register_"):
                    return lambda *args, **kwargs: calls.append((attribute, args))
                if attribute == "spawn_task":
                    return lambda *args, **kwargs: None
                raise AttributeError(attribute)

        module.register(Context())
        hook = next(args[1] for kind, args in calls if kind == "register_hook")
        self.assertTrue(hook.__self__.__class__.__module__.startswith(name + ".hermes_guests"))
        self.assertIn(
            "guests_prepare_access", [args[0] for kind, args in calls if kind == "register_tool"]
        )
