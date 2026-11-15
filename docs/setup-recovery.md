# Setup and recovery

Start with [Get started](../README.md#get-started). This reference covers
installation issues, identity, configuration and recovery.

## Installation help

Install Hermes Guests from the Python environment used by your Hermes gateway. With
multiple profiles, enable it for the profile connected to your Telegram bot and
follow Hermes’s reload prompt or restart the gateway if you manage it separately.
Use one bot token per gateway. A separate gateway
for a named profile may need `gateway.standalone: true` in that profile’s config.

Hermes Guests pins the released `hermes-tenuo==0.4.0` Python dependency and uses its
exported guard. Hermes’s package manager can refuse installation when both plugins
are selected, because it omits the workspace source for the dependency between
them. A tested fix is awaiting upstream review. Until your Hermes includes it,
install Hermes Guests by itself in the environment used for sharing.
[Package-manager fix](https://github.com/NousResearch/hermes-agent/pull/153953)

If a source scan asks for review, inspect the reported files before confirming.
Hermes Guests launches isolated guest workers; its tests contain Unicode permission labels.

After updating Hermes Guests, send `/new` in your Telegram chat before your next
sharing request. Hermes keeps an open chat's tool descriptions until a new session
starts, so an old chat can describe the previous version. Hermes Guests reminds you
once when its tools have changed.

## Model setup

Use `hermes model` in the owner profile to select the model for guest turns.
Hermes Guests supports the ChatGPT/Codex subscription on the standard endpoint through
the Git-installed plugin. It supports API keys for OpenAI, OpenRouter, Anthropic,
Google Gemini, DeepSeek and xAI. Configure the key in the same Hermes profile.

For Ollama, LM Studio or another compatible server, use a local or named custom
provider and select a model with tool support if guests will use services.
Custom endpoints support Chat Completions, Anthropic Messages and Responses.
Configure an authenticated custom endpoint with `key_env` and HTTPS, or use
loopback HTTP. Hermes Guests can try a configured supported fallback when the primary
route cannot be selected. It keeps the selected model for the duration of a turn.

Claude subscription sign-in, Nous Portal and other subscription routes without
a Hermes credentialed request API are unavailable for guests. If the owner
uses one of these routes, configure a supported fallback for sharing. The Codex
app-server runtime also requires a supported native API fallback.

## First use

Install and enable Hermes Guests in the same environment as Hermes, connect the owner's
model and service using Hermes's normal setup, and follow the native reload
prompt. Keep the bot's native allowlist or pairing limited to its single owner.
Hermes Guests reuses the sole admitted numeric Telegram identity. When multiple people
are already allowlisted or paired, set `owner_id` in
`plugins.entries.hermes-guests.settings` to an existing admitted user before setup.
The allowlist order carries no ownership authority. A display
name and the first person to message the bot cannot establish ownership.

Send a message in the owner's private Telegram chat. Hermes answers normally and
shows **Set up guest sharing** once. You can keep chatting and set it up later.
Ask to invite someone or send `/guests_setup` to open setup.
Choose **Enable sharing** after reviewing the setup. Hermes Guests creates local
keys and adds any missing sharing tools to the bot's current tool selection.
Use `/guests_setup` to change the never-share list, guest data retention, or
whether access cards open in the simple or detailed view.

Then ask who may use which resources and for how long. Hermes Guests discovers actual
tool schemas and resources, validates a bounded proposal, and shows its complete
English permissions and exact expiry with **Approve / Change / Cancel**. A relative
duration is resolved before the review; delayed acceptance cannot extend it.
Approval produces an invitation. The guest must open it and the owner must confirm
the actual Telegram account before service access exists. Setup alone never
creates a guest grant.

Connecting an unavailable service still uses that service's normal login/setup.
Hermes Guests cannot reconstruct missing service credentials or make an unsupported
model runtime safe by bypassing its restrictions.

## Identity, state, and keys

Hermes supplies Telegram sender identity, session context, and profile-scoped
`ctx.state`. Hermes Guests uses that native store for access records, immutable proposals,
invitation status, revisions, and revocation checkpoints. Protected artifacts in
the plugin's data directory hold signing keys, warrants, signed revocation data,
and local audit segments. Private key material is not part of the JSON ledger or
model context. Treat the plugin state and protected artifacts as sensitive;
ordinary operation does not require editing either.

Guests run in separate native profiles/processes with only scoped proxy tools.
Owner memories, conversation history, and unrestricted tools are not copied.
Model access is supplied to workers at runtime rather than persisted as provider
credentials in guest profiles. This is process/profile separation, not an OS
sandbox. Remote services need their own authorizer for independent enforcement.

## Ordinary recovery

| Situation | What to do | What stays unchanged |
| --- | --- | --- |
| Lost, expired, or declined invitation | Owner sends `/guests` and requests a replacement link | Approved capabilities and exact end time |
| Wrong account opens the invitation | Owner declines it and replaces the invitation | No access for the wrong account |
| Gateway reconnects or a review is interrupted | Owner sends `/guests` to restore pending cards | Existing scope, account binding, expiry, and revocation |
| Guest wants a fresh conversation | Guest sends `/new` | Scope and expiry |
| Guest access expires | Guest uses `/renew` or Request renewal; owner reviews and accepts | Access stays expired until approval |
| Access is revoked | Owner creates a fresh invitation if sharing should resume | Revoked access stays ended |
| Saved access or service schema needs checking | Owner presses **Check sharing** in `/guests`, or asks Hermes to use `guests_repair` | No automatic scope or expiry expansion |

Repair verifies active artifacts, holder keys, current schemas and revocation
state, clears cached bindings for checked records, and pauses records it cannot
verify. The next valid call reattaches eligible authority. The Telegram repair
command also runs cleanup of ended profiles. Inspection is available through
`/guests`, guest `/access`, and owner activity queries.

Missing issuer/audit key files can be restored only while the matching trusted
key is still alive in the running process. A conflicting or corrupt key is not
silently replaced. A missing guest holder can be replaced through a new
owner-confirmed access review. Losing the issuer across a restart requires the
explicit recovery flow below, rather than a claim that the old key was recreated.

## Lost sharing identity

Use `/guests_recover` only when matching saved authority cannot be restored. The
owner sees a full warning and must choose **End access and reset**. Recovery ends
local guest access and invitations, archives protected artifacts, retains audit
evidence, and requires `/guests_setup` followed by fresh invitations. External
authorizers must separately stop trusting the previous identity.

Do not use identity reset as an ordinary repair or test cleanup shortcut. Restore
a matching backup where appropriate; never overwrite a valid identity, extend an
expiry, resurrect revoked access, or disable enforcement to hide a failure.

## Cleanup and acceptance

Expiry and revocation block new model starts and service calls independently of
profile cleanup. Guest conversations and memories are retained until explicit
deletion by default. An owner can opt into automatic deletion after a configured
period, or confirm End and delete data for a guest. Reauthorization can
reuse retained personalization. A new approved invitation after deletion starts
with a fresh profile. Unrelated profiles and security/audit evidence remain.
Actions already in progress may finish. Later calls and stale replies are stopped.

[The sharing scenarios](scenarios.md) cover setup, changes, renewal, restarts,
repair, and cleanup. Test identity recovery separately with disposable authority.
Use [development testing](testing.md) for fixture and model checks. Record
actual Telegram delivery and connected-service results separately.

## Settings

Set these under `plugins.entries.hermes-guests.settings` in the profile configuration.

| Setting | Default | Allowed | What it does |
| --- | --- | --- | --- |
| `review_seconds` | 600 | 60–86400 | How long an access review can be approved. |
| `invitation_seconds` | 86400 | 60–2592000 | How long an invitation link works when access is a length of time. With a fixed end, the link works until access ends. |
| `claim_seconds` | 86400 | 60–2592000 | How long you have to confirm the account that opened an invitation. |
| `guest_names` | `yours` | `yours`, `telegram` | Keep the name you gave a guest, or switch to their Telegram name once they join. |
| `activity_retention_days` | 90 | 1–3650 | How long readable activity is kept. |
| `require_owner_warrant` | `false` | `true`, `false` | Fall back to Hermes, or block, when your own tool call isn't covered by your warrant. See [below](#using-the-hermes-tenuo-plugin-alongside-hermes-guests). |

A length of access ("15 minutes", "a week") starts when you confirm the guest's
account, so a late invitation costs nothing. A fixed end ("until Sunday 9 PM") never
moves, and invitation and confirmation deadlines never pass it.

The agent can use `guests_status` to distinguish pending or expired reviews from
invitations awaiting a claim. A pending invitation does not appear as active guest
access until the actual Telegram account is confirmed. An expired review needs a new
review. Cards and invitation messages show deadlines in the reader's time zone.

## Choosing time to accept in chat

You can say “Give Alex two days of access and a day to accept,” or “until Sunday 9 PM,
and keep the invite open all weekend.” The review shows how long the invitation
works, and the invitation message shows its exact deadline. A replacement link reuses
that window, capped by a fixed end time. Changes to an existing guest's access need
no new invitation.

`guests_prepare_access.invitation_seconds` accepts 60 through 2592000 seconds (30
days). Without it, the profile's `invitation_seconds` default applies.

## Guest data

Each guest has a separate Hermes profile with conversations and private memory.
New permission reviews include private memory. To enable it for an existing guest,
review and approve an updated grant with the desired permissions and end time.
The guest can ask Hermes to remember a preference, then use `/new` for a fresh
conversation that retains authorized saved preferences.

Ending access prevents further model starts and tool calls while preserving data
for a later approved return. Ask “End Alex’s access and delete their data” to
review immediate removal. Deletion ends that guest’s grants and subtasks before
removing their profiles, conversations and memory. Busy workers or storage failures
leave deletion pending for maintenance to retry. Later invitations can start fresh.
Deletion also removes the guest’s own request text and Telegram name from saved
state, including copies in invitations and reactivation reviews; their numeric ID
and the name you gave them stay with the audit records. Pending reactivation reviews
are invalidated when their personal details are removed.

Choose how long guest data is kept after access ends in `/guests_setup` → Guest
data, or ask “Keep guest data for seven days after access ends”. The default is
unlimited, and each new guest's review states the current setting. Shortening retention applies to already-ended
profiles. Security records, operator backups and delivered Telegram messages have
separate retention. Previously admitted service operations may still finish.

Operators can set `guest_data_retention_seconds` under
`plugins.entries.hermes-guests.settings`. Use `null` for unlimited, or an integer
from zero through 31536000 seconds. An owner-confirmed setting in chat takes
precedence and survives restarts.

## Guest network requests

Before admitting a guest request, Hermes Guests resolves web addresses in its inputs and
requires every answer to be public. An exception requires that exact address in
the matching reviewed permission rule. Unresolvable names are refused too,
including when an external proxy would normally resolve them.

This checks the address written in the request. Redirects and subsequent DNS
answers need checks inside the fetching tool. Hermes’s native web tools provide
those checks unless configured to allow private addresses. Other plugins define
their own fetching behavior. Share unfamiliar fetching tools with explicit
addresses and review their network behavior before granting broader access.

## Using the Hermes-Tenuo plugin alongside Hermes Guests

Both plugins can load in the same profile after the package-manager issue above
is resolved. Hermes Guests enforces each guest’s signed access through its own guard.
The separately enabled Hermes-Tenuo plugin can additionally enforce your native
owner-agent calls.

An unconfigured Hermes-Tenuo plugin passes ordinary calls through by default.
Setting `require_warrant: true` under `plugins.entries.hermes-tenuo` opts into
blocking calls when no warrant is configured. A configured but unloadable warrant
also blocks calls. When configuring that additional enforcement, include the
Hermes Guests management operations you want the owner agent to use in its warrant.
Enabling guest sharing leaves this setting to you. A Hermes Guests approval
cannot override a denial from Hermes-Tenuo. Every guest still needs a valid
signed warrant, including guests with chat-only access.

The [Hermes-Tenuo README](https://github.com/tenuo-ai/hermes-tenuo#readme) covers
its configuration and task-warrant behavior. These settings govern that plugin;
Hermes Guests' guest authorization remains active independently.

### Owner warrants

Setup issues an owner warrant covering the current Telegram tools. By default,
a missing, expired, or narrower warrant leaves the owner's tool decision to
Hermes and its other plugins. To require this local warrant for owner Telegram
tool calls, set `plugins.entries.hermes-guests.settings.require_owner_warrant`
to `true`. If your tools change, run `/guests_setup` to review them again.
For a separately managed owner policy, see [Hermes-Tenuo](https://github.com/tenuo-ai/hermes-tenuo#readme).
