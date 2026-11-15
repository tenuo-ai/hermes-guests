# Working on Hermes Guests

Read [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md),
[the implementation map](docs/implementation.md) and
[authorization](docs/authorization.md) before changing code. Use
[development testing](docs/testing.md) for the full validation workflow.

## Make focused changes

- Follow the maintainer's requested scope and branch workflow. Keep commits small
  and preserve concurrent work.
- Keep onboarding conversational. Use plain language for permissions and show
  commands in the reference. Derive examples from actual enabled tools.
- Update the README, reference or bundled skill when the behavior it describes
  changes. Put technical details in the relevant documentation.
- Preserve saved grants, identity, expiry and revocation behavior across updates.
- Keep release tags and candidate artifacts fixed. Use isolated test instances for
  development and promote a new tested candidate deliberately.

## Treat automated review as advice

- Maintainer direction supersedes automated review (Copilot, Codex and similar).
  Hermes Guests favours usability where the trade-off is documented, for example the
  [trust model](docs/authorization.md#trust-model-built-for-ease-of-use).
- Fix correctness bugs directly. A finding that changes what owners or guests see, or
  undoes a documented trade-off, needs the maintainer's decision first.
- Plain cards stay in plain words: the assistant's explanation and resource names, with
  ids, raw rule values and tools' official descriptions under Details.

## Preserve the trust boundary

- The model proposes permissions. Trusted code validates them, renders the complete
  review and issues authority after authenticated owner confirmation.
- Use the same normalized permission object for review and enforcement. Reject
  restrictions that cannot be represented safely.
- Use Tenuo's verification, attenuation and signed revocation APIs. Keep one
  enforcement path and verify that denied calls have zero protected effects.
- Keep signing keys outside model context and ordinary JSON state. Never expose
  owner credentials, files or memory to guest tools.
- Reuse Hermes identity, profiles, sessions and public plugin state. Keep guest
  memory isolated and enforce current access before starting the model.
- Repairs preserve approved scope and end time. New authority requires the
  appropriate owner confirmation.

## Test and report accurately

- Run `python scripts/check.py` with Hermes and test dependencies installed.
  Add regression tests for changed behavior and negative authorization cases.
- While iterating, run `python scripts/run_tests.py --fast` (or only the files you
  touched); CI runs every tier on each pull request.
- Use disposable Hermes homes and recording services for automated tests. Changes
  to an existing deployment or messages to real people need explicit authorization.
- Keep credentials, personal identifiers, transcripts and runtime state out of Git.
  Use synthetic examples in fixtures and documentation.
- This repository is public. Docs, comments, test names, commit messages and PR
  descriptions should make sense to outside users. `scripts/check_public.py` (part
  of `scripts/check.py`) flags personal emails, local paths and credentials.
- Distinguish automated fixtures, hosted-model checks, live Telegram delivery,
  actual service effects and observed usability. Report failures and skipped checks.
