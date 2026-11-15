"""Lifecycle housekeeping and recovery preserve boundaries and unrelated state."""

import copy
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from hermes_guests.core import GuestsService
from hermes_guests.guest import NativeGuestRunner
from hermes_guests.model import GuestsError, iso
from hermes_guests.recovery import recover_local_authority
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState
from hermes_guests.transport import LocalBroker
from tests.tiers import slow


class RecoveryProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.effects = []
        self.deleted = []
        self.state = FileState(self.root / "control")
        self.service = GuestsService(
            self.state,
            "100",
            catalog={"light.porch": {"name": "Porch light"}},
            dispatch=lambda *a: self.effects.append(a),
            tool_resolver=GUEST_SCHEMAS.get,
            guest_data_retention_seconds=86400,
        )
        p = self.service.prepare(
            "100", "Alex", {"lights": ["light.porch"], "expires_at": iso(time.time() + 3600)}
        )
        inv = self.service.approve("100", p["proposal_id"])
        claim = self.service.claim(inv["token"], "200", "Alex")
        self.sid = self.service.accept_claim("100", claim["claim_id"])["share_id"]

    def runner(self):
        broker = LocalBroker(self.service)
        self.addCleanup(broker.close)

        def create(name):
            p = self.root / "profiles" / name
            p.mkdir(parents=True, exist_ok=True)
            return p

        def delete(name):
            self.deleted.append(name)
            shutil.rmtree(self.root / "profiles" / name)

        return NativeGuestRunner(
            self.service,
            broker,
            lambda: {},
            profile_factory=create,
            profile_resolver=lambda name: self.root / "profiles" / name,
            profile_deleter=delete,
        )

    def revoke(self):
        p = self.service.prepare("100", "Alex", None, share_id=self.sid, action="revoke")
        self.service.approve("100", p["proposal_id"])

    def test_new_native_profile_can_start_with_an_initialized_session_database(self):
        import sqlite3
        from unittest.mock import patch

        runner = self.runner()
        runner.profile_factory = None
        home = self.root / "fresh-profile"

        def create(name, **kwargs):
            home.mkdir()
            with sqlite3.connect(home / "state.db") as db:
                db.execute("CREATE TABLE sessions (id TEXT)")
            return home

        with (
            patch("hermes_cli.profiles.profile_exists", return_value=False),
            patch("hermes_cli.profiles.create_profile", side_effect=create),
        ):
            self.assertEqual(runner.ensure_profile(self.sid), home)

        self.assertEqual((home / ".hermes-guests-profile").read_text(), self.sid)
        self.assertTrue((home / "state.db").is_file())

    @slow
    def test_existing_unmarked_native_profile_with_session_database_is_still_refused(self):
        from unittest.mock import patch

        runner = self.runner()
        runner.profile_factory = None
        home = self.root / "existing-profile"
        home.mkdir()
        database = home / "state.db"
        database.touch()
        with (
            patch("hermes_cli.profiles.profile_exists", return_value=True),
            patch("hermes_cli.profiles.get_profile_dir", return_value=home),
            self.assertRaisesRegex(GuestsError, "existing private state cannot be adopted"),
        ):
            runner.ensure_profile(self.sid)

        self.assertTrue(database.exists())
        self.assertFalse((home / ".hermes-guests-profile").exists())

    @slow
    def test_guest_profile_gets_no_copy_of_the_plugin_when_share_is_installed_as_a_package(self):
        from unittest.mock import patch

        with patch("hermes_guests.guest.installed_as_package", return_value=True):
            home = self.runner().ensure_profile(self.sid)
            self.assertFalse((home / "plugins" / "hermes-guests").exists())
            # A link left by an earlier version is removed, since Hermes treats it as a
            # second installation and switches the plugin off for the guest.
            (home / "plugins" / "hermes-guests").symlink_to(self.root, target_is_directory=True)
            self.runner().ensure_profile(self.sid)
            self.assertFalse((home / "plugins" / "hermes-guests").is_symlink())

    def test_guest_profile_links_a_source_checkout_and_follows_it_when_it_moves(self):
        from unittest.mock import patch

        import hermes_guests.guest as guest

        project = Path(guest.__file__).resolve().parent.parent
        if not (project / "plugin.yaml").is_file():
            self.skipTest("Source-directory linking is verified in source installation tests")
        with patch("hermes_guests.guest.installed_as_package", return_value=False):
            home = self.runner().ensure_profile(self.sid)
            link = home / "plugins" / "hermes-guests"
            self.assertEqual(link.resolve(), project)
            link.unlink()
            link.symlink_to(self.root, target_is_directory=True)  # the code used to live elsewhere
            self.runner().ensure_profile(self.sid)
            self.assertEqual(link.resolve(), project)

    def test_cleanup_ends_access_first_keeps_audit_and_never_recreates_profiles(self):
        r = self.runner()
        home = r.ensure_profile(self.sid)
        evidence = self.service.audit.receipt_path
        self.revoke()
        self.assertEqual(r.cleanup("100"), [])
        self.service.clock = lambda: time.time() + 2 * 86400
        self.assertEqual(r.cleanup("100"), [self.sid])
        self.assertFalse(home.exists())
        self.assertTrue(evidence.exists())
        self.assertEqual(r.cleanup("100"), [])
        self.assertEqual(len(self.deleted), 1)
        with self.assertRaises(GuestsError):
            self.service.call("200", self.sid, "ha_get_state", {"entity_id": "light.porch"})

    def test_cleanup_wont_delete_active_busy_or_unowned_profile(self):
        r = self.runner()
        home = r.ensure_profile(self.sid)
        self.assertEqual(r.cleanup("100", grace_seconds=0), [])
        self.revoke()
        self.service.clock = lambda: time.time() + 2 * 86400
        r.busy.add((self.sid, None))
        self.assertEqual(r.cleanup("100"), [])
        r.busy.clear()
        (home / ".hermes-guests-profile").write_text("unrelated-profile")
        self.assertEqual(r.cleanup("100"), [])
        self.assertTrue(home.exists())

    @slow
    def test_native_guest_memory_is_signed_and_bound_to_identity_and_current_access(self):
        from hermes_guests.model import GUEST_MEMORY_CAPABILITY

        self.assertIn(
            GUEST_MEMORY_CAPABILITY,
            self.service._warrant(self.service.get_access("100", self.sid)).capabilities,
        )
        self.assertTrue(self.service.admit_memory("200", self.sid, "user")["allowed"])
        for actor, target, delegation in (
            ("300", "user", None),
            ("200", "owner", None),
            ("200", "user", "fake-child"),
        ):
            with self.assertRaises(GuestsError):
                self.service.admit_memory(actor, self.sid, target, delegation=delegation)
        with self.assertRaises(GuestsError):
            self.service.call("200", self.sid, GUEST_MEMORY_CAPABILITY, {"target": "user"})
        self.revoke()
        with self.assertRaises(GuestsError):
            self.service.admit_memory("200", self.sid, "memory")
        self.assertEqual(self.effects, [])

    def test_existing_signed_scope_is_not_silently_given_private_memory(self):
        from hermes_guests import core
        from hermes_guests.core import compile_scope, signed_permissions

        record = self.service.get_access("100", self.sid)
        legacy = dict(record["scope"])
        legacy.pop("guest_memory")
        restored = self.service.normalize_scope(legacy, saved=True)
        self.assertFalse(restored.get("guest_memory"))
        holder = core.load_key(self.service.root / "control" / (self.sid + ".key"), create=False)
        old = compile_scope(restored, holder, self.service.issuer, now=time.time())
        self.assertNotEqual(
            signed_permissions(old), signed_permissions(self.service._warrant(record))
        )
        legacy.pop("preset_schemas", None)
        self.assertTrue(self.service.normalize_scope(legacy).get("guest_memory"))

    def test_retention_is_configurable_confirmed_persistent_and_validated(self):
        r = self.runner()
        home = r.ensure_profile(self.sid)
        self.revoke()
        p = self.service.prepare("100", "Guest data", None, action="retention", retention_seconds=0)
        self.assertEqual(r.cleanup("100"), [])
        self.service.approve("100", p["proposal_id"])
        restored = GuestsService(self.state, "100", tool_resolver=GUEST_SCHEMAS.get)
        self.assertEqual(restored.data_settings("100")["retention_seconds"], 0)
        self.assertEqual(r.cleanup("100"), [self.sid])
        self.assertFalse(home.exists())
        for seconds in (-1, True, 366 * 86400, "7 days"):
            with self.assertRaises(GuestsError):
                self.service.prepare(
                    "100", "Guest data", None, action="retention", retention_seconds=seconds
                )
        with self.assertRaises(GuestsError):
            self.service.data_settings("200")

    def test_delete_revokes_first_waits_for_busy_workers_and_cannot_recreate_data(self):
        r = self.runner()
        home = r.ensure_profile(self.sid)
        memories = home / "memories"
        memories.mkdir()
        (memories / "USER.md").write_text("guest-private-preference")
        (home / "state.db").write_text("conversation")
        audit = self.service.audit.receipt_path
        r.busy.add((self.sid, None))
        p = self.service.prepare("100", "Alex", None, share_id=self.sid, action="delete")
        self.assertTrue(self.service._fresh(self.service.get_access("100", self.sid)))
        self.service.approve("100", p["proposal_id"])
        self.assertEqual(r.cleanup("100"), [])
        self.assertTrue(home.exists())
        for operation in (
            lambda: r.ensure_profile(self.sid),
            lambda: self.service.admit_memory("200", self.sid, "user"),
            lambda: self.service.prepare(
                "100", "Alex", {"chat_only": True, "duration_seconds": 600}, share_id=self.sid
            ),
        ):
            with self.assertRaises(GuestsError):
                operation()
        r.busy.clear()
        self.assertEqual(r.cleanup("100"), [self.sid])
        self.assertFalse(home.exists())
        self.assertTrue(audit.exists())
        self.assertIsNotNone(self.service.get_access("100", self.sid)["profile_deleted_at"])
        self.assertEqual(r.cleanup("100"), [])
        self.assertEqual(self.effects, [])

    def test_delete_of_an_unopened_profile_never_creates_it(self):
        r = self.runner()
        p = self.service.prepare("100", "Alex", None, share_id=self.sid, action="delete")
        self.service.approve("100", p["proposal_id"])
        r.cleanup("100")
        self.assertEqual(self.deleted, [])
        self.assertIsNotNone(self.service.get_access("100", self.sid)["profile_deleted_at"])

    def test_stale_retention_and_delete_reviews_cannot_overwrite_newer_choices(self):
        p = self.service.prepare("100", "Guest data", None, action="retention", retention_seconds=0)
        q = self.service.prepare(
            "100", "Guest data", None, action="retention", retention_seconds=604800
        )
        self.service.approve("100", q["proposal_id"])
        with self.assertRaises(GuestsError):
            self.service.approve("100", p["proposal_id"])
        deletion = self.service.prepare("100", "Alex", None, share_id=self.sid, action="delete")
        changed = self.service.prepare(
            "100", "Alex", {"chat_only": True, "duration_seconds": 600}, share_id=self.sid
        )
        self.service.approve("100", changed["proposal_id"])
        with self.assertRaises(GuestsError):
            self.service.approve("100", deletion["proposal_id"])
        self.assertTrue(self.service._fresh(self.service.get_access("100", self.sid)))

    def test_delete_revokes_descendants_and_retries_storage_failure_without_restoring_access(self):
        from unittest.mock import Mock

        r = self.runner()
        home = r.ensure_profile(self.sid)
        child = self.service.delegate(
            "200", self.sid, {"lights": ["light.porch"]}, task="Read the light"
        )
        p = self.service.prepare("100", "Alex", None, share_id=self.sid, action="delete")
        self.service.approve("100", p["proposal_id"])
        self.assertEqual(self.service.delegates, {})
        with self.assertRaises(GuestsError):
            self.service.call(
                "200",
                self.sid,
                "ha_get_state",
                {"entity_id": "light.porch"},
                delegation=child["task_id"],
            )
        deleter = r.profile_deleter
        r.profile_deleter = Mock(side_effect=OSError("storage unavailable"))
        with self.assertRaises(OSError):
            r.cleanup("100")
        self.assertTrue(home.exists())
        self.assertEqual(self.service.get_access("100", self.sid)["effective_status"], "revoked")
        r.profile_deleter = deleter
        self.assertEqual(r.cleanup("100"), [self.sid])
        self.assertFalse(home.exists())
        self.assertEqual(self.effects, [])

    def test_new_guest_memory_cannot_adopt_another_profiles_memory_or_follow_symlinks(self):
        r = self.runner()
        name = "guests-" + self.sid.replace("_", "-")
        home = self.root / "profiles" / name
        (home / "memories").mkdir(parents=True)
        foreign = self.root / "owner-memory"
        foreign.write_text("owner-secret")
        (home / "memories" / "MEMORY.md").symlink_to(foreign)
        with self.assertRaises(GuestsError):
            r.ensure_profile(self.sid)
        self.assertEqual(foreign.read_text(), "owner-secret")

    def test_denied_native_memory_handler_never_executes_a_write(self):
        import importlib.util

        if importlib.util.find_spec("run_agent") is None:
            self.skipTest("Native Hermes required")
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from hermes_guests import worker_state
        from hermes_guests.worker import register_worker

        broker = LocalBroker(self.service)
        self.addCleanup(broker.close)
        token = broker.open("200", self.sid)
        ctx = SimpleNamespace(register_tool=Mock(), register_hook=Mock())
        payload = {
            "tool_schemas": {},
            "delegation": False,
            "guest_memory": True,
            "broker_url": broker.url,
            "broker_token": token,
        }
        with (
            patch.object(worker_state, "payload", payload),
            patch("tools.memory_tool.memory_tool") as write,
        ):
            register_worker(ctx)
            remember = next(
                call.args[3]
                for call in ctx.register_tool.call_args_list
                if call.args[0] == "guest_memory"
            )
            self.revoke()
            result = remember(
                {
                    "target": "user",
                    "operations": [{"action": "add", "content": "private preference"}],
                }
            )
            import json

            self.assertFalse(json.loads(result)["success"])
            write.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_delete_also_removes_older_guest_profiles_and_preserves_other_people(self):
        r = self.runner()
        original = r.ensure_profile(self.sid)
        self.revoke()

        def invite(label, actor):
            p = self.service.prepare("100", label, {"chat_only": True, "duration_seconds": 600})
            i = self.service.approve("100", p["proposal_id"])
            c = self.service.claim(i["token"], actor, label)
            return self.service.accept_claim("100", c["claim_id"])["share_id"]

        latest = invite("Alex", "200")
        current_home = r.ensure_profile(latest)
        other = invite("Blair", "300")
        other_home = r.ensure_profile(other)
        p = self.service.prepare("100", "Alex", None, share_id=latest, action="delete")
        result = self.service.approve("100", p["proposal_id"])
        self.assertEqual(set(result["shares"]), {self.sid, latest})
        self.assertEqual(set(r.cleanup("100")), {self.sid, latest})
        self.assertFalse(original.exists())
        self.assertFalse(current_home.exists())
        self.assertTrue(other_home.exists())
        self.assertTrue(self.service._fresh(self.service.get_access("100", other)))

    def test_unlimited_retention_preserves_ended_profiles_until_explicit_deletion(self):
        self.service.default_retention_seconds = None
        r = self.runner()
        home = r.ensure_profile(self.sid)
        (home / "private-preference.txt").write_text("kept")
        self.revoke()
        self.service.clock = lambda: time.time() + 3650 * 86400
        self.assertEqual(r.cleanup("100"), [])
        self.assertTrue(home.exists())
        self.assertIsNone(self.service.data_settings("100")["retention_seconds"])
        self.service.clock = time.time
        p = self.service.prepare(
            "100", "Guest data", None, action="retention", retention_seconds=None
        )
        self.service.approve("100", p["proposal_id"])
        self.assertIsNone(
            GuestsService(self.state, "100").data_settings("100")["retention_seconds"]
        )

    def test_ended_invalid_or_revoked_warrants_cannot_start_a_model_or_runtime_lookup(self):
        from unittest.mock import Mock, patch

        r = self.runner()
        route = Mock()
        r.resolve_runtime = route
        with patch("hermes_guests.guest.subprocess.Popen") as process:
            with self.service.ledger.transaction() as db:
                self.service._revoke_warrant(db, db["shares"][self.sid]["warrant_id"])
            with self.assertRaises(GuestsError):
                r.run("200", self.sid, "Do a generic expensive task")
            with self.assertRaises(GuestsError):
                r.run("300", self.sid, "Try another person's identity")
            self.revoke()
            with self.assertRaises(GuestsError):
                r.run("200", self.sid, "Keep chatting after access ended")
            route.assert_not_called()
            process.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_reauthorization_preserves_personalization_and_deletion_allows_a_fresh_invitation(self):
        from hermes_guests.plugin import GuestsPlugin

        r = self.runner()
        home = r.ensure_profile(self.sid)
        private = home / "memories"
        private.mkdir()
        (private / "USER.md").write_text("Prefers brief answers")
        self.revoke()
        plugin = GuestsPlugin(object(), service=self.service, runner=r)
        self.assertEqual(plugin.existing_access("100", "Alex"), self.sid)
        p = self.service.prepare(
            "100", "Alex", {"chat_only": True, "duration_seconds": 600}, share_id=self.sid
        )
        self.service.approve("100", p["proposal_id"])
        self.assertEqual(r.ensure_profile(self.sid), home)
        self.assertEqual((private / "USER.md").read_text(), "Prefers brief answers")
        p = self.service.prepare("100", "Alex", None, share_id=self.sid, action="delete")
        self.service.approve("100", p["proposal_id"])
        r.cleanup("100")
        self.assertIsNone(plugin.existing_access("100", "Alex"))
        p = self.service.prepare("100", "Alex", {"chat_only": True, "duration_seconds": 600})
        invitation = self.service.approve("100", p["proposal_id"])
        claim = self.service.claim(invitation["token"], "200", "Alex")
        fresh = self.service.accept_claim("100", claim["claim_id"])["share_id"]
        self.assertNotEqual(fresh, self.sid)
        self.assertFalse((r.ensure_profile(fresh) / "memories" / "USER.md").exists())
        self.assertFalse(home.exists())

    def test_standing_renewal_refuses_an_invalid_signature(self):
        from unittest.mock import Mock, patch

        p = self.service.prepare(
            "100", "Alex", {"chat_only": True, "until_ended": True}, share_id=self.sid
        )
        self.service.approve("100", p["proposal_id"])
        before = self.service.get_access("100", self.sid)["warrant_id"]
        self.service.clock = lambda: time.time() + 31 * 86400
        with patch.object(
            self.service, "_warrant", return_value=Mock(verify=Mock(return_value=False))
        ):
            with self.service.ledger.transaction() as db:
                self.assertFalse(self.service._keep_standing(db, db["shares"][self.sid]))
        self.assertEqual(self.service.get_access("100", self.sid)["warrant_id"], before)

    @slow
    def test_expired_signed_grant_is_rejected_before_model_lookup_even_with_active_local_state(
        self,
    ):
        import hashlib
        from unittest.mock import Mock, patch

        from tenuo import Warrant

        from hermes_guests.core import load_key
        from hermes_guests.store import atomic_private

        holder = load_key(self.service.root / "control" / (self.sid + ".key"), create=False)
        grant = (
            Warrant.mint_builder()
            .holder(holder.public_key)
            .ttl(1)
            .capability("ha_get_state")
            .mint(self.service.issuer)
        )
        wire = grant.to_bytes()
        atomic_private(self.service.root / "warrants" / (str(grant.id) + ".warrant"), wire)
        with self.service.ledger.transaction() as db:
            db["shares"][self.sid].update(
                warrant_id=str(grant.id), artifact_hash=hashlib.sha256(wire).hexdigest()
            )
        time.sleep(1.5)
        self.assertTrue(self.service._fresh(self.service.get_access("100", self.sid)))
        r = self.runner()
        r.resolve_runtime = Mock()
        with patch("hermes_guests.guest.subprocess.Popen") as process:
            with self.assertRaises(GuestsError):
                r.run("200", self.sid, "An expensive generic task")
            r.resolve_runtime.assert_not_called()
            process.assert_not_called()

    def test_signed_revocation_survives_restart_and_rollback_is_rejected(self):
        raw = (self.service.root / "revocations.srl").read_bytes()
        self.revoke()
        restored = GuestsService(
            self.state, "100", catalog=self.service.catalog, tool_resolver=GUEST_SCHEMAS.get
        )
        with self.assertRaises(GuestsError):
            restored.call("200", self.sid, "ha_get_state", {"entity_id": "light.porch"})
        (self.service.root / "revocations.srl").write_bytes(raw)
        with self.assertRaises(GuestsError):
            GuestsService(self.state, "100", tool_resolver=GUEST_SCHEMAS.get)

    def test_missing_keys_do_not_silently_replace_trust_and_recovery_ends_all_access(self):
        old_issuer = self.service.issuer.public_key.to_bytes()
        evidence = self.service.audit.receipt_path
        (self.service.root / "control" / "issuer.key").unlink()
        with self.assertRaises(GuestsError):
            GuestsService(self.state, "100", tool_resolver=GUEST_SCHEMAS.get)
        result = recover_local_authority(self.state, "100")
        self.assertEqual(result["ended"], 1)
        restored = GuestsService(self.state, "100", tool_resolver=GUEST_SCHEMAS.get)
        self.assertNotEqual(restored.issuer.public_key.to_bytes(), old_issuer)
        self.assertEqual(restored.get_access("100", self.sid)["effective_status"], "revoked")
        self.assertTrue(evidence.exists())
        self.assertFalse(restored.owner_ready())
        with self.assertRaises(GuestsError):
            restored.call("200", self.sid, "ha_get_state", {"entity_id": "light.porch"})

    def test_live_repair_restores_missing_key_files_without_changing_trust(self):
        issuer = self.service.issuer.public_key.to_bytes()
        audit = self.service.audit_key.public_key.to_bytes()
        before = self.service.get_access("100", self.sid)["scope"]
        (self.service.root / "control" / "issuer.key").unlink()
        (self.service.root / "control" / "audit.key").unlink()
        result = self.service.repair("100")
        self.assertIn(self.sid, result["restored"])
        restored = GuestsService(
            self.state, "100", catalog=self.service.catalog, tool_resolver=GUEST_SCHEMAS.get
        )
        self.assertEqual(restored.issuer.public_key.to_bytes(), issuer)
        self.assertEqual(restored.audit_key.public_key.to_bytes(), audit)
        self.assertEqual(restored.get_access("100", self.sid)["scope"], before)

    def test_recovery_refuses_a_different_owner(self):
        before = copy.deepcopy(self.service.ledger.snapshot())
        with self.assertRaises(GuestsError):
            recover_local_authority(self.state, "300")
        self.assertEqual(before, self.service.ledger.snapshot())

    def test_corrupt_state_has_no_permissive_reset(self):
        self.state.path.write_text("not-json")
        with self.assertRaises(GuestsError):
            GuestsService(self.state, "100", tool_resolver=GUEST_SCHEMAS.get)
        with self.assertRaises(GuestsError):
            recover_local_authority(self.state, "100")
        self.assertEqual(self.state.path.read_text(), "not-json")

    def test_native_profile_creation_and_cleanup_use_native_apis_in_an_isolated_home(self):
        import importlib.util

        if importlib.util.find_spec("hermes_cli") is None:
            self.skipTest("Native Hermes source required")
        script = r"""
import json,os,time
from pathlib import Path
from hermes_guests.core import GuestsService
from hermes_guests.schemas import GUEST_SCHEMAS
from hermes_guests.store import FileState
from hermes_guests.model import iso
from hermes_guests.transport import LocalBroker
from hermes_guests.guest import NativeGuestRunner
root=Path(os.environ['HERMES_HOME']);root.mkdir(exist_ok=True)
(root/'config.yaml').write_text('model:\n  default: fixture\n')
(root/'.env').write_text('OWNER_PRIVATE_SENTINEL=do-not-copy\n')
s=GuestsService(FileState(root/'control'),'100',catalog={'light.porch':{'name':'Porch'}},tool_resolver=GUEST_SCHEMAS.get)
p=s.prepare('100','Alex',{'lights':['light.porch'],'expires_at':iso(time.time()+3600)})
i=s.approve('100',p['proposal_id']);c=s.claim(i['token'],'200','Alex');sid=s.accept_claim('100',c['claim_id'])['share_id']
b=LocalBroker(s)
try:
 r=NativeGuestRunner(s,b,lambda:{})
 home=r.ensure_profile(sid)
 assert 'OWNER_PRIVATE_SENTINEL' not in (home/'.env').read_text()
 assert not (home/'memories'/'USER.md').exists()
 assert (home/'gateway.parked').exists()
 p=s.prepare('100','Alex',None,share_id=sid,action='revoke');s.approve('100',p['proposal_id'])
 s.clock=lambda:time.time()+2*86400
 assert r.cleanup('100', grace_seconds=86400)==[sid]
 assert not (home/'config.yaml').exists()
 print('NATIVE_PROFILE_OK')
finally:b.close()
"""
        env = {k: v for k, v in os.environ.items() if k in {"PATH", "PYTHONPATH", "LANG"}}
        env.update(HOME=str(self.root / "host"), HERMES_HOME=str(self.root / "native-home"))
        result = subprocess.run(
            [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertIn("NATIVE_PROFILE_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
