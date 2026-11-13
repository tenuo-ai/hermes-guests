"""Explicit owner-confirmed local authority recovery, never silent key replacement."""

from __future__ import annotations

import time
import uuid

from .model import GuestsError
from .store import Ledger, private_dir


def recover_local_authority(state, owner):
    ledger = Ledger(state)
    with ledger.transaction() as db:
        if db["setup"].get("owner_id") not in {None, str(owner)}:
            raise GuestsError(
                "The saved owner differs. Restore the matching sharing backup before recovery."
            )
        archive = private_dir(ledger.root / "recovery" / uuid.uuid4().hex)
        # Keep old artifacts for inspection; no historical audit evidence is removed.
        for name in ("control", "warrants", "revocations.srl"):
            path = ledger.root / name
            if path.exists():
                path.rename(archive / name)
        for r in db["shares"].values():
            r.update(status="revoked", revoked_at=time.time(), revision=r["revision"] + 1)
        db["proposals"] = {}
        db["invites"] = {}
        catalog = db["setup"].get("catalog", {})
        db["setup"] = {
            "owner_id": str(owner),
            "catalog": catalog,
            "trust_generation": db["setup"].get("trust_generation", 0) + 1,
            # The owner's never-share list and their choice of how long guest data is kept
            # are theirs, not part of the identity being reset.
            **(
                {"never_share": db["setup"]["never_share"]}
                if db["setup"].get("never_share")
                else {}
            ),
            **{
                key: db["setup"][key]
                for key in ("guest_data_retention_seconds", "data_policy_revision")
                if key in db["setup"]
            },
        }
    return {"status": "reset", "ended": len(db["shares"])}
