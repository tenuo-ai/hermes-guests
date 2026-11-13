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
- Use disposable Hermes homes and recording services for automated tests. Changes
  to an existing deployment or messages to real people need explicit authorization.
- Keep credentials, personal identifiers, transcripts and runtime state out of Git.
  Use synthetic examples in fixtures and documentation.
- Distinguish automated fixtures, hosted-model checks, live Telegram delivery,
  actual service effects and observed usability. Report failures and skipped checks.
