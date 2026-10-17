#!/usr/bin/env bash
# Start the judge demo: console + API on ONE explicit origin, with the code-graph change (PR-900) and investigation
# memory enabled. No LLM (deterministic planner) unless AGENT_PROVIDER is set. Never stops another process: if the
# port is taken it says so and exits. Prerequisites: FalkorDB running, `python blastgate_milestone1.py`, and
# `scripts/codegraph_demo.py` (which writes codegraph_demo_out/report.json).
#
#   scripts/start_demo.sh               # http://127.0.0.1:8766/
#   PORT=8770 scripts/start_demo.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8766}"

case "$PORT" in ''|*[!0-9]*) echo "PORT must be a number, got '$PORT'."; exit 1;; esac
if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/uvicorn ]; then
  echo "No BlastGate virtualenv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1
fi
if .venv/bin/python -c "import socket, sys; sys.exit(socket.socket().connect_ex(('127.0.0.1', int(sys.argv[1]))) != 0)" "$PORT"; then
  echo "Port $PORT is already in use by another process (see: lsof -nP -iTCP:$PORT -sTCP:LISTEN)."
  echo "That process may be an older BlastGate. Choose a free port instead, e.g.: PORT=8770 $0"
  exit 1
fi
if [ ! -f codegraph_demo_out/report.json ]; then
  echo "No code-graph review yet. Run first:"
  echo "  BLASTGATE_GRISTLE_BIN=.gristle-venv/bin/gristle BLASTGATE_MEMORY_GRAPH=blastgate_memory .venv/bin/python scripts/codegraph_demo.py"
  exit 1
fi

# FalkorDB must be up and the service graph seeded, or the demo would start "successfully" with nothing to show.
if ! .venv/bin/python -c "
import sys
from blastgate.graph import connect, graph_name
try:
    db = connect(); db.connection.ping()
except Exception as e:
    sys.exit(f'FalkorDB is not reachable ({type(e).__name__}). Start it first (see README Setup).')
if graph_name() not in db.list_graphs():
    sys.exit(f'Service graph {graph_name()!r} not found. Seed it: .venv/bin/python blastgate_milestone1.py')
"; then exit 1; fi

export BLASTGATE_CODEGRAPH_REPORT="$PWD/codegraph_demo_out/report.json"   # absolute: no cwd ambiguity
export BLASTGATE_MEMORY_GRAPH="${BLASTGATE_MEMORY_GRAPH:-blastgate_memory}"
export AGENT_PROVIDER="${AGENT_PROVIDER:-deterministic}"
echo "BlastGate demo: open http://127.0.0.1:$PORT/   (planner: $AGENT_PROVIDER; PR-900 is the real code change; Ctrl-C to stop)"
exec .venv/bin/uvicorn blastgate.api:app --host 127.0.0.1 --port "$PORT"
