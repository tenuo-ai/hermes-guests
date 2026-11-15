# Testing and acceptance

Use a dedicated Hermes test home, test Telegram bot, and recording service fixtures.
Keep credentials, sender IDs, chats, warrants, and runtime evidence out of Git.
Do not clone an owner's personal history, memories, credentials, or configuration.
Do not modify an existing user deployment to run these tests.

## Automated checks

Use a Hermes checkout with its dependencies installed. In that Python environment,
install Hermes Guests' test dependencies from this repository and run the full check.
Native-agent tests import Hermes modules such as `run_agent`; if this repository
lives elsewhere, add the Hermes checkout to `PYTHONPATH`.

```sh
python -m pip install -e '.[test]'
python scripts/check.py
```

The check runs lint/format, the suite, guard probes and a wheel build. To run the
tests and probes individually:

```bash
python -m unittest discover -s tests -v
python experiments/released_guard.py
```

Native agent tests need Hermes source/dependencies in that environment. They use
an in-process scripted model endpoint and a recording backend; they do not contact
Telegram, a real model provider, or real home devices. Preserve the distinction
between fixture tests and live validation when reporting results.

Every pull request and every push to `main` runs `scripts/check.py` on Linux
against each Hermes release listed in `.github/workflows/checks.yml`: lint,
format, the test suite, the released-guard probes, and a wheel build. Hermes is
pinned there by commit. Add a release to that list before calling it verified.

## Test the README install

From the Hermes Guests checkout, run this with a Hermes checkout whose `.venv`
has the Hermes and test dependencies described above.

```sh
bash scripts/test-install.sh /path/to/hermes-agent
```

The script installs the current committed Guests checkout through Hermes's
native installer. It exercises onboarding, an invitation, and guest tool use in
two temporary homes, including one with existing Telegram tool settings. The
same check runs in CI. It uses synthetic Telegram messages, a scripted model,
and a recording service, with no bot token or model API key.

To test a different committed source, pass its Git URL as the second argument.
To run with the official Home Assistant plugin against a simulated service,
install its Python dependencies in the Hermes environment and pass the plugin
directory as the third argument. For live testing, use a dedicated bot and the
[sharing scenarios](scenarios.md).

## Acceptance checklist

- Owner setup explains the full resulting access and creates keys automatically.
- A complete plain-English review precedes every guest root-scope change.
- An invitation grants nothing until the owner confirms the actual sender.
- A guest sees only approved tools and a separate conversation context.
- Allowed requests reach the fixture; denied requests have zero backend effects.
- Expansion, reduction, expiry, renewal, and revocation work across new sessions
  and restarts, including stale buttons and duplicate callbacks.
- Unknown users, forwarded links, spoofed names, and wrong-account buttons grant nothing.
- Narrowed subagents run automatically without another user confirmation.
- Missing keys, schema drift, failed services, and corrupt state do not widen access.
- Recovery explains what can be restored and which owner decision is required.
- Profile cleanup preserves audit evidence and never deletes unrelated profiles.
- Receipt evidence distinguishes authorization from service execution outcomes.

## Small-screen layout preview

With test dependencies installed, generate a local preview from the actual
recording-bot owner/guest flow.

```sh
python scripts/preview_telegram.py /tmp/guests-telegram-preview.html
# Include completed synthetic model reports when available
python scripts/preview_telegram.py /tmp/guests-telegram-preview.html --reports /tmp/journey-reports
```

Open that file in a browser and use a 320, 375 or 430-pixel viewport. Check the
review, claimant confirmation, welcome, permissions and long-name cards with
normal and enlarged text. All data is synthetic and the controls are disabled.
The preview uses the same rendered text and Telegram heading entities.

Its CSS approximates a chat layout. It cannot verify Telegram's fonts, native
button sizes, screen reader behavior, message delivery or real model proposal
quality. Record those checks separately in the human test.

## Real-model scenario evaluation

`tests/evaluate_journey.py` runs the native owner and guest with a hosted API-key
model, synthetic tools and a recording Telegram bot. It covers chat-only sharing,
company filings, calendar reading, reminders, media and mixed device controls.
It uses the installed skill to interpret requests, then exercises actual proposal,
confirmation, invitation, guest dispatch and denial paths. Calendar evaluation
also checks that the guest resumes permitted work after a boundary request.

Use a disposable container and fresh Hermes home for each case. Configure an
API key through Hermes for that profile, set `HERMES_GUESTS_EVAL_MODEL` and, for
a provider other than OpenAI, set `HERMES_GUESTS_EVAL_PROVIDER`. Run one case per
process. The automated Codex test exercises the native guest tool loop against
recorded Responses fixtures through the host credential request boundary.

```sh
python tests/evaluate_journey.py --case calendar --output /tmp/calendar-result.json
```

The report contains synthetic scope, card text, a guest response and effect counts.
Keep it in local ignored evidence storage. These are real model calls with simulated
approval and backend effects. Actual Telegram delivery and real integration semantics
remain part of live acceptance.

## Engineering acceptance

A human test begins with the repository README and the tester's own
Hermes installation. [Sharing scenarios](scenarios.md)

Use an isolated engineering instance, recording integrations and consenting
participants for fault injection and deterministic boundary checks. Verify
pre-model rejection for expired, revoked, wrong-holder and unverifiable warrants.
Confirm zero protected effects for denied service calls and memory writes. Cover
concurrent workers, deletion retry, stale callbacks, retained personalization,
reauthorization and fresh invitations after deletion. Check keys and audit
integrity through the native SDK. Record exact versions and source revisions.

Use consenting owner and guest participants and a dedicated bot. Record a useful
allowed result, a boundary attempt, access inspection, a permission change, and
revocation. Prefer synthetic catalogs and disposable service accounts. A recording
should show both chats without exposing tokens, keys, private IDs, or personal data.

Clean up only resources created for the test. Revoke test authority before deleting
its profiles. Do not stop unrelated services or erase retained audit evidence.
