#!/bin/bash
set -Eeuo pipefail

cd "$(dirname "$0")/.."

echo "== Resume Writer environment setup =="
echo "cwd: $(pwd)"

VENV_DIR="${RESUME_WRITER_VENV_DIR:-$HOME/.local/share/resume-writer/venv}"
VENV_PARENT="$(dirname "$VENV_DIR")"
echo "venv: $VENV_DIR"

PYTHON_BIN=""
for candidate in python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3.12 or python3.11 is required."
  exit 1
fi

mkdir -p "$VENV_PARENT"

if [ -e venv ] || [ -L venv ]; then
  stamp="$(date +%Y%m%d_%H%M%S)"
  mv venv "venv_broken_${stamp}"
  echo "Moved existing venv to venv_broken_${stamp}"
fi

if [ -e "$VENV_DIR" ]; then
  stamp="$(date +%Y%m%d_%H%M%S)"
  mv "$VENV_DIR" "${VENV_DIR}_broken_${stamp}"
  echo "Moved existing external venv to ${VENV_DIR}_broken_${stamp}"
fi

"$PYTHON_BIN" -m venv "$VENV_DIR"
ln -s "$VENV_DIR" venv
echo "python: $(venv/bin/python --version 2>&1)"

venv/bin/python -m pip install --upgrade pip setuptools wheel
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -m playwright install chromium

venv/bin/python -c "print('before', flush=True); from playwright.sync_api import sync_playwright; print('after import', flush=True)"

echo "Environment setup complete."
