#!/bin/bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

export PYTHONDONTWRITEBYTECODE=1
export FLASK_PORT="${FLASK_PORT:-8080}"
export CAREER_OPS_HTML_DIR="${CAREER_OPS_HTML_DIR:-/Users/lewis/Desktop/career/career-ops/output/html}"

if [ ! -x venv/bin/python ]; then
  echo "ERROR: venv is missing. Run scripts/setup_env.sh first."
  exit 1
fi

exec venv/bin/python -u app.py
