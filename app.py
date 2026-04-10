import json
import logging
import re
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
import pdfplumber
from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL
from flask import Flask, request, render_template, send_file, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    sync_playwright = None
    class PlaywrightTimeoutError(Exception):
        pass
    PLAYWRIGHT_AVAILABLE = False

# ── logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("resume")

if not PLAYWRIGHT_AVAILABLE:
    log.warning("Playwright가 설치되지 않았습니다.")

# ── app setup ────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

BASE = Path(__file__).parent
SAMPLES_DIR = BASE / "resumes"  # 샘플 이력서 저장
OUTPUTS_DIR = BASE / "outputs"
LOGS_DIR = BASE / "logs"
SAMPLES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# OpenRouter 설정 (더 저렴한 모델로 변경 가능)
# 사용 가능한 모델:
# - "deepseek/deepseek-chat" (기본, ~$0.14/1M tokens)
# - "openai/gpt-4-turbo" (고급)
# - "anthropic/claude-3-haiku" (가장 저렴)
OPENROUTER_MODEL = "openai/gpt-4o-mini"  # 더 저렴하고 빠름
HTTP_TIMEOUT = 30.0
http_client = httpx.Client(timeout=HTTP_TIMEOUT)

# ── fixed JSON schema ────────────────────────────────────────────────

RESUME_SCHEMA = {
    "name": "",
    "title": "",
    "summary": "",
    "experience": [],
    "skills": [],
}

# ── fixed prompt templates ───────────────────────────────────────────

REWRITE_PROMPT = """You are a professional resume writer. Your task is to make the provided resume more relevant to the target job while maintaining honesty and authenticity.

## CORE PRINCIPLE:
**Preserve the actual experience and achievements. Only reframe language and emphasis to highlight genuine relevance to the target role.**

## INSTRUCTIONS:

1. **Keep All Facts & Numbers**:
   - Every achievement, metric, and timeline MUST stay exactly as provided
   - Do NOT add or modify quantifiable results
   - Do NOT exaggerate responsibilities or scope

2. **Smart Reframing (Language Only)**:
   - Use terminology from the JD that matches your actual experience (e.g., if you managed "customer relations" and JD says "stakeholder management", use their term)
   - Reorder bullets to show most relevant work first
   - Keep original context - don't distort what you actually did

3. **Title Strategy**:
   - Adjust title to align with target role IF it reflects your actual position
   - If current title is "Account Manager" and JD seeks "Product Manager", only change if you genuinely did product work
   - Keep it honest over perfect match

4. **Skills & Summary**:
   - Extract skills you actually have that appear in the JD
   - Write summary highlighting genuine overlaps
   - Do NOT add skills you don't have

5. **What NOT to Do**:
   - ❌ Rewrite experience bullets to claim things you didn't do
   - ❌ Add metrics or achievements that weren't in original
   - ❌ Expand scope of past roles beyond what actually happened
   - ❌ Change the narrative of what your role was

## OUTPUT FORMAT (JSON ONLY - no markdown, no explanation):

{{
  "name": "string",
  "title": "string (role title - keep honest)",
  "summary": "string (3-4 sentences, true highlights that match JD)",
  "experience": [
    {{"company": "string", "role": "string", "period": "string", "bullets": ["bullet1 (original fact, JD-relevant language)", "bullet2", ...]}}
  ],
  "skills": ["skill1 (you actually have)", "skill2", "skill3"]
}}

## CONTEXT:

TARGET JOB:
{jd}

RESUME DATA:
{resume}

---

Now adapt the resume to the target role while keeping all facts and achievements exactly as they are. Change only the framing and language. JSON only."""

# ── HTML template for PDF ────────────────────────────────────────────

RESUME_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
  <style>
    @page {{ size: A4; margin: 20mm 18mm 20mm 18mm; }}
    html {{ height: 100%; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', sans-serif; color: #333; font-size: 10.5pt; line-height: 1.6; margin: 0; padding: 20mm 18mm; min-height: 1123px; display: flex; flex-direction: column; width: 100%; box-sizing: border-box; background: #fff; }}

    @media print {{
      body {{ margin: 0; padding: 0; }}
      * {{ outline: none !important; }}
    }}

    .resume-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; margin-bottom: 4px; padding-bottom: 12px; border-bottom: 1px solid #d8d8d8; }}
    .header-left {{ flex: 1; }}
    .header-left h1 {{ font-size: 32pt; margin: 0; color: #111; line-height: 1.1; }}
    .location-availability {{ font-size: 10pt; color: #6a6a6a; line-height: 1.4; margin: 4px 0 0; }}
    .header-right {{ width: 220px; display: flex; flex-direction: column; align-items: flex-end; gap: 6px; color: #444; }}
    .contact-row {{ text-align: right; font-size: 10pt; }}
    .contact-link {{ color: #1c6ce4; text-decoration: none; font-weight: 600; font-size: 11pt; letter-spacing: 0.05em; text-transform: uppercase; }}
    .contact-link:hover {{ text-decoration: underline; }}
    .contact-detail {{ font-size: 10pt; color: #222; }}
    .summary-block {{ margin-top: 12px; margin-bottom: 16px; color: #343434; font-size: 11pt; line-height: 1.6; width: 100%; max-width: none; display: flex; flex-direction: column; gap: 6px; }}
    .summary-block .position-title {{ margin: 0 0 8px; font-size: 15pt; font-weight: 600; color: #0066cc }}
    .summary-text {{ margin: 0; max-width: none; }}

  h2 {{ font-size: 11pt; font-weight: 600; color: #555; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin: 14px 0 8px; letter-spacing: 0.5px; }}

  .exp-item {{ margin-bottom: 10px; }}
  .exp-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 2px; }}
  .exp-title {{ font-weight: 600; font-size: 10.5pt; color: #0066cc; }}
  .exp-company {{ font-size: 10pt; color: #666; }}
  .exp-period {{ font-size: 9.5pt; color: #888; }}
  .exp-meta {{ font-size: 9pt; color: #999; margin-bottom: 3px; }}

  ul {{ padding-left: 18px; margin: 4px 0 0 0; }}
  li {{ margin-bottom: 3px; font-size: 10pt; color: #444; }}

  .skills {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; margin-bottom: 40px; flex-grow: 1; }}
  .skill-tag {{ background: #f5f5f5; padding: 4px 10px; border-radius: 3px; font-size: 9.5pt; color: #555; border: 1px solid #e0e0e0; }}
</style>
</head>
<body>
  <div class="resume-header">
    <div class="header-left">
      <h1>{name}</h1>
      {availability_html}
    </div>
    <div class="header-right">
      {contact_html}
    </div>
  </div>

  {summary_block}

  {experience_html}

  {skills_html}
</body>
</html>"""
#
# ── OpenRouter helper ───────────────────────────────────────────────────────

def call_deepseek(prompt: str) -> dict:
    """OpenRouter API 호출 (429 재시도 로직 포함)"""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OpenRouter API key is not configured")

    url = f"{OPENROUTER_BASE_URL.rstrip('/')}/chat/completions"
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}"}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = http_client.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code == 429:
                wait_time = min(2 ** attempt, 30)  # 지수 백오프: 1초, 2초, 4초...
                if attempt < max_retries - 1:
                    log.warning(f"Rate limited (429). Retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429 and attempt < max_retries - 1:
                wait_time = min(2 ** attempt, 30)
                log.warning(f"Rate limited (429). Retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                continue
            log.error("OpenRouter API error: %s", exc)
            raise
        except httpx.RequestError as exc:
            log.error("OpenRouter request failed: %s", exc)
            raise

    raise RuntimeError("Max retries exceeded")


# ── helpers ──────────────────────────────────────────────────────────


def parse_pdf(file) -> str:
    """PDF에서 텍스트 추출 (fallback: 빈 텍스트도 raw로 저장)"""
    text = ""
    try:
        with pdfplumber.open(file) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        log.warning(f"PDF parsing partial failure: {e}")
    return text.strip()


def list_samples() -> list[str]:
    """샘플 이력서 목록 (확장자 제외)"""
    return sorted(
        p.stem for p in SAMPLES_DIR.glob("*.json")
    )


def load_sample(sample_name: str) -> dict | None:
    """샘플 이력서 로드"""
    path = SAMPLES_DIR / f"{sample_name}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        log.error(f"샘플 로드 실패 ({sample_name}): {e}")
        raise ValueError(f"샘플 파일 파싱 실패: {e}") from e


def save_jd_log(jd: str, company: str):
    """JD 원문을 logs/에 저장 (디버깅용)"""
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOGS_DIR / f"jd_{company}_{date_str}.txt"
    path.write_text(jd, encoding="utf-8")
    log.info(f"[3] JD received — saved to {path.name}")


def extract_json(text: str) -> dict:
    """Claude 응답에서 JSON 추출 + 검증"""
    try:
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("응답이 JSON 객체가 아닙니다")
        return data
    except json.JSONDecodeError as e:
        log.error(f"JSON 파싱 실패: {e}, 원본 텍스트: {text[:200]}")
        raise ValueError(f"JSON 파싱 실패: {e}") from e
    except Exception as e:
        log.error(f"응답 처리 중 오류: {e}")
        raise


def validate_resume(data: dict) -> dict:
    """Claude 응답 검증 + 기본값 처리"""
    def sanitize(val):
        """문자열 이스케이프, null → 기본값"""
        if val is None:
            return ""
        if not isinstance(val, str):
            return str(val)
        # HTML 태그 이스케이프
        return (
            val.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    return {
        "name": sanitize(data.get("name")),
        "title": sanitize(data.get("title")),
        "summary": sanitize(data.get("summary")),
        "experience": [
            {
                "company": sanitize(exp.get("company", "")) if isinstance(exp, dict) else "",
                "role": sanitize(exp.get("role") or exp.get("title", "")) if isinstance(exp, dict) else "",
                "period": sanitize(exp.get("period", "")) if isinstance(exp, dict) else "",
                "bullets": [
                    sanitize(b) for b in (exp.get("bullets") or exp.get("responsibilities") or exp.get("description") or [] if isinstance(exp, dict) else [])
                ] if isinstance((exp.get("bullets") or exp.get("responsibilities") or exp.get("description")), list) else [],
            }
            for exp in (data.get("experience") or [])
            if isinstance(exp, dict)
        ],
        "skills": [sanitize(s) for s in (data.get("skills") or []) if isinstance(s, str)],
    }




def rewrite_for_jd(raw_text: str, structured: dict, jd: str) -> dict:
    """JD에 맞게 이력서 재작성 → JSON 반환"""
    resume_str = json.dumps(structured, ensure_ascii=False)
    if raw_text:
        resume_str += "\n\n[Original text for reference]\n" + raw_text

    prompt = REWRITE_PROMPT.replace("{resume}", resume_str).replace("{jd}", jd)

    response = call_deepseek(prompt)
    log.info("[4] DeepSeek rewrite done")
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("DeepSeek 응답에 선택지가 없습니다")
    content = choices[0].get("message", {}).get("content", "")
    data = extract_json(content)
    return validate_resume(data)


def json_to_html(data: dict) -> str:
    """JSON → HTML 변환 (validate_resume에서 이미 이스케이프됨)"""

    # 연락처 정보 생성 (라벨 포함)
    contact_html = ""
    contact = data.get("contact", {}) or {}
    if isinstance(contact, dict):
        rows = []
        linkedin_val = contact.get("linkedin", "").strip()
        phone_val = contact.get("phone", "").strip()
        email_val = contact.get("email", "").strip()

        if linkedin_val:
            if linkedin_val.startswith("http"):
                linkedin_url = linkedin_val
            else:
                linkedin_url = f"https://linkedin.com/in/{linkedin_val}"
            rows.append(
                '<div class="contact-row">'
                f'<a class="contact-link" href="{linkedin_url}" target="_blank" rel="noreferrer">Linkedin</a>'
                "</div>"
            )

        if phone_val:
            rows.append(
                '<div class="contact-row">'
                f'<span class="contact-detail">{phone_val}</span>'
                "</div>"
            )

        if email_val:
            rows.append(
                '<div class="contact-row">'
                f'<span class="contact-detail">{email_val}</span>'
                "</div>"
            )

        contact_html = "".join(rows)

    # 가용성 정보 생성
    location = data.get("location", "").strip()
    availability = data.get("availability", "").strip()
    availability_text = ""
    if location and availability:
        availability_text = f"Based in {location}<br>Available to start immediately"
    elif location:
        availability_text = f"Based in {location}"
    elif availability:
        availability_text = availability

    availability_html = ""
    if availability_text:
        availability_html = f'<div class="location-availability">{availability_text}</div>'

    # Summary 섹션
    summary = data.get("summary", "").strip()
    title = data.get("title", "").strip()
    summary_block = ""
    if title or summary:
        block_parts = []
        if title:
            block_parts.append(f'<p class="position-title">{title}</p>')
        if summary:
            block_parts.append(f'<p class="summary-text">{summary}</p>')
        summary_block = '<div class="summary-block">' + "".join(block_parts) + "</div>"

    # Experience 섹션
    exp_html = ""
    experience = data.get("experience", []) or []
    if experience:
        exp_items = []
        for exp in experience:
            role = exp.get("role") or exp.get("title", "")
            company = exp.get("company", "")
            period = exp.get("period", "")
            bullets = exp.get("bullets") or exp.get("responsibilities") or exp.get("description") or []

            exp_title = f"{role}"
            if company:
                exp_title = f"{role} | {company}"

            bullets_html = "".join(f"<li>{b}</li>" for b in bullets)

            exp_item = f'''<div class="exp-item">
            <div class="exp-header">
              <span class="exp-title">{exp_title}</span>
              <span class="exp-period">{period}</span>
            </div>
            <ul>{bullets_html}</ul>
          </div>'''
            exp_items.append(exp_item)

        exp_html = f'<h2>Experience</h2>{"".join(exp_items)}'

    # Skills 섹션
    skills_html = ""
    skills = data.get("skills", []) or []
    if skills:
        skill_tags = "".join(f'<span class="skill-tag">{s}</span>' for s in skills)
        skills_html = f'<h2>Skills</h2><div class="skills">{skill_tags}</div>'

    return RESUME_HTML_TEMPLATE.format(
        name=data.get("name", ""),
        contact_html=contact_html,
        availability_html=availability_html,
        summary_block=summary_block,
        experience_html=exp_html,
        skills_html=skills_html,
    )


def json_to_pdf(data: dict, pdf_path: str):
    """JSON → PDF 직접 생성 (사실상 사용 안 함 - HTML로 대체)"""
    # HTML을 PDF로 변환하기 위해 HTML 파일만 저장하고
    # 클라이언트에서 print-to-PDF 사용
    pass


def extract_company_name(jd: str) -> str:
    """JD에서 회사명 추출 (간단한 휴리스틱)"""
    # 첫 몇 줄에서 회사명 추출 시도
    lines = jd.strip().split("\n")[:5]
    for line in lines:
        line = line.strip()
        if line and len(line) < 50:
            # 영문/한글 단어만 추출, 소문자 변환, 특수문자 제거
            name = re.sub(r"[^a-zA-Z0-9가-힣]", "", line.split()[0] if line.split() else "")
            if name:
                return name.lower()
    return "company"


 
 
# ── routes ───────────────────────────────────────────────────────────


@app.route("/")
def index():
    samples = list_samples()
    return render_template("index.html", samples=samples)


@app.route("/generate", methods=["POST", "OPTIONS"])
def generate():
    """
    샘플 선택 → JD 입력 → 이력서 재작성 → PDF 생성

    Request: { "sample": "igaming_am", "jd_text": "..." }
    """
    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    body = request.json or {}
    sample_name = body.get("sample", "").strip()
    jd = body.get("jd_text", "").strip()

    # [1] 샘플 이력서 로드
    if not sample_name:
        return jsonify({"error": "샘플을 선택해주세요"}), 400

    try:
        sample = load_sample(sample_name)
        if not sample:
            return jsonify({"error": f"'{sample_name}' 샘플을 찾을 수 없습니다"}), 404
    except Exception as exc:
        log.error(f"[1] Sample load failed ({sample_name})", exc_info=exc)
        return jsonify({"error": "샘플 로드 중 오류가 발생했습니다"}), 500

    log.info(f"[1] Sample loaded — {sample_name}")

    if not OPENROUTER_API_KEY:
        log.error("OpenRouter API key is not configured")
        return (
            jsonify(
                {
                    "error": (
                        "OpenRouter API 키가 설정되지 않았습니다. "
                        "OPENROUTER_API_KEY 또는 OPENAI_API_KEY 환경변수를 설정하고 서버를 다시 시작하세요."
                    )
                }
            ),
            503,
        )

    # [2] JD 검증
    if not jd:
        return jsonify({"error": "JD를 입력해주세요"}), 400

    # [3] JD 저장 + 회사명 추출
    company = extract_company_name(jd)
    save_jd_log(jd, company)

    # [4] Claude API로 sample + JD → 재작성
    try:
        rewritten = rewrite_for_jd("", sample, jd)
    except httpx.HTTPStatusError as exc:
        log.error("[4] DeepSeek HTTP error", exc_info=exc)
        return (
            jsonify(
                {
                    "error": (
                        "이력서 재작성 API가 오류를 반환했습니다. "
                        "OPENROUTER_API_KEY가 유효하고 DeepSeek 서비스가 정상인지 확인하세요."
                    )
                }
            ),
            502,
        )
    except httpx.RequestError as exc:
        log.error("[4] DeepSeek request failed", exc_info=exc)
        return (
            jsonify(
                {
                    "error": (
                        "이력서 재작성 API 호출에 실패했습니다. "
                        "네트워크 상태를 확인하거나 나중에 다시 시도해주세요."
                    )
                }
            ),
            502,
        )
    except Exception as exc:
        log.error("[4] DeepSeek rewrite failed", exc_info=exc)
        return jsonify({"error": "이력서 재작성 중 오류가 발생했습니다. 로그를 확인해주세요."}), 500

    # [4.5] 샘플의 연락처 정보 병합
    rewritten["contact"] = sample.get("contact", {})
    rewritten["location"] = sample.get("location", "")
    rewritten["availability"] = sample.get("availability", "")

    # [5] JSON → HTML 생성
    html = json_to_html(rewritten)
    log.info("[5] HTML generated")

    # [6] HTML 저장 (브라우저에서 print-to-PDF 사용)
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{company}_{date_str}"

    html_path = OUTPUTS_DIR / f"{filename}.html"
    html_path.write_text(html, encoding="utf-8")

    log.info(f"[6] HTML saved — outputs/{filename}.html")

    return jsonify({
        "ok": True,
        "html": html,
        "filename": filename,
        "pdf_path": f"{filename}.pdf",
        "sample": sample_name,
    })


@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    """HTML → PDF 변환 후 다운로드"""
    body = request.json or {}
    html_content = body.get("html", "").strip()
    filename = body.get("filename", "resume").strip()

    if not html_content:
        return jsonify({"error": "HTML content required"}), 400

    if not PLAYWRIGHT_AVAILABLE:
        message = "Playwright가 설치되지 않았습니다. pip install playwright && playwright install chromium 명령을 실행하세요."
        log.error(message)
        return jsonify({"error": message}), 503

    margins = {"top": "20mm", "right": "18mm", "bottom": "20mm", "left": "18mm"}

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page()
                page.set_content(html_content, wait_until="networkidle")
                pdf_bytes = page.pdf(
                    format="A4",
                    margin=margins,
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()

        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name=f"{filename}.pdf"
        )
    except PlaywrightTimeoutError as exc:
        log.error("Playwright timeout while rendering PDF", exc_info=exc)
        return jsonify({"error": "PDF 렌더링이 제한 시간을 초과했습니다."}), 504
    except Exception as e:
        log.error(f"PDF conversion failed: {e}", exc_info=e)
        return jsonify({"error": f"PDF 변환 중 오류가 발생했습니다: {str(e)}"}), 500


@app.route("/view/<filename>")
def view_resume(filename):
    """HTML 이력서를 브라우저에서 표시"""
    html_path = OUTPUTS_DIR / f"{filename}.html"
    if not html_path.exists():
        return "이력서를 찾을 수 없습니다", 404
    return send_file(html_path, mimetype="text/html")


@app.route("/samples")
def get_samples():
    """사용 가능한 샘플 목록"""
    return jsonify(list_samples())


@app.after_request
def set_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/sample/<name>")
def get_sample(name):
    """특정 샘플 조회"""
    sample = load_sample(name)
    if not sample:
        return jsonify({"error": f"'{name}' 샘플을 찾을 수 없습니다"}), 404
    return jsonify(sample)


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(error):
    return jsonify({"error": "PDF 파일은 16MB 이하로 업로드해 주세요."}), 413


if __name__ == "__main__":
    app.run(debug=True, port=8080)
