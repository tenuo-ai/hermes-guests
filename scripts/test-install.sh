#!/usr/bin/env bash
# Run the README installer and the recorded Telegram sharing journey in disposable homes.
set -euo pipefail

if [[ $# -lt 1 || $# -gt 3 ]]; then
    echo 'Usage: scripts/test-install.sh HERMES_CHECKOUT [GUESTS_GIT_SOURCE] [HOME_ASSISTANT_PLUGIN]' >&2
    exit 2
fi

core=$(cd "$1" && pwd)
root=$(cd "$(dirname "$0")/.." && pwd)
source=${2:-file://$root}
ha_plugin=
if [[ $# -eq 3 ]]; then
    ha_plugin=$(cd "$3" && pwd)
fi
sandbox=$(mktemp -d "${TMPDIR:-/tmp}/hermes-guests-install.XXXXXX")
trap 'rm -rf -- "$sandbox"' EXIT
export HOME="$sandbox/host"
export GIT_TERMINAL_PROMPT=0
unset TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_USERS TELEGRAM_ALLOW_ALL_USERS
unset HASS_TOKEN HASS_URL
mkdir -p "$HOME"
export PYTHONPATH="$root:$core${PYTHONPATH:+:$PYTHONPATH}"
export GUESTS_FIXTURE_HOMEASSISTANT="$ha_plugin"
cd "$root"

for profile in fresh existing; do
    export HERMES_HOME="$sandbox/$profile"
    mkdir -p "$HERMES_HOME"
    unset GUESTS_FIXTURE_SAVED_TOOLS GUESTS_FIXTURE_HEADERLESS_SSE
    if [[ $profile == existing ]]; then
        fixture_toolset=fixture_media
        if [[ -n $ha_plugin ]]; then
            fixture_toolset=homeassistant
        fi
        printf 'platform_toolsets:\n  telegram: [%s]\n  cli: [web]\n' "$fixture_toolset" > "$HERMES_HOME/config.yaml"
        printf 'keep me\n' > "$HERMES_HOME/owner-state-sentinel"
        export GUESTS_FIXTURE_SAVED_TOOLS=1 GUESTS_FIXTURE_HEADERLESS_SSE=1
    fi
    printf '\nTesting %s profile\n' "$profile"
    "$core/.venv/bin/hermes" plugins install "$source" --enable --yes-deps
    "$core/.venv/bin/python" -c '
import importlib.util
import runpy
import sys
from pathlib import Path
from pm.environments import activate_dependencies

if any(importlib.util.find_spec(name) is None for name in ("telegram", "hermes_tenuo")):
    activate_dependencies(Path(sys.argv[1]))
sys.path.insert(0, str(Path.cwd()))
runpy.run_module("tests.owner_flow_fixture", run_name="__main__")
' "$core"
done
