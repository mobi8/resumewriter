#!/bin/bash
cd "$(dirname "$0")"
set -Eeuo pipefail

trap 'rc=$?; echo "❌ run.sh failed at line ${LINENO} (exit ${rc})"; exit ${rc}' ERR
echo "== Resume Writer boot =="
echo "cwd: $(pwd)"

# 8080 포트 및 관련 Flask 프로세스 모두 종료
pkill -9 -f "app.py" 2>/dev/null || true
PIDS=$(lsof -ti :8080 2>/dev/null || true)
if [ -n "$PIDS" ]; then
  echo "8080 포트 프로세스 종료 중..."
  kill -9 $PIDS 2>/dev/null || true
fi
# 포트가 완전히 해제될 때까지 대기 (최대 5초)
for i in $(seq 1 10); do
  lsof -ti :8080 2>/dev/null && sleep 0.5 || break
done

# .env 파일에서 환경 변수 로드
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

# 가상환경 생성 및 활성화
if [ ! -d "venv" ]; then
  echo "가상환경 생성 중..."
  python3 -m venv venv
fi

PYTHON="./venv/bin/python"
echo "python: $("$PYTHON" -V 2>&1)"
echo "python path: $("$PYTHON" -c 'import sys; print(sys.executable)')"

# pip 버전 경고를 억제
export PIP_DISABLE_PIP_VERSION_CHECK=1

# 의존성 설치 (무거운 import 대신 패키지 존재 여부만 확인)
if "$PYTHON" - <<'PY' 2>/dev/null
import importlib.util
missing = [name for name in ("flask", "pdfplumber", "httpx", "playwright", "fpdf") if importlib.util.find_spec(name) is None]
raise SystemExit(1 if missing else 0)
PY
then
  echo "✓ 의존성 최신 상태"
else
  echo "의존성 설치 중... (최초 1회만 실행됩니다)"
  "$PYTHON" -m pip install -r requirements.txt
fi

echo "✓ Playwright chromium 준비됨"

# 환경 변수 확인
if [ -z "$OPENROUTER_API_KEY" ] && [ -z "$OPENAI_API_KEY" ]; then
  echo "⚠️  경고: OPENROUTER_API_KEY 또는 OPENAI_API_KEY가 설정되지 않았습니다."
  echo "다음 중 하나를 실행하세요:"
  echo "  export OPENROUTER_API_KEY='your-key-here'"
  echo "  또는"
  echo "  export OPENAI_API_KEY='your-key-here'"
  exit 1
fi

echo "✓ API 키 로드 완료"
echo "🚀 Flask 앱 시작 중... (http://localhost:8080)"

# 앱 실행
exec "$PYTHON" -u app.py
