# How sharing works

## First use

Install/enable through the Hermes environment. The existing owner opens the private
bot chat. Hermes Guests checks its identity, native API/model readiness, and enabled tools;
the owner reviews and enables sharing. The setup card says what sharing lets the
owner do and that hermes-tenuo sets up sensible default Tenuo warrants; **Details**
lists the owner's tools and the warrant behind them. Local keys and state are automatic. Setup
then offers the never-share list and how long guest data is kept after access
ends; both are optional and can be changed later in `/guests_setup`.
Broad native allow-all/pairing must not coexist with scoped guest access.

Ordinary greetings stay brief and carry no sharing controls. Sharing begins when
the owner asks for it or opens its command.

## Share a service

Owner: “Let Alex use the guest-room lights until Sunday.” Or: “Let Alex request
movies through my media plugin, but keep the rest private.” Hermes inspects the
actual tools/resources and drafts enforceable permissions. If a restriction is
not representable, it explains that specific limit before proposing broader access.

The bot presents the entire resulting scope and expiry, with Approve / Change /
Cancel. Long reviews are ordered numbered pages with buttons only at the end.
Change discards that draft and invites a correction; it does not apply a partial
scope. Approve creates an invitation link the owner sends to the guest.

Finite described resources group their allowed operations in the review. A
read-only resource explicitly lists reading. Other input restrictions stay with
the operation they constrain. A per-invitation acceptance window is visible and
can be requested conversationally.

The owner can also add a guest without the model: **Add a guest** in `/guests`, or
`/guests_new <name>`, chooses chat only or tools, then how long, then shows the
same review. Each new guest's review states how long their data is kept.

## Never share

Owner: “Never let guests unlock the front door.” Hermes prepares the change and the
owner presses Confirm; nothing changes before that. The list applies at once to
existing and new access and overrides any grant. The same list has buttons in
`/guests_setup`.

## Receive an invitation

The guest opens the Telegram link and sees “Waiting for the owner's approval.”
No service access exists yet. The owner sees the actual account and the complete
scope and accepts or denies it. The guest is notified either way. An accepted
guest can send text messages to the existing bot using only the granted tools.
Photos, files, voice, and other native commands receive a clear text-only message.

## Inspect and change

Owner: `/guests` lists everyone, pending reviews and invitations, with an
**Activity** button for everyone's recent activity. Each guest opens to change what
they can use, **Extend** their time, see their activity, or end or delete. Extend
counts from the current end time, or from now if access has ended, and keeps the
permissions; it leads to the usual review. “What can Alex do?” “Also allow the porch light.” “Keep media, remove home
controls.” “Another day.” “End Alex's access.” Each root change shows a complete
new review. Expansions preserve the end time unless the owner changes it.

A length of time ("15 minutes", "a week") starts when the owner approves: for a new
guest, when the owner confirms their account. More time on access that is still on
("another hour") is added to the current end, from the Extend button, from chat, or
when the guest asks; once access has ended it runs from approval. Reviews say "Lasts 15 minutes from
approval" and show where it would end if approved now. Access of a day or more gets a
24-hour invitation window by default.

Guest: `/access` shows actual permissions and expiry; `/new` resets conversation
context without extending access. Guests get a message about two minutes before
access ends and another when it ends, with the exact time and a Request more time
button. Asking for more time offers the length the owner chose last time, 1 hour or
1 day, or the guest types a length such as "6 hours". The owner sees "Alex asks for
15 minutes more", can change the length before approving, and no extension happens
automatically. Revoked guests need a new invitation. Both parties are notified of
changes.

Times: every message is written for the person reading it. A guest's messages use
their own time zone when the owner gives it ("Mom is in Vancouver") or the guest
says where they are; otherwise the owner's. The welcome says which time zone is
shown. The guest's model is told the current time and when access ends, and that
refusals from before a renewal no longer apply.

Names: the owner's name for a guest stays, and the claim card shows their Telegram
account beside it ("Someone opened your invitation for Mom. Telegram account:
Linda"). Set `guest_names: telegram` to use Telegram names once guests join.

Requests: a guest has one request open at a time. Only requests the owner declines
count: after three in a day the guest waits until the next day. When the owner
approves any change to that guest's access, an open request for more is answered.

Cards: by default a review opens with what is being decided ("Share with Alex?",
"Alex asks for 15 minutes more") and the access in plain words. **Details** shows the
exact rules. Activity is a plain summary of recent events with a **Full log** button;
readable activity is kept for 90 days by default (`activity_retention_days`).

## While work is running

The guest sees a typing indicator. A second request gets a clear busy response.
The owner can inspect/revoke while the model is thinking or a service is slow.
New access stops immediately on committed revocation; previously admitted actions
may finish. Stale model results are not delivered as if access were still active.

Useful subtasks get narrower access automatically, without involving either user.
Only a change to the guest's overall permissions returns to owner confirmation.

## Recover

`/guests` restores pending reviews/account approvals after a restart and
can replace a lost invitation link with unchanged permissions. Failed delivery
can be retried without issuing duplicate authority. **Check sharing** in `/guests` verifies saved
access and explains paused grants. Lost identity recovery is explicit and explains
that existing local access ends before a fresh setup/invitation flow.

No ordinary journey exposes warrant blobs, signing commands, YAML, or cryptographic
verification steps. Advanced details are available in the technical documentation.
