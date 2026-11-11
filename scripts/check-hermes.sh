#!/usr/bin/env bash
# Run Hermes' own catalog checks against this plugin tree, inside WSL.
#   usage: scripts/check-hermes.sh [path-to-plugin-tree]   (default: the repo root)
# Env:   HERMES_VENV  venv with Hermes installed (default: ~/hermes-thumbs/venv)
# Exits non-zero if any check fails. Never touches ~/.hermes/.env or starts the gateway.
set -u

TREE="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
TREE="$(cd "$TREE" && pwd)"
VENV="${HERMES_VENV:-$HOME/hermes-thumbs/venv}"

if [ ! -f "$VENV/bin/activate" ]; then
  echo "check-hermes: no virtualenv at $VENV (see docs/live-setup.md, section 0)" >&2
  exit 2
fi
# shellcheck disable=SC1091
. "$VENV/bin/activate"
command -v hermes >/dev/null || { echo "check-hermes: hermes not found in $VENV" >&2; exit 2; }

cd "$TREE" || exit 2
echo "== hermes: $(hermes --version 2>&1 | head -1)"
echo "== tree:   $TREE"

fail=0
run() {
  echo
  echo "== $*"
  "$@"
  rc=$?
  echo "== exit $rc"
  [ "$rc" -eq 0 ] || fail=1
}

run hermes plugins doctor . --ci
run hermes plugins validate . --install-deps

# ---- Ticket 4: integration test (real gateway_platform_event hook, fake adapter) ----
# Hermes' own modules are importable here (the venv); the repo root and tests/ are on pytest's path.
python -c 'import pytest' 2>/dev/null || pip install -q pytest
run python -m pytest -q tests/test_hermes_integration.py tests/test_hermes_llm.py tests/test_hermes_applying.py tests/test_hermes_commands.py -p no:cacheprovider -rs
# -------------------------------------------------------------------------------

echo
if [ "$fail" -eq 0 ]; then echo "check-hermes: ALL PASSED"; else echo "check-hermes: FAILED" >&2; fi
exit "$fail"
