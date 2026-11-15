# Authorization and state

Hermes supplies Telegram sender identity, native profiles, conversations, and
profile-scoped plugin state. Hermes Guests stores its access ledger in `ctx.state` and
protected key, warrant, revocation, and receipt artifacts in its plugin data area.
The model cannot read signing keys or approve its own proposals.

## Trust model: built for ease of use

Hermes Guests drafts each permission, signs it with keys kept on the same machine
as Hermes, and asks you to approve it with a button. The model cannot read the keys
or approve its own proposals, and every guest action is checked against the signed
permission. But the machine running Hermes is also the one that issues
permissions, so anyone who takes control of that machine could issue new ones.
This is a deliberate tradeoff that keeps setup to a single button. If you need
permissions set and signed outside Hermes, for example by your organization, this
release does not support that yet.

## Confirmation

The owner asks naturally. The agent discovers actual enabled tool schemas and
drafts a bounded scope with an expiry. Trusted code normalizes it, renders the
complete English review, and binds its immutable digest and revision to a native
confirmation. The callback must come from the owner in their private chat and
from the exact bot message containing that review. Buttons appear only after
all review pages have been delivered. A copied or stale button is insufficient.

An invitation conveys no service access. The owner also confirms the actual
Telegram account that claims it. An accepted grant binds that stable sender ID,
not a display name or username. The ID is signed into the grant itself, so a saved
record that names another account is refused and repair pauses it. Grants issued
before this was added are bound by their record alone until they are next changed.
Guests are never added to broad native pairing.
Every root-scope change receives a complete new review. Duplicate callbacks do
not create additional authority; obsolete applied reviews cannot restore access,
and an invitation that was accepted can never be replaced, also after that guest's
data is deleted. A review that switches ended or lapsed access back on states which
Telegram account it is for, since no invitation or account confirmation follows.
If access expires while a change review is open, approval requires a fresh review
with that account notice before access can be restored.

## Execution

Hermes Guests issues a warrant for the owner's reviewed tools at setup and
renews it when possible. By default its owner check passes control back to
Hermes if the warrant cannot authorize a call. Configure
[owner enforcement](setup-recovery.md#owner-warrants) explicitly to block on
those failures. Management and approval always require the authenticated owner.

Guests run native Hermes agents in separate processes and profiles. Their service tools are scoped proxies and the bounded delegation tool. A guest
with the signed private-memory entitlement also has a profile-local `guest_memory`
tool, implemented with native Hermes MemoryStore. It first asks the authenticated
broker to verify identity, active access, the signed target constraint and revocation
through HermesGuard and the SDK authorizer. It then writes only the worker
profile store. No model-chosen paths or controller memory calls are accepted.
Audit records contain the target, never preference text. Subtasks have no durable
memory access. Existing warrants gain this entitlement only through a fresh owner review. No owner
memory, history, skills, credentials, terminal, filesystem, browser, host task
launcher, or configuration tools are exposed. This is process/profile separation,
not an OS security sandbox for arbitrary untrusted code.

The broker binds a per-turn token to the authenticated guest and share; the model
cannot choose another identity or holder. Before admitting each service request,
Hermes Guests checks current local lifecycle state, scope/schema bindings, the full signed
chain, proof of possession, and current signed revocation data through the released
SDK and exported HermesGuard. It persists authorization evidence before dispatch.

Before any model invocation, Hermes Guests verifies active authenticated access, issuer trust,
expiry, current revocation and holder binding through the SDK. This includes
generic conversation turns and legacy service grants. Retaining data never
authorizes model use. Ending access prevents subsequent model starts.

Admission is atomic with lifecycle changes, but slow service I/O does not hold the
lifecycle lock. Revocation blocks subsequent admissions while previously admitted
requests may finish. It cannot undo an already started external effect. Pending
native model workers are stopped on access changes and cannot deliver stale
responses after revocation or a scope revision.

Schema changes block affected calls and require a fresh review. Input constraints
do not establish every hidden effect or returned-data boundary of a plugin. The
review states this limit for inferred generic permissions. Unrepresentable rules
are rejected before confirmation rather than approximated silently.

## Never-share list

The owner's never-share list names tools, and words in tool inputs, that no guest
may use. It is checked when access is prepared, so a review leaves blocked items
out and says so, and again before each guest call, so it also overrides access
granted earlier. Only the owner changes it: with buttons in `/guests_setup`, or by
confirming a change that the `guests_never` tool proposes. The model can propose a
change but cannot apply one; the change is applied only by the owner's
authenticated press of Confirm on a card that has not expired. A guest's own
request never changes the list.

## Delegation

Within approved guest access, the agent selects narrower child permissions without
another user prompt. Trusted code checks the subset and uses SDK attenuation,
a fresh child holder, and explicit parent-chain attachment. Children have one hop,
a maximum five-minute lifetime bounded by their parent, and four active tasks per
share. Finished tasks detach their local bindings. Root replacement/revocation
invalidates descendants and stops tracked workers.

Calls carry the chain and proof as metadata outside ordinary model arguments.
A remote authorizer must verify that evidence, its schema/operation contract, and
current revocation information to enforce independently. Unaware services do not
acquire enforcement just by receiving metadata. The guaranteed boundary is the local Hermes Guests service dispatcher.

## Audit, recovery, and cleanup

Authorization decisions have SDK-signed local receipts. Activity links input
summaries, decisions, adapter blocks, and outcomes without treating authorization
as proof of successful execution. The activity tool can select one guest, the
owner or everyone. Owner and all-user views require the authenticated owner.
Owner tool inputs are omitted. A request outside the owner warrant in the
default mode can be recorded as a request without an authorization verdict. Readable event timestamps include the
requested timezone, date and seconds. A timestamped card is sent directly to the
owner so its presentation is independent of model summarization. The view covers
recent retained records, with authorization decisions, owner tool requests, and sharing changes.
Conversation history and service completion have separate evidence. Admission failures without usable authority are
recorded separately. Receipts use a new signed segment per process start; local
files are not deletion-proof or a global cross-restart completeness guarantee.

Repair verifies saved unexpired authority, pauses corrupt grants, refreshes
service discovery, and clears stale bindings. It cannot widen or renew authority.
A complete owner-approved replacement can repair a missing guest holder. Explicit
owner-confirmed identity recovery ends old local grants/invitations and retains
historical audit/artifacts before new setup. Unreadable state is not silently reset.

A supervised maintenance task removes ended, idle, owned guest profiles after an
owner-configured retention period. Data is kept until explicit deletion by default. An owner-confirmed immediate
delete revokes all of that guest’s access records and descendants first, then removes
owned, idle guest profiles. A persisted deletion marker blocks old reviews and
profile recreation. Busy or unsafe profiles remain pending for maintenance retry. It never recreates a profile for deletion or adopts unrelated
state. Expiry/revocation enforcement does not wait for profile cleanup. Audit
evidence remains separate from conversation profiles.

## Chat-only access

An owner may approve `chat_only` with an end time. The signed warrant contains a
reserved internal entitlement so it follows the same identity, expiry and signed
revocation lifecycle. The dispatcher rejects that entitlement explicitly. Guest
agents receive zero service proxies and zero delegation tools. Every attempted
service call is denied before dispatch. Existing saved service grants retain their
original representation.

Each chat-only turn verifies the signed entitlement and current revocation data
before model admission. The internal entitlement never reaches a service handler.
