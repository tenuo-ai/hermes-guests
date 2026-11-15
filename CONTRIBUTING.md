# Contributing to Hermes Guests

Thanks for trying Hermes Guests. Any of these helps:

- **Bugs:** something didn't work, or a card or message was confusing.
- **Improvements and ideas:** something you wanted to share with a guest but
  couldn't, or a step that took longer than it should.
- **Pull requests:** fixes, wording, docs, or support for a tool you use.

[Open an issue](https://github.com/tenuo-ai/hermes-guests/issues/new/choose) or send a
pull request. We reply within a week.

Ideas about how Hermes tool calls are authorized in general, beyond sharing with
guests, belong in [Hermes-Tenuo](https://github.com/tenuo-ai/hermes-tenuo).

## Pull requests

Keep a pull request to one change and say what it improves for owners or guests. CI
runs the full test suite on every pull request, so you don't need to run it all
yourself. To check locally, from this repository in your Hermes environment:

```sh
python -m pip install -e '.[test]'
python scripts/run_tests.py --fast
```

If your change affects what a guest may do, include a test showing that a refused
call never reaches the tool. We'll help with this in review.
[More on testing](docs/testing.md)

## Keep private things out

Leave invitation links, tokens, personal names and chat history out of issues,
screenshots and tests. Report security problems privately as described in
[SECURITY.md](SECURITY.md).

Contributions are provided under the [Apache-2.0 license](LICENSE).
