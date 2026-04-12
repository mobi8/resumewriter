#!/bin/bash
cd "$(dirname "$0")"

# 8080 포트 및 관련 Flask 프로세스 모두 종료
PIDS=$(lsof -ti :8080 2>/dev/null)
if [ -n "$PIDS" ]; then
  echo "8080 포트 프로세스 종료 중..."
  kill -9 $PIDS 2>/dev/null
fi
# Flask debug reloader 부모 프로세스도 정리
pkill -9 -f "python3 app.py" 2>/dev/null || true
sleep 0.5

# .env 파일에서 환경 변수 로드
if [ -f .env ]; then
  export $(cat .env | grep -v '^#' | xargs)
fi

# 가상환경 생성 및 활성화
if [ ! -d "venv" ]; then
  echo "가상환경 생성 중..."
  python3 -m venv venv
fi

source venv/bin/activate

# pip 버전 경고를 억제
export PIP_DISABLE_PIP_VERSION_CHECK=1

# 의존성 설치 (핵심 패키지 import 가능하면 스킵)
if python3 -c "import flask, pdfplumber, httpx, playwright, fpdf" 2>/dev/null; then
  echo "✓ 의존성 최신 상태"
else
  echo "의존성 설치 중... (최초 1회만 실행됩니다)"
  pip install -r requirements.txt
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
python3 app.py
