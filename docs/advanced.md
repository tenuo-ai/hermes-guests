# How security works

Hermes Guests handles the invitation, readable review and isolated guest experience.
[Hermes-Tenuo](https://github.com/tenuo-ai/hermes-tenuo) and
[Tenuo](https://github.com/tenuo-ai/tenuo) provide the signed authorization mechanism.

## Local authority

Trusted plugin code creates local issuer, audit and holder keys in protected plugin
storage within the selected Hermes profile. Key files use private permissions.
The model proposes a scope from enabled tool schemas. The authenticated owner
confirms the complete reviewed scope before trusted code issues authority.

Guests prove their Telegram account through a claim followed by owner confirmation.
A forwarded invitation alone grants no access. Guest workers receive scoped service
proxies through a per-turn authenticated broker. The broker verifies signatures,
holder proof, scope, expiry and current signed revocation data before dispatch.

A chat-only grant uses an internal conversation entitlement in the same signed
lifecycle. Its worker has zero shared-service or delegation tools. Separately authorized
private guest memory can still save preferences. The dispatcher rejects attempts
to execute the conversation entitlement itself.

## Model connection

For a ChatGPT or Codex sign-in, the worker sends Responses requests to a
loopback endpoint in Hermes Guests' local broker. The broker checks current signed
access and the turn's approved model on every request. It forwards requests
to the fixed Codex endpoint using Hermes's credentialed provider API. Hermes
attaches and refreshes the subscription credential. The worker receives a
local turn token and the broker returns a buffered Responses stream.

API-key routes read the key through Hermes's active profile secret scope and
supply it to the trusted worker through a private pipe. Guest tools cannot
access that payload, and guest profiles do not store it. A keyless local
endpoint uses the same native model worker. See [model setup](setup-recovery.md#model-setup).

## Changes and revocation

Owner-approved replacement issues new authority and revokes the old warrant.
Subtasks receive narrower signed chains within the parent's end time. Revocation
blocks new admissions. An operation admitted earlier may finish. Scope changes
invalidate stale worker authority and child bindings. Some changes start fresh
model context within the same Telegram chat.

## Activity and service behavior

Signed local receipts record authorization decisions. Service outcomes and chat
refusals are separate observations. A tool returning successfully may require an
additional service check to verify its effect. Local receipt files can be deleted
by a privileged operator. They provide local evidence with that trust boundary.

Tool schemas describe inputs. Their constraints cannot establish every hidden
service effect or returned-data boundary. Review those limits before sharing.
Guest process/profile isolation does not sandbox arbitrary hostile code. Native
external agent runtimes require additional enforcement and are currently rejected.

For implementation and recovery details, see [authorization](authorization.md),
[structured permissions](integrations.md), and [setup and recovery](setup-recovery.md).
