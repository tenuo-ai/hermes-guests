# Implementation map

- `plugin.py`: native tools, Telegram handlers, owner/guest UX, supervision.
- `compat.py`: shared native platform-tool selection seam.
- `permissions.py`: schema validation, input rules, narrowing, proxy schemas.
- `model.py`: expiry/resource normalization and complete English reviews.
- `core.py`: invitations, owner confirmation, grants, signed revocation, dispatch.
- `store.py`: native state transactions and protected atomic artifacts.
- `audit.py`: signed authorization receipts and correlated execution outcomes.
- `guest.py` / `worker.py`: isolated native profiles and native agent processes.
- `transport.py`: loopback per-turn authenticated service/delegation broker.
- `recovery.py`: explicit owner-confirmed local identity recovery.
- `ui.py`: ordered, Unicode-safe complete review pagination.

Home Assistant and research presets demonstrate the workflow. Installed structured
tools use the generic path without a dedicated adapter. The bundled skill helps
the model discover and draft; trusted code owns validation, review, issuance,
identity binding, scope changes, and enforcement.

Run `python scripts/check.py` with native Hermes and the test extras installed to
execute the pre-human-test gate. Tests use recording services and a recording bot;
passing them is not a claim of live Telegram/device acceptance. See testing.md.
