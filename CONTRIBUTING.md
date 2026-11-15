# Contributing to Hermes Guests

You can help by trying your first guest invitation and telling us where you
got stuck. Clearer wording, a small reproduction, and fixes for a tool you
actually use are welcome contributions.

## Report a problem

[Open an issue](https://github.com/tenuo-ai/hermes-guests/issues) with what you
tried, what you expected and what happened. Include your Hermes and Hermes Guests versions,
the connected tool involved, and a redacted screenshot or small reproduction
when helpful. Keep tokens, invitation links, personal identifiers and private chat
history out of reports. Use the [security reporting instructions](SECURITY.md) for sensitive findings.

For a larger change, discuss the user problem in an issue before building it.

## Set up for development

Use a working Hermes checkout and its Python environment. Clone this repository,
then install the editable package and test dependencies from its root.

```sh
python -m pip install -e '.[test]'
python scripts/check.py
```

The check runs lint, formatting, the test suite, guard compatibility probes and a
wheel build. Native-agent tests need Hermes's source modules available to the same
Python interpreter. See [development testing](docs/testing.md) for setup details,
individual commands and optional model evaluations.

Automated tests use disposable Hermes homes, recording Telegram transport and
synthetic services. Human testing starts with the README and
[sharing scenarios](docs/scenarios.md). Use consenting participants and services
you're authorized to test.

To try the README install and first guest invitation locally, run
`bash scripts/test-install.sh /path/to/hermes-agent`. The
[install smoke test guide](docs/testing.md#test-the-readme-install) explains
the fixtures and how to exercise Home Assistant.

## Send a pull request

Keep the change focused and explain the user-facing behavior it improves. Add a
regression test for a fix or changed behavior, update affected documentation and
run the checks above. Report what passed, what you skipped and why.

Permission changes need negative tests proving that denied operations never reach
the protected handler. Preserve existing saved access and end times. The full
review and enforcement must use the same normalized permissions.
[Authorization details](docs/authorization.md)

Use synthetic names and resources in examples. Keep runtime state, credentials,
private keys and personal conversations out of commits and test output.

Contributions are provided under the project's [Apache-2.0 license](LICENSE).
