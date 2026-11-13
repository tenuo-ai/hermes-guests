"""Discovery follows the real Hermes platform catalog, including unfamiliar plugins."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_guests.plugin import GuestsPlugin


@unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "Native Hermes source required")
class NativeCatalogTests(unittest.TestCase):
    def setUp(self):
        from hermes_cli.plugins import get_plugin_manager
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        from tools.registry import registry

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        (home / "config.yaml").write_text("plugins:\n  enabled: [fixture-media]\n")
        plugin = home / "plugins" / "fixture-media"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text(
            "name: fixture-media\nversion: 0.1.0\nkind: standalone\ndescription: Local media fixture\n"
        )
        self.schema = {
            "name": "fixture_library_find",
            "description": "Search an unfamiliar media library.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
        (plugin / "__init__.py").write_text(
            'import json\ndef register(ctx):\n    ctx.register_tool("fixture_library_find","fixture_media",'
            + repr(self.schema)
            + ',lambda args,**kw:json.dumps({"query":args["query"]}))\n'
        )
        token = set_hermes_home_override(str(home))
        self.addCleanup(reset_hermes_home_override, token)
        self.registry = registry
        manager = get_plugin_manager()
        manager.discover_and_load()
        self.assertIsNotNone(registry.get_schema("fixture_library_find"))
        self.addCleanup(registry.deregister, "fixture_library_find", scope=manager.scope_key)

    def config(self, enabled):
        return {
            "platform_toolsets": {"telegram": enabled},
            "known_plugin_toolsets": {"telegram": self.registry.get_registered_toolset_names()},
        }

    def test_summarized_ha_catalog_does_not_verify_a_group_as_one_light(self):
        from hermes_guests.core import GuestsService
        from hermes_guests.model import GuestsError
        from hermes_guests.schemas import GUEST_SCHEMAS
        from hermes_guests.store import FileState

        schemas = list(GUEST_SCHEMAS.values()) + [
            {
                "name": "ha_list_entities",
                "description": "List lights",
                "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}},
            }
        ]
        calls = []

        def dispatch(name, args):
            calls.append((name, args))
            if name == "ha_list_entities":
                return {
                    "entities": [
                        {
                            "entity_id": "light.all_lights",
                            "friendly_name": "All lights",
                            "state": "off",
                        },
                        {"entity_id": "light.porch", "friendly_name": "Porch", "state": "off"},
                    ]
                }
            return {
                "entity_id": args["entity_id"],
                "attributes": (
                    {"entity_id": ["light.porch", "light.office"]}
                    if args["entity_id"] == "light.all_lights"
                    else {}
                ),
            }

        service = GuestsService(FileState(Path(self.temp.name) / "share"), "100")
        plugin = GuestsPlugin(SimpleNamespace(dispatch_tool=dispatch), service=service)
        plugin.installed_tools = lambda: schemas
        plugin.discover("100")
        with self.assertRaisesRegex(GuestsError, "group"):
            plugin.verify_lights("100", ["light.all_lights"])
        plugin.verify_lights("100", ["light.porch"])
        self.assertTrue(service.catalog["light.all_lights"]["group"])
        self.assertFalse(service.catalog["light.porch"]["group"])
        self.assertEqual(
            [args["entity_id"] for name, args in calls if name == "ha_get_state"],
            ["light.all_lights", "light.porch"],
        )

    def test_listing_a_light_tool_as_never_shared_keeps_the_owners_catalog(self):
        from hermes_guests.core import GuestsService
        from hermes_guests.schemas import GUEST_SCHEMAS
        from hermes_guests.store import FileState

        schemas = list(GUEST_SCHEMAS.values()) + [
            {
                "name": "ha_list_entities",
                "description": "List lights",
                "parameters": {"type": "object", "properties": {"domain": {"type": "string"}}},
            }
        ]
        porch = {"entities": [{"entity_id": "light.porch", "friendly_name": "Porch"}]}
        service = GuestsService(FileState(Path(self.temp.name) / "share"), "100")
        plugin = GuestsPlugin(SimpleNamespace(dispatch_tool=lambda n, a: porch), service=service)
        plugin.installed_tools = lambda: schemas
        self.assertIn("light.porch", plugin.discover("100")["lights"])
        service.never_set("100", "tools", "ha_list_entities", True)
        found = plugin.discover("100")
        self.assertEqual(found["lights"], {})  # not offered for sharing
        self.assertNotIn("ha_list_entities", [c["name"] for c in found["catalog"]])
        self.assertIn("light.porch", service.catalog)  # the owner's catalog is untouched
        self.assertIn("light.porch", service.ledger.snapshot()["setup"]["catalog"])
        service.never_set("100", "tools", "ha_list_entities", False)
        self.assertIn("light.porch", plugin.discover("100")["lights"])

    def test_unfamiliar_enabled_plugin_is_discovered_without_an_adapter(self):
        plugin = GuestsPlugin(SimpleNamespace())
        with patch(
            "hermes_cli.config.load_config_readonly", return_value=self.config(["fixture_media"])
        ):
            actual = plugin.resolve_tool("fixture_library_find")
            self.assertEqual(actual["parameters"], self.schema["parameters"])
            self.assertEqual(actual["description"], self.schema["description"])

    def test_native_disabled_toolset_is_not_shareable(self):
        plugin = GuestsPlugin(SimpleNamespace())
        config = self.config(["web"])
        config["agent"] = {"disabled_toolsets": ["fixture_media"]}
        with patch("hermes_cli.config.load_config_readonly", return_value=config):
            self.assertIsNone(plugin.resolve_tool("fixture_library_find"))

    def test_new_native_plugin_default_is_followed(self):
        plugin = GuestsPlugin(SimpleNamespace())
        config = {
            "platform_toolsets": {"telegram": ["web"]},
            "known_plugin_toolsets": {"telegram": []},
        }
        with patch("hermes_cli.config.load_config_readonly", return_value=config):
            self.assertIsNotNone(plugin.resolve_tool("fixture_library_find"))


if __name__ == "__main__":
    unittest.main()
