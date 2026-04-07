import json
import logging
import re
from datetime import datetime
from pathlib import Path

import anthropic
import pdfplumber
from fpdf import FPDF
from flask import Flask, request, render_template, send_file, jsonify
from werkzeug.exceptions import RequestEntityTooLarge

# ── logging ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("resume")

# ── app setup ────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

BASE = Path(__file__).parent
RESUMES_DIR = BASE / "resumes"
OUTPUTS_DIR = BASE / "outputs"
LOGS_DIR = BASE / "logs"
RESUMES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

client = anthropic.Anthropic()

# ── fixed JSON schema ────────────────────────────────────────────────

RESUME_SCHEMA = {
    "name": "",
    "title": "",
    "summary": "",
    "experience": [],
    "skills": [],
}

# ── fixed prompt templates ───────────────────────────────────────────

PARSE_PROMPT = """You are a resume parsing assistant.

Extract the resume into this exact JSON schema. JSON only, no other text.

Schema:
{
  "name": "",
  "title": "",
  "summary": "",
  "experience": [
    {"company": "", "role": "", "period": "", "bullets": []}
  ],
  "skills": ["skill1", "skill2"]
}

Resume text:
"""

REWRITE_PROMPT = """You are a resume optimization assistant.

Rewrite the resume based on the job description.

Constraints:
- Do not invent experience
- Keep metrics and numbers
- Keep professional tone
- Max bullet length 25 words
- Keep structure clean

Output: a single JSON object matching this schema exactly. JSON only, no markdown.

{
  "name": "",
  "title": "",
  "summary": "",
  "experience": [
    {"company": "", "role": "", "period": "", "bullets": []}
  ],
  "skills": ["skill1", "skill2"]
}

Resume:
{resume}

Job Description:
{jd}
"""

# ── HTML template for PDF ────────────────────────────────────────────

RESUME_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  @page { size: A4; margin: 20mm 18mm; }
  body { font-family: 'Helvetica Neue', Arial, sans-serif; color: #222; font-size: 11pt; line-height: 1.5; }
  h1 { font-size: 22pt; margin: 0 0 2px; }
  .title { font-size: 12pt; color: #555; margin-bottom: 12px; }
  .summary { margin-bottom: 16px; color: #333; }
  h2 { font-size: 13pt; border-bottom: 1px solid #ccc; padding-bottom: 4px; margin: 16px 0 8px; text-transform: uppercase; letter-spacing: 1px; color: #444; }
  .exp-header { display: flex; justify-content: space-between; margin-bottom: 2px; }
  .exp-header strong { font-size: 11pt; }
  .exp-header span { color: #666; font-size: 10pt; }
  .exp-role { color: #555; font-style: italic; margin-bottom: 4px; }
  ul { padding-left: 18px; margin: 4px 0 12px; }
  li { margin-bottom: 2px; }
  .skills { display: flex; flex-wrap: wrap; gap: 6px; }
  .skill-tag { background: #f0f0f0; padding: 3px 10px; border-radius: 4px; font-size: 10pt; }
</style>
</head>
<body>
  <h1>{name}</h1>
  <div class="title">{title}</div>
  <div class="summary">{summary}</div>

  <h2>Experience</h2>
  {experience_html}

  <h2>Skills</h2>
  <div class="skills">{skills_html}</div>
</body>
</html>"""


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


def resume_paths(resume_type: str = "default") -> tuple[Path, Path]:
    """resume_type별 파일 경로 반환"""
    return (
        RESUMES_DIR / f"{resume_type}_raw.txt",
        RESUMES_DIR / f"{resume_type}.json",
    )


def save_resume(raw_text: str, structured: dict, resume_type: str = "default"):
    """raw_text + JSON 둘 다 저장"""
    raw_path, json_path = resume_paths(resume_type)
    raw_path.write_text(raw_text, encoding="utf-8")
    json_path.write_text(json.dumps(structured, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"[2] JSON loaded — resume saved to resumes/{resume_type}.json")


def load_resume(resume_type: str = "default") -> tuple[str, dict] | None:
    """저장된 이력서 로드 (raw, structured)"""
    raw_path, json_path = resume_paths(resume_type)
    if not json_path.exists():
        return None
    raw = raw_path.read_text(encoding="utf-8") if raw_path.exists() else ""
    structured = json.loads(json_path.read_text(encoding="utf-8"))
    return raw, structured


def list_resume_types() -> list[str]:
    """저장된 resume_type 목록"""
    return sorted(
        p.stem for p in RESUMES_DIR.glob("*.json")
    )


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
                "company": sanitize(exp.get("company")) if isinstance(exp, dict) else "",
                "role": sanitize(exp.get("role")) if isinstance(exp, dict) else "",
                "period": sanitize(exp.get("period")) if isinstance(exp, dict) else "",
                "bullets": [
                    sanitize(b) for b in (exp.get("bullets", []) if isinstance(exp, dict) else [])
                ] if isinstance(exp.get("bullets"), list) else [],
            }
            for exp in (data.get("experience") or [])
            if isinstance(exp, dict)
        ],
        "skills": [sanitize(s) for s in (data.get("skills") or []) if isinstance(s, str)],
    }


def structure_resume(raw_text: str) -> dict:
    """Claude로 이력서 구조화 (고정 스키마)"""
    msg = client.messages.create(
        model="claude-sonnet-4-6-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": PARSE_PROMPT + raw_text}],
    )
    data = extract_json(msg.content[0].text)
    return validate_resume(data)


def rewrite_for_jd(raw_text: str, structured: dict, jd: str) -> dict:
    """JD에 맞게 이력서 재작성 → JSON 반환"""
    resume_str = json.dumps(structured, ensure_ascii=False)
    if raw_text:
        resume_str += "\n\n[Original text for reference]\n" + raw_text

    prompt = REWRITE_PROMPT.replace("{resume}", resume_str).replace("{jd}", jd)

    msg = client.messages.create(
        model="claude-sonnet-4-6-20250514",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    log.info("[4] Claude rewrite done")
    data = extract_json(msg.content[0].text)
    return validate_resume(data)


def json_to_html(data: dict) -> str:
    """JSON → HTML 변환 (validate_resume에서 이미 이스케이프됨)"""
    # Experience 섹션
    exp_parts = []
    for exp in data.get("experience", []) or []:
        bullets = "".join(f"<li>{b}</li>" for b in (exp.get("bullets") or []))
        exp_parts.append(
            f'<div class="exp-header"><strong>{exp.get("company", "")}</strong>'
            f'<span>{exp.get("period", "")}</span></div>'
            f'<div class="exp-role">{exp.get("role", "")}</div>'
            f"<ul>{bullets}</ul>"
        )

    # Skills 섹션 (validate_resume에서 null → []로 변환됨)
    skills = "".join(f'<span class="skill-tag">{s}</span>' for s in (data.get("skills") or []))

    return RESUME_HTML_TEMPLATE.format(
        name=data.get("name", ""),
        title=data.get("title", ""),
        summary=data.get("summary", ""),
        experience_html="".join(exp_parts),
        skills_html=skills,
    )


def json_to_pdf(data: dict, pdf_path: str):
    """JSON → PDF 직접 생성 (fpdf2)"""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # 한글 폰트 fallback: 시스템 폰트 탐색
    font_added = False
    for font_path in [
        "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]:
        if Path(font_path).exists():
            pdf.add_font("Korean", "", font_path, uni=True)
            pdf.set_font("Korean", size=11)
            font_added = True
            break
    if not font_added:
        pdf.set_font("Helvetica", size=11)

    # Name
    pdf.set_font_size(22)
    pdf.cell(0, 12, data.get("name", ""), new_x="LMARGIN", new_y="NEXT")

    # Title
    pdf.set_font_size(12)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 8, data.get("title", ""), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # Summary
    pdf.set_font_size(11)
    pdf.multi_cell(0, 6, data.get("summary", ""))
    pdf.ln(6)

    # Experience
    pdf.set_font_size(13)
    pdf.cell(0, 8, "EXPERIENCE", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    pdf.set_font_size(11)
    for exp in data.get("experience", []):
        # Company + period
        pdf.set_font_size(11)
        company = exp.get("company", "")
        period = exp.get("period", "")
        pdf.cell(0, 7, f"{company}  —  {period}", new_x="LMARGIN", new_y="NEXT")

        # Role
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, exp.get("role", ""), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        # Bullets
        for bullet in exp.get("bullets", []):
            pdf.cell(5)
            pdf.multi_cell(0, 5, f"•  {bullet}")
        pdf.ln(4)

    # Skills
    pdf.set_font_size(13)
    pdf.cell(0, 8, "SKILLS", new_x="LMARGIN", new_y="NEXT")
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    pdf.set_font_size(10)
    skills_text = "  |  ".join(data.get("skills", []))
    pdf.multi_cell(0, 6, skills_text)

    pdf.output(pdf_path)


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
    types = list_resume_types()
    has_resume = len(types) > 0
    return render_template("index.html", has_resume=has_resume, resume_types=types)


@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("pdf")
    if not file or not file.filename.endswith(".pdf"):
        return jsonify({"error": "PDF 파일을 업로드해주세요"}), 400

    resume_type = request.form.get("resume_type", "default").strip() or "default"

    raw_text = parse_pdf(file)
    log.info("[1] PDF parsed" + (" (text extracted)" if raw_text else " (no text — raw saved)"))

    # raw_text 항상 저장
    raw_path, _ = resume_paths(resume_type)
    raw_path.write_text(raw_text or "[PDF text extraction failed]", encoding="utf-8")

    if not raw_text:
        return jsonify({"error": "PDF에서 텍스트를 추출할 수 없습니다. 원본은 raw 파일에 저장됨."}), 400

    try:
        structured = structure_resume(raw_text)
    except Exception as exc:
        log.error("[2] Claude parsing failed", exc_info=exc)
        return jsonify({"error": "이력서 파싱 중에 오류가 발생했습니다. 로그를 확인해주세요."}), 500
    save_resume(raw_text, structured, resume_type)

    return jsonify({"ok": True, "structured": structured, "resume_type": resume_type})


@app.route("/generate", methods=["POST"])
def generate():
    """
    JD 입력 → 이력서 재작성 → PDF 생성

    Request: { "jd_text": "...", "resume_type": "product" }
    Flow:
      1. JSON 이력서 로드 (resume_type 기준)
      2. JD 텍스트 입력 받음
      3. Claude API로 resume + JD 전달
      4. summary + experience 재작성
      5. HTML 생성
      6. PDF 생성
      7. 파일 반환
    """
    body = request.json or {}
    resume_type = body.get("resume_type", "default").strip() or "default"
    jd = body.get("jd_text", body.get("jd", "")).strip()

    # [1] JSON 이력서 로드
    data = load_resume(resume_type)
    if not data:
        return jsonify({"error": f"'{resume_type}' 타입 이력서가 없습니다. 먼저 업로드해주세요."}), 400

    raw_text, structured = data
    log.info(f"[1] Resume loaded — type: {resume_type}")

    # [2] JD 텍스트 검증
    if not jd:
        return jsonify({"error": "JD를 입력해주세요 (jd_text 필드)"}), 400

    # [3] JD 저장 + 회사명 추출
    company = extract_company_name(jd)
    save_jd_log(jd, company)

    # [4] Claude API로 resume + JD → 재작성
    try:
        rewritten = rewrite_for_jd(raw_text, structured, jd)
    except Exception as exc:
        log.error("[4] Claude rewrite failed", exc_info=exc)
        return jsonify({"error": "JD 기반 재작성 중 오류가 발생했습니다. 로그를 확인해주세요."}), 500

    # [5] JSON → HTML 생성
    html = json_to_html(rewritten)
    log.info("[5] HTML generated")

    # [6] HTML → PDF 생성
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{company}_{date_str}"

    html_path = OUTPUTS_DIR / f"{filename}.html"
    pdf_path = OUTPUTS_DIR / f"{filename}.pdf"
    html_path.write_text(html, encoding="utf-8")
    json_to_pdf(rewritten, str(pdf_path))

    log.info(f"[6] PDF generated — outputs/{filename}.pdf")

    # [7] 결과 반환
    return jsonify({
        "ok": True,
        "html": html,
        "filename": filename,
        "pdf_path": f"{filename}.pdf",
        "resume_type": resume_type,
    })


@app.route("/download/<filename>")
def download(filename):
    pdf_path = OUTPUTS_DIR / f"{filename}.pdf"
    if not pdf_path.exists():
        return "PDF를 찾을 수 없습니다", 404
    return send_file(pdf_path, as_attachment=True, download_name=f"{filename}.pdf")


@app.route("/resume")
@app.route("/resume/<resume_type>")
def get_resume(resume_type="default"):
    data = load_resume(resume_type)
    if not data:
        return jsonify({"error": f"'{resume_type}' 타입 이력서가 없습니다"}), 404
    return jsonify(data[1])


@app.route("/resume-types")
def get_resume_types():
    return jsonify(list_resume_types())


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(error):
    return jsonify({"error": "PDF 파일은 16MB 이하로 업로드해 주세요."}), 413


if __name__ == "__main__":
    app.run(debug=True, port=5000)
