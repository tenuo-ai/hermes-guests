# Setup and repair behavior

Check which trusted setup/repair tools are actually installed. First-use setup is
provided by the native Telegram `/guests_setup` flow. Use `guests_repair` to verify
saved grants and refresh discovery. If these are absent, report sharing unavailable
rather than pretending to fix it.

The native first-run flow creates protected identities and native plugin state
automatically, checks enforcement/context isolation, and explains readiness.
Use trusted existing Hermes sender identity; never assign ownership to the first
unknown sender. Missing service login uses that service's native setup flow.

For an actual installed repair tool, diagnose before retrying. Safe restoration
of the same approved unexpired scope may proceed without repeating approval.
Preserve operation ID, recipient, issuer identity, exact expiry, and revocation
status. Never replace keys, extend time, widen tools, or disable enforcement to
make the error disappear.

Explain scope/trust/identity changes and prepare a new native confirmation.
Lost issuer material is a rotation/recovery decision, not a reconstructable key.
Keep technical detail optional. Tell the user what failed, what can be restored,
and whether a decision is needed.
