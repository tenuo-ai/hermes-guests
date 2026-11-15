# Reviewing Hermes Guests

Hermes Guests puts usability first, with documented trade-offs (see the trust model in
docs/authorization.md). Review for real problems, not for more caution.

Flag:
- A guest getting access the owner didn't approve, or a refused call reaching a tool.
- One guest seeing another guest's or the owner's data.
- Lost or corrupted saved access, keys or end times; crashes; wrong results.

Don't flag, or mark as a low-priority suggestion only:
- Wording, layout or what a card shows. Plain cards are deliberately short; details
  live under Details.
- Adding limits, caps, confirmations, warnings or stricter defaults that make sharing
  harder, unless they close a real bypass.
- Documented trade-offs, such as signing on the owner's machine.
- Rare cases that need several unlikely things to happen together; say how likely it is.

Keep each comment to the problem, a concrete failing case and a small fix.
