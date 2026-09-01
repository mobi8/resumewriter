#!/bin/bash
set -Eeuo pipefail

echo "[run.sh] cd repo root"
cd "$(dirname "$0")"

echo "[run.sh] activate venv"
source venv/bin/activate

echo "[run.sh] set PYTHONDONTWRITEBYTECODE=1"
export PYTHONDONTWRITEBYTECODE=1
export CAREER_OPS_HTML_DIR="${CAREER_OPS_HTML_DIR:-/Users/lewis/Desktop/career/career-ops/output/html}"

echo "[run.sh] start app: python -u app.py"
exec python -u app.py
