#!/usr/bin/env bash
# Start RouteOpt on a Linux/macOS machine: OSRM in Docker (pre-built data in
# docker/osrm/data), then the Streamlit app.  First run creates .venv and installs the app.
#
#   bash start.sh                      # app on http://0.0.0.0:8501
#   ROUTEOPT_PORT=8080 bash start.sh   # another port; ROUTEOPT_HOST=127.0.0.1 for local only
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

command -v docker >/dev/null || { echo "docker is not installed or not on PATH" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker is installed but the engine is not running (start Docker, or run with sudo -E)" >&2; exit 1; }
ls docker/osrm/data/routeopt-coverage.osrm.* >/dev/null 2>&1 || {
  echo "No OSRM dataset in docker/osrm/data - run docker/osrm/setup.sh first (see docker/osrm/README.md)" >&2
  exit 1
}

echo "Starting OSRM ..."
( cd docker/osrm && docker compose up -d )

if [[ ! -x .venv/bin/python ]]; then
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3)"
  echo "Creating .venv with $PY ..."
  "$PY" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
fi

# OSRM needs a few seconds to load after `up`; report, but never block the app on it.
for _ in 1 2 3 4 5 6; do
  if .venv/bin/routeopt osrm-check 2>/dev/null | grep -q healthy; then break; fi
  sleep 5
done
.venv/bin/routeopt osrm-check || true

HOST="${ROUTEOPT_HOST:-0.0.0.0}"
PORT="${ROUTEOPT_PORT:-8501}"
echo "RouteOpt on http://$HOST:$PORT  (Ctrl-C stops the app; OSRM keeps running in Docker)"
exec .venv/bin/streamlit run app/Home.py --server.address "$HOST" --server.port "$PORT" --server.headless true
