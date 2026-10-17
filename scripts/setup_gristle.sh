#!/usr/bin/env bash
# Create a SEPARATE virtual environment for Gristle, the code-graph indexer used by `blastgate.codegraph`.
# Gristle pins mcp<2 and BlastGate pins mcp 2.x, so it must never be installed into BlastGate's .venv.
# Never removes or recreates an existing environment. Reads no .env file. Makes no LLM calls.
#
#   scripts/setup_gristle.sh                        # -> .gristle-venv/ in the repo root (git-ignored)
#   GRISTLE_VENV=~/gristle-venv PYTHON=python3.12 scripts/setup_gristle.sh
#
# Then:  export BLASTGATE_GRISTLE_BIN="<venv>/bin/gristle"
set -euo pipefail

GRISTLE_REPO="https://github.com/Alchemy-Agentic/gristle"
GRISTLE_REF="d72083df5528b167913747ddc13f2247622f5b5b"   # tag v0.12.0 (MIT)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${GRISTLE_VENV:-$ROOT/.gristle-venv}"
PY="${PYTHON:-python3.12}"

if [ -e "$VENV" ]; then
  echo "Not touching existing $VENV."
else
  "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Gristle needs Python >= 3.11 ($PY)"; exit 1; }
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --disable-pip-version-check --quiet \
    --constraint "$ROOT/scripts/gristle-constraints.txt" "gristle @ git+$GRISTLE_REPO@$GRISTLE_REF"
fi

"$VENV/bin/gristle" --help >/dev/null
echo "Gristle $("$VENV/bin/pip" show gristle | sed -n 's/^Version: //p') ready."
echo "export BLASTGATE_GRISTLE_BIN=\"$VENV/bin/gristle\""
