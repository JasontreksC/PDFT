#!/usr/bin/env bash
# Start the local Docling service (Apple GPU / MPS on an M-series Mac). First run creates .venv and installs Docling.
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi
echo "Docling service: http://0.0.0.0:${PORT:-8765}  (token file: $(pwd)/.token)"
exec .venv/bin/python -m uvicorn server:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8765}"
