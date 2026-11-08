#!/usr/bin/env bash
# May be run by itself as the cloud host's pocket user. No pairing, model or service changes.
set -euo pipefail
root="${HERMES_INSTALL_DIR:-$HOME/apps/hermes-agent}"
venv="$root/runtime/venv"
ready() { [ -x "$venv/bin/hermes" ] && "$venv/bin/hermes" --help >/dev/null; }
if ready; then exit 0; fi

if [ ! -f "$root/pyproject.toml" ]; then
  git clone -q "${HERMES_REPO_URL:-https://github.com/NousResearch/hermes-agent.git}" "$root"
fi
if [ ! -x "$venv/bin/python" ]; then python3 -m venv "$venv"; fi
# Reconcile dependencies against the checkout the CLI will import; do not fetch a new revision.
"$venv/bin/python" -m pip install -q -e "$root"
if ! ready; then
  printf 'Hermes CLI startup still fails after dependency repair (%s).\n' "$venv" >&2
  exit 1
fi
