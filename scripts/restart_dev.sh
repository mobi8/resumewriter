#!/bin/bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${FLASK_PORT:-8080}"

log() {
  echo "[$(date '+%H:%M:%S')] $*"
}

stop_pid() {
  local pid="$1"
  local label="$2"

  if [ -z "$pid" ] || [ "$pid" = "$$" ]; then
    return
  fi

  if kill -0 "$pid" 2>/dev/null; then
    log "Stopping ${label}: PID ${pid}"
    kill "$pid" 2>/dev/null || true
    sleep 0.5
  fi

  if kill -0 "$pid" 2>/dev/null; then
    log "Force stopping ${label}: PID ${pid}"
    kill -9 "$pid" 2>/dev/null || true
  fi
}

cd "$ROOT"

log "== Resume Writer restart =="
log "Project: $ROOT"
log "Port: $PORT"

log "Step 1/3: stopping processes listening on port $PORT"
port_pids="$(lsof -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [ -n "$port_pids" ]; then
  for pid in $port_pids; do
    stop_pid "$pid" "port $PORT listener"
  done
else
  log "No process is listening on port $PORT"
fi

log "Step 2/3: stopping leftover Python processes from this repository"
repo_pids=""
while read -r pid cmd; do
  if [ -z "${pid:-}" ]; then
    continue
  fi

  case "$cmd" in
    *Python*|*python*)
      if printf '%s\n' "$cmd" | grep -F "$ROOT" >/dev/null 2>&1; then
        repo_pids="${repo_pids} ${pid}"
        continue
      fi

      cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' || true)"
      if [ "$cwd" = "$ROOT" ]; then
        repo_pids="${repo_pids} ${pid}"
      fi
      ;;
  esac
done < <(ps -ax -o pid= -o command= 2>/dev/null || true)

if [ -n "$repo_pids" ]; then
  for pid in $repo_pids; do
    stop_pid "$pid" "repo Python process"
  done
else
  log "No leftover repo Python process found"
fi

if [ ! -x venv/bin/python ]; then
  log "ERROR: venv/bin/python not found. Create the venv before restarting."
  exit 1
fi

log "Step 3/3: starting app"
export PYTHONDONTWRITEBYTECODE=1
export FLASK_HOST="${FLASK_HOST:-127.0.0.1}"
export FLASK_PORT="$PORT"
export CAREER_OPS_HTML_DIR="${CAREER_OPS_HTML_DIR:-/Users/lewis/Desktop/career/career-ops/output/html}"

log "Server command: venv/bin/python -u app.py"
exec venv/bin/python -u app.py
