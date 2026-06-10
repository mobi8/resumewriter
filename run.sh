#!/bin/bash
cd "$(dirname "$0")"
set -Eeuo pipefail

PORT="${FLASK_PORT:-8080}"
FLASK_PID=""

cleanup_port() {
  local pids
  pids=$(lsof -ti :"$PORT" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    echo "${PORT} 포트 프로세스 강제 종료 중..."
    kill -9 $pids 2>/dev/null || true
  fi
}

cleanup() {
  local rc=$?
  trap - EXIT INT TERM ERR

  echo "서버 종료 중... 모든 하위 프로세스를 박멸합니다."

  # Flask 프로세스 직접 kill -9
  if [ -n "$FLASK_PID" ] && kill -0 "$FLASK_PID" 2>/dev/null; then
    kill -9 "$FLASK_PID" 2>/dev/null || true
  fi

  # 스크립트 자식 프로세스 전체 kill -9
  pkill -9 -P $$ 2>/dev/null || true

  # 포트 점유 프로세스 kill -9
  cleanup_port

  echo "청소 완료."
  exit "$rc"
}

on_error() {
  local rc=$?
  echo "❌ run.sh failed at line ${BASH_LINENO[0]} (exit ${rc})"
  exit "$rc"
}

trap cleanup EXIT INT TERM
trap on_error ERR

echo "== Resume Writer boot =="
echo "cwd: $(pwd)"

# 8080 포트에 남아 있는 이전 서버 정리
cleanup_port

# 포트가 완전히 해제될 때까지 대기 (최대 5초)
for i in $(seq 1 10); do
  lsof -ti :"$PORT" 2>/dev/null && sleep 0.5 || break
done

# .env 파일에서 환경 변수 로드
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# 가상환경 생성 및 활성화
# greenlet 등 C 확장 패키지가 Python 3.14와 호환되지 않으므로 3.12 → 3.11 순으로 고정
if [ ! -d "venv" ]; then
  echo "가상환경 생성 중..."
  VENV_PYTHON=""
  for candidate in python3.12 python3.11; do
    if command -v "$candidate" &>/dev/null; then
      VENV_PYTHON="$candidate"
      break
    fi
  done
  if [ -z "$VENV_PYTHON" ]; then
    echo "❌ python3.12 또는 python3.11을 찾을 수 없습니다."
    echo "   brew install python@3.12  으로 설치하세요."
    exit 1
  fi
  echo "사용할 Python: $("$VENV_PYTHON" -V 2>&1)"
  "$VENV_PYTHON" -m venv venv
fi

PYTHON="./venv/bin/python"
echo "python: $("$PYTHON" -V 2>&1)"
echo "python path: $("$PYTHON" -c 'import sys; print(sys.executable)')"

# pip 버전 경고를 억제
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PYTHONDONTWRITEBYTECODE=1

# 의존성 설치 (bash find로 flask 디렉토리 존재 여부만 확인 — 0ms)
if [ -n "$(find venv/lib -maxdepth 4 -name "flask" -type d 2>/dev/null | head -1)" ]; then
  echo "✓ 의존성 최신 상태"
else
  echo "의존성 설치 중... (최초 1회만 실행됩니다)"
  "$PYTHON" -m pip install -r requirements.txt
fi

echo "✓ Playwright chromium 준비됨"

# 환경 변수 확인
if [ -z "${OPENROUTER_API_KEY:-}" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "⚠️  경고: OPENROUTER_API_KEY 또는 OPENAI_API_KEY가 설정되지 않았습니다."
  echo "다음 중 하나를 실행하세요:"
  echo "  export OPENROUTER_API_KEY='your-key-here'"
  echo "  또는"
  echo "  export OPENAI_API_KEY='your-key-here'"
  exit 1
fi

echo "✓ API 키 로드 완료"
echo "🚀 Flask 앱 시작 중... (http://localhost:${PORT})"

# 디버그 모드 활성화 (소스 수정 시 자동 반영)
export FLASK_DEBUG=1

# Flask를 백그라운드로 실행해 PID를 추적하고, 종료 시 cleanup trap이 정확히 동작하게 함
"$PYTHON" -u app.py &
FLASK_PID=$!
wait $FLASK_PID
