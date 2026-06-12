#!/bin/bash
set -Eeuo pipefail

echo "[run.sh] cd repo root"
cd "$(dirname "$0")"

echo "[run.sh] activate venv"
source venv/bin/activate

echo "[run.sh] set PYTHONDONTWRITEBYTECODE=1"
export PYTHONDONTWRITEBYTECODE=1

echo "[run.sh] start app: python -u app.py"
exec python -u app.py
