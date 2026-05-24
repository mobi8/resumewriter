import json
import logging
import re
import os
import time
import tempfile
import threading
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path

from config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL
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

PDF_LOCK = threading.Lock()
PDF_PLAYWRIGHT = None
PDF_BROWSER = None

BASE = Path(__file__).parent
SAMPLES_DIR = BASE / "resumes"  # 샘플 이력서 저장
OUTPUTS_DIR = BASE / "outputs"
LOGS_DIR = BASE / "logs"
CAREER_OPS_HTML_DIR = Path("/Users/lewis/Desktop/career/career-ops/output/html")
SAMPLES_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)


def get_pdf_browser():
    """Return a warmed Chromium instance for PDF generation."""
    global PDF_PLAYWRIGHT, PDF_BROWSER
    with PDF_LOCK:
        if PDF_BROWSER:
            try:
                if PDF_BROWSER.is_connected():
                    return PDF_BROWSER
            except Exception:
                PDF_BROWSER = None
        from playwright.sync_api import sync_playwright

        if PDF_PLAYWRIGHT is None:
            PDF_PLAYWRIGHT = sync_playwright().start()
        PDF_BROWSER = PDF_PLAYWRIGHT.chromium.launch(headless=True)
        return PDF_BROWSER


def warm_pdf_browser():
    try:
        get_pdf_browser()
        log.info("[pdf] chromium warmed")
    except Exception:
        log.warning("[pdf] chromium warmup failed", exc_info=True)

# OpenRouter 설정 (더 저렴한 모델로 변경 가능)
# 사용 가능한 모델:
# - "deepseek/deepseek-chat" (기본, ~$0.14/1M tokens)
# - "openai/gpt-4-turbo" (고급)
# - "anthropic/claude-3-haiku" (가장 저렴)
OPENROUTER_MODEL = "openai/gpt-4o-mini"  # 더 저렴하고 빠름
HTTP_TIMEOUT = 90.0

LLM_MODE = os.getenv("LLM_MODE", "ats")  # ats | career_ops

# ── fixed JSON schema ────────────────────────────────────────────────

RESUME_SCHEMA = {
    "name": "",
    "title": "",
    "summary": "",
    "experience": [],
    "skills": [],
}

# ── fixed prompt templates ───────────────────────────────────────────

ATS_REWRITE_PROMPT = """You are a professional resume writer. Your task is to make the provided resume more relevant to the target job while maintaining honesty and authenticity.

## CORE PRINCIPLE:
**Preserve the actual experience and achievements. Only reframe language and emphasis to highlight genuine relevance to the target role.**

## INSTRUCTIONS:

1. **Keep All Facts & Numbers**:
   - Every achievement, metric, and timeline MUST stay exactly as provided
   - Preserve all quantified outcomes exactly as written, including volumes, percentages, uptime, cost savings, user counts, and timelines
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

6. **Experience Deduplication**:
  - Limit company/role labels to the header line for each experience entry; do not repeat them inside the bullets.
  - If the summary already references “Head of Wallet” or the employer name, keep the experience bullets focused on fresh outcomes or processes, not re-stating the same title or company.
  - When you detect a duplicated noun/verb that mirrors the company/role label, adjust the wording so each bullet highlights a distinct action, KPI, or stakeholder group.

## JD ALIGNMENT REQUIREMENTS:

- **Use the JD's actual role level first.** Infer the role level from the JD and the source experience together. If the JD clearly indicates a specialist or operations role, keep the title at that level. Do NOT promote it to "Head of", "Lead", or "Director" unless the JD and experience both support that level.
- **Match content depth to role level.** 
  - **Director/Head**: emphasize strategy, governance, cross-functional oversight, decision-making, and broad outcomes. Use fewer bullets and broader framing.
  - **Lead**: emphasize ownership, coordination, escalation handling, process improvement, and stakeholder alignment. Use a balanced mix of strategy and execution.
  - **Specialist/Operations**: emphasize hands-on execution, daily monitoring, transaction verification, issue resolution, tools, workflows, and operational details. Use concrete, action-oriented language and more detailed bullets.
- **Prefer wallet operations language.** When the JD is about deposits, withdrawals, transaction verification, block explorers, reconciliation, hot/cold wallets, delayed transactions, compliance coordination, customer support, test transactions, or SOPs, use those terms directly and prominently.
- **Echo JD phrasing** when it matches facts. Reuse only the JD terms that are truly supported by the source experience, especially "deposits and withdrawals", "blockchain transactions", "confirmations", "block explorers", "reconciliation", "hot wallet", "cold wallet", "stuck transactions", "compliance", "customer support", "test transactions", and "operational documentation".
- **Key Outcomes**: For each role, create 2–3 short lines before the detailed bullets that tie a quantified fact to a JD theme. Use concise operational language such as monitoring, verification, escalation, reconciliation, uptime, and risk control. Keep each line under 20 words.
- **Keep the tone role-appropriate.** Specialist/operations roles should read hands-on and practical. Lead roles should read like owners and coordinators. Director/Head roles should read broader and more strategic.
- **Avoid vague, abstract, or overly polished consulting-style expressions.** Prefer concrete operational verbs, tools, workflows, and outcomes over broad phrases unless they are paired with a specific wallet-ops action.
- **Keep the GGPoker role grounded** in wallet-ops framing: high-volume deposits/withdrawals, 24/7 monitoring, transaction verification, escalation ownership, compliance collaboration, and reporting. Do not frame it as a senior leadership role unless the source data supports that level.
- **Bullet depth rule**: Do not use a fixed number of bullets for every role. Let the JD relevance and source detail control the length. Dense, highly relevant roles should have 7-9 bullets and 2-3 key outcomes. Moderately relevant roles should have 4-6 bullets. Lower-relevance roles should stay at 2-3 bullets. Split distinct operational duties into separate bullets when the source material supports it.
- **JD language density**: The more relevant a role is to the target JD, the more directly its bullets should echo the JD's operational nouns and verbs. For the most relevant role, mirror the JD's vocabulary on deposits/withdrawals, transaction verification, block explorers, confirmations, reconciliation, wallet balances, support coordination, test transactions, and documentation wherever facts support it.
- **Wallet ops coverage**: For the most relevant role, try to surface at least some of the following where facts support them: deposits/withdrawals monitoring, transaction status checks or confirmations, reconciliation support, wallet balance tracking, delayed or stuck transaction escalation, support coordination, test transactions, and process documentation.
- **Specialist format expectation**: If the JD is specialist/operations, the summary should be short, direct, and hands-on. It should read like an operator describing what they do day to day. The experience section should feel practical and process-oriented, not executive or advisory.

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

PREFERRED ROLE LABEL (derived from the JD; use this for the summary heading if it fits):
{preferred_title}

JD KEYWORDS TO ECHO (auto-extracted from the JD; only reuse ones that truly match your facts):
{jd_keywords}

JD TIER:
{jd_tier}

SOURCE DETAIL AND BULLET DEPTH GUIDANCE:
{role_guidance}

- **Summary rewrite**: Rebuild the summary paragraph around the extracted keywords and the “Preferred Role Label.” Keep it aligned to specialist/operations level if that is what the JD indicates. The first sentence should immediately mirror the JD's core role language and include 2-3 concrete JD nouns or verbs if the facts support them. Use the summary to signal direct fit for wallet operations, not general leadership. Restate the key competencies in your own words using the new JD language - do not inflate the role level.
- **Summary level consistency**: If the JD and preferred title are specialist/operations level, the summary must stay specialist/operations level end-to-end. The opening should read like "Wallet Specialist with..." or "Wallet Operations Specialist with..." and should name the most relevant wallet operations facts immediately. Do not start with "Head of", "Lead", "Director", "leader", or strategy-heavy language.
- **Summary opening rule**: For specialist/operations roles, the summary must start with "Wallet Specialist with..." or "Wallet Operations Specialist with...". Do not begin the summary with any other role label, senior title, or leadership phrase.
- **Specialist summary style**: For specialist roles, use 2-3 sentences max, keep the verbs concrete, and mention operational actions such as monitoring, verifying, escalating, reconciling, tracking balances, supporting CS, or executing test transactions.
- **Specialist summary wording**: In the summary, avoid phrases like "wallet uptime" or platform-style engineering wording. Prefer operational phrases such as "operational reliability", "service continuity", or "smooth transaction flow". Keep detailed metrics like uptime, cost savings, and volumes more visible in the experience bullets.
- **Experience rewrite**: Each experience entry should also leverage the extracted keywords and preferred title. For the most JD-relevant roles, keep all distinct operational facts and expand them into separate bullets instead of compressing them into one line. Prioritize direct match language for the highest-fit roles, and only compress when the source material is genuinely thin. Treat every bullet as a fresh angle built on the current JD's vocabulary. For the highest-fit role, include concrete wallet ops details such as monitoring, confirmations, reconciliation, balance checks, escalation, reporting, and support coordination if the source facts allow it. If multiple operational actions happened in the same role, write them as separate bullets.
- **Bullet order and dedupe**: Within each role, order bullets so the most operationally important items come first. A good sequence is core wallet activity, then compliance/risk handling, then reporting/documentation, then optimization or growth. Remove repeated or near-duplicate bullets so each line adds a new fact or angle.
- **Role-level example target**: For a specialist wallet JD, aim for the kind of output that says: "Hands-on wallet operations specialist with experience managing high-volume crypto transactions across multiple blockchain networks..." and then follows with detailed operational bullets. The text should feel practical, not aspirational. For the best-fit role, it is acceptable to produce a denser set of bullets if each line adds a distinct wallet-ops action.
- **Metric preservation**: When rewriting, keep all strong metrics and quantified achievements visible in the final output. Do not drop numbers like $20M+/month, 99.9% uptime, $1M cost savings, 450K users, 30% cost reduction, or 40% response-time improvement unless the source data is genuinely not relevant to the target role.
- **Highest-fit roles first**: Identify the 1-2 roles in the resume that most strongly match the JD. Those roles should absorb the most JD language, the most bullets, and the most operational detail. Lower-fit roles should be shorter, more compact, and more generic.
- **Role title alignment**: Deduce the closest role label from the JD and use it as the title. If the JD clearly says "Wallet Specialist" or "Wallet Operations Specialist", keep that exact seniority. Use the supplied “Preferred Role Label” from the context block if it matches the JD; only fall back to the existing sample title when necessary. If the JD is broader or more senior, reflect that honestly rather than forcing a specialist label. Do not invent new employers - keep the experience company names accurate, but let the top title speak directly to the JD role.

---

Now adapt the resume to the target role while keeping all facts and achievements exactly as they are. Change only the framing and language. JSON only."""

CAREER_OPS_REWRITE_PROMPT = """You are generating a Career-Ops style ATS resume from the provided resume data and job description.

## CORE PRINCIPLES
- Preserve truthfulness: never invent experience, metrics, or scope.
- Optimize for ATS parsing and recruiter readability.
- Keep the top section concise, direct, and keyword-rich.
- Mirror the job description only where facts support it.
- Prefer concrete nouns and verbs over polished consulting language.
- Keep bullets distinct; do not repeat the same point.

## STRUCTURE
1. Professional Summary: 2-3 sentences max.
2. Experience: order by role relevance; most relevant role first.
3. Skills: only skills the candidate actually has and that match the JD.

## ATS WRITING RULES
- Use the JD keywords that are supported by the source resume.
- Preserve all numbers, timelines, employer names, and titles unless honestly reframed.
- For operations roles, prioritize monitoring, reconciliation, verification, escalation, support coordination, reporting, SOPs, and process control.
- For management roles, emphasize ownership, coordination, stakeholder alignment, and risk management.
- Avoid vague hype words like "passionate", "innovative", "world-class", "synergy", or "cutting-edge".
- Use plain HTML-safe text only.

## OUTPUT FORMAT (JSON ONLY - no markdown)
{{
  "name": "string",
  "title": "string",
  "summary": "string",
  "experience": [
    {{"company": "string", "role": "string", "period": "string", "bullets": ["..."]}}
  ],
  "skills": ["..."]
}}

## CONTEXT

TARGET JOB:
{jd}

RESUME DATA:
{resume}

PREFERRED ROLE LABEL:
{preferred_title}

JD KEYWORDS:
{jd_keywords}

ROLE GUIDANCE:
{role_guidance}

Now rewrite the resume in Career-Ops ATS style. JSON only."""

JD_STOPWORDS = {
    "and", "or", "the", "a", "an", "to", "with", "for", "of", "in", "across", "between",
    "into", "with", "ensure", "ensure", "ensure", "by", "from", "that", "this", "these",
    "those", "per", "as", "at", "on", "per", "while", "into", "through", "via", "within",
    "our", "its", "any", "other", "each", "every", "all", "own", "team", "teams", "lead",
    "leading", "leading-edge", "leading", "global", "industry", "world", "large", "improve",
    "build", "provide", "deliver", "drive", "driving", "operational", "operating", "operationally",
}


def extract_jd_keywords(jd_text: str, max_keywords: int = 12) -> list[str]:
    """단순 noun-phrase/keyword 추출하여 prompt에 넣을 리스트 식별"""
    cleaned = re.sub(r"<[^>]+>", " ", jd_text)
    phrases = Counter()
    for raw_line in cleaned.splitlines():
        line = raw_line.strip()
        if not line or len(line.split()) < 2:
            continue
        words = [
            token.lower()
            for token in re.findall(r"[A-Za-z0-9]+", line)
            if len(token) > 1
        ]
        words = [word for word in words if word not in JD_STOPWORDS]
        if not words:
            continue
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i : i + n])
                phrases[phrase] += 1
        for word in words:
            phrases[word] += 1
    if not phrases:
        return []
    sorted_phrases = sorted(
        phrases.items(),
        key=lambda item: (len(item[0].split()), item[1]),
        reverse=True,
    )
    keywords = []
    for phrase, _count in sorted_phrases:
        if phrase in keywords:
            continue
        keywords.append(phrase)
        if len(keywords) >= max_keywords:
            break
    return keywords


def format_keywords_for_prompt(keywords: list[str]) -> str:
    if not keywords:
        return "[]"
    return "[" + ", ".join(f'"{keyword}"' for keyword in keywords) + "]"


def parse_direction_sections(direction: str) -> str:
    if not direction or direction.strip() == "(none)":
        return "(none)"
    lines = [line.strip() for line in direction.splitlines() if line.strip()]
    if not lines:
        return "(none)"
    sections = []
    for line in lines:
        if ":" in line:
            section, note = line.split(":", 1)
            sections.append(f"- {section.strip()}: {note.strip()}")
        else:
            sections.append(f"- {line}")
    return "\n".join(sections)


def normalize_direction_text(direction: str) -> str:
    if not direction:
        return ""
    mapping = {"summary": "summary", "experience": "experience", "skills": "skills", "title": "title"}
    lines = []
    for raw in direction.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        prefix = next((k for k in mapping if lower.startswith(k)), None)
        if prefix and ":" in line:
            _, rest = line.split(":", 1)
            lines.append(f"{mapping[prefix]}: {rest.strip()}")
        elif prefix:
            lines.append(f"{mapping[prefix]}: {line[len(prefix):].strip()}")
        else:
            lines.append(line)
    return "\n".join(lines)


def flatten_role_text(exp: dict) -> str:
    """경력 항목의 텍스트를 하나로 합쳐 JD 겹침 점수 계산에 사용"""
    parts = [
        exp.get("title", ""),
        exp.get("role", ""),
        exp.get("company", ""),
        exp.get("period", ""),
    ]
    for field in ("responsibilities", "bullets", "description", "key_outcomes"):
        values = exp.get(field) or []
        if isinstance(values, list):
            parts.extend(v for v in values if isinstance(v, str))
        elif isinstance(values, str):
            parts.append(values)
    return " ".join(parts).lower()


def infer_jd_tier(jd_text: str) -> str:
    """JD에서 role seniority / level 신호를 추론"""
    cleaned = re.sub(r"<[^>]+>", " ", jd_text).lower()
    if re.search(r"\b(head of|head\b)", cleaned):
        return "head"
    if re.search(r"\b(vp|vice president|director|chief)\b", cleaned):
        return "executive"
    if re.search(r"\b(lead|leadership)\b", cleaned):
        return "lead"
    if re.search(r"\b(specialist|specialist\/operations|operations specialist|wallet specialist|wallet operations specialist)\b", cleaned):
        return "specialist"
    if re.search(r"\b(manager|senior manager)\b", cleaned):
        return "manager"
    return "general"


def build_role_guidance(structured: dict, jd_keywords: list[str]) -> str:
    """JD와 샘플 경력의 겹침을 기반으로 불렛 수/디테일 가이드 생성"""
    experiences = structured.get("experience") or []
    if not isinstance(experiences, list):
        return ""

    keyword_set = [k.lower() for k in jd_keywords if isinstance(k, str)]
    scored = []
    for exp in experiences:
        if not isinstance(exp, dict):
            continue
        text = flatten_role_text(exp)
        overlap = 0
        for keyword in keyword_set:
            if len(keyword) > 2 and keyword in text:
                overlap += 1
        detail_count = 0
        for field in ("responsibilities", "bullets", "description", "key_outcomes"):
            value = exp.get(field) or []
            if isinstance(value, list):
                detail_count += sum(1 for item in value if isinstance(item, str) and item.strip())
            elif isinstance(value, str) and value.strip():
                detail_count += 1
        title = exp.get("title") or exp.get("role") or exp.get("company") or "Role"
        score = overlap * 2 + min(detail_count, 8)
        scored.append((score, overlap, detail_count, title))

    if not scored:
        return ""

    scored.sort(reverse=True)
    lines = []
    lines.append("SOURCE DETAIL AND BULLET DEPTH GUIDANCE:")
    for score, overlap, detail_count, title in scored[:6]:
        if score >= 10 or detail_count >= 7 or overlap >= 4:
            bullet_target = "7-9 bullets"
            outcome_target = "2-3 key outcomes"
            note = "High JD overlap and rich source detail; expand the role with distinct operational angles and separate each wallet-ops action."
        elif score >= 6 or detail_count >= 4 or overlap >= 2:
            bullet_target = "4-6 bullets"
            outcome_target = "1-2 key outcomes"
            note = "Moderate relevance; keep the role concise but preserve the strongest relevant details."
        else:
            bullet_target = "2-3 bullets"
            outcome_target = "0-1 key outcomes"
            note = "Lower relevance; compress and avoid forcing extra detail."
        lines.append(f"- {title}: {bullet_target}, {outcome_target}. {note}")

    lines.append(
        "- If a role is operationally rich, split separate duties into separate bullets instead of merging them into one generic line."
    )
    lines.append(
        "- Preserve more detail for the most JD-relevant role; do not flatten strong source material into the same bullet count as weakly relevant roles."
    )
    lines.append(
        "- Order bullets in a logical operations flow: core wallet activity first, then compliance/risk handling, then reporting/documentation, then optimization or growth."
    )
    lines.append(
        "- Remove repeated or near-duplicate bullets within the same role so each line adds a distinct operational point."
    )
    return "\n".join(lines)


def normalize_compare_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def tokenize_compare_text(text: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", normalize_compare_text(text))
        if len(token) > 2
    }
    return tokens


def compare_similarity(text_a: str, text_b: str) -> float:
    tokens_a = tokenize_compare_text(text_a)
    tokens_b = tokenize_compare_text(text_b)
    if not tokens_a or not tokens_b:
        return 0.0
    overlap = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return overlap / union if union else 0.0


def bullet_priority_bucket(text: str) -> int:
    lower = text.lower()
    if any(
        phrase in lower
        for phrase in (
            "deposit",
            "withdrawal",
            "transaction",
            "confirm",
            "block explorer",
            "reconciliation",
            "wallet balance",
            "stuck",
            "delayed",
            "monitor",
            "node",
            "signing services",
            "wallet infrastructure",
        )
    ):
        return 0
    if any(
        phrase in lower
        for phrase in (
            "compliance",
            "kyc",
            "aml",
            "kyt",
            "fraud",
            "alerts",
            "exceptions",
            "auditor",
            "regulator",
            "risk",
        )
    ):
        return 1
    if any(
        phrase in lower
        for phrase in (
            "reporting",
            "documentation",
            "playbook",
            "sop",
            "customer support",
            "cs teams",
            "governance",
        )
    ):
        return 2
    if any(
        phrase in lower
        for phrase in (
            "optimization",
            "optimized",
            "automation",
            "cost",
            "savings",
            "launch",
            "scaled",
            "growth",
            "expand",
            "expansion",
            "integration",
            "user acquisition",
        )
    ):
        return 3
    return 4


def bullet_relevance_score(text: str, jd_keywords: list[str]) -> float:
    lower = text.lower()
    score = 0.0
    for keyword in jd_keywords:
        keyword_l = keyword.lower()
        if keyword_l and keyword_l in lower:
            score += 2.0 + min(len(keyword_l.split()), 3) * 0.5

    boost_terms = {
        "deposit": 2.5,
        "withdraw": 2.5,
        "transaction": 2.0,
        "confirm": 2.0,
        "block explorer": 2.5,
        "reconciliation": 2.5,
        "wallet balance": 2.0,
        "stuck": 2.0,
        "delayed": 2.0,
        "monitor": 1.5,
        "compliance": 1.5,
        "report": 1.5,
        "documentation": 1.5,
        "support": 1.0,
        "test": 1.5,
        "custody": 1.0,
        "signing": 1.0,
        "node": 1.0,
    }
    for term, boost in boost_terms.items():
        if term in lower:
            score += boost

    if any(term in lower for term in ("owned", "managed", "maintained", "led", "coordinated", "delivered", "ensured")):
        score += 0.75
    return score


def reorder_and_dedupe_bullets(bullets: list[str], jd_keywords: list[str]) -> list[str]:
    seen_texts: list[str] = []
    deduped: list[str] = []
    for bullet in bullets:
        if not isinstance(bullet, str):
            continue
        cleaned = re.sub(r"\s{2,}", " ", bullet).strip()
        if not cleaned:
            continue
        if any(compare_similarity(cleaned, prev) >= 0.78 for prev in seen_texts):
            continue
        seen_texts.append(cleaned)
        deduped.append(cleaned)

    scored = []
    for idx, bullet in enumerate(deduped):
        bucket = bullet_priority_bucket(bullet)
        score = bullet_relevance_score(bullet, jd_keywords)
        scored.append((bucket, -score, idx, bullet))

    scored.sort()
    return [bullet for _bucket, _neg_score, _idx, bullet in scored]


def trim_summary_redundancy(summary: str, bullets: list[str]) -> str:
    """summary와 거의 같은 문장을 bullet에서 한 번 더 쓰지 않도록 정리"""
    cleaned_summary = re.sub(r"\s{2,}", " ", (summary or "")).strip()
    if not cleaned_summary:
        return cleaned_summary

    for bullet in bullets:
        if compare_similarity(cleaned_summary, bullet) >= 0.72:
            # summary는 유지하되, 지나치게 반복되는 표현만 정리한다.
            cleaned_summary = re.sub(r"\b(24/7 monitoring and structured escalation)\b", "operational monitoring and escalation", cleaned_summary, flags=re.IGNORECASE)
            cleaned_summary = re.sub(r"\b(deposit and withdrawal resiliency)\b", "deposit and withdrawal operations", cleaned_summary, flags=re.IGNORECASE)
            cleaned_summary = re.sub(r"\bwallet governance and operational resilience\b", "wallet operations and resilience", cleaned_summary, flags=re.IGNORECASE)
            break
    return cleaned_summary


def normalize_title_for_tier(title: str, jd_tier: str, preferred_title: str) -> str:
    """JD level에 비해 과한 직급 표현을 완화"""
    if not title:
        return title
    cleaned = title.strip()
    if jd_tier in {"specialist", "manager", "general"}:
        cleaned = re.sub(r"^(Head of|Director of|VP of|Vice President of|Chief of)\s+", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"^(Head|Director|VP|Vice President|Chief)\s+", "", cleaned, flags=re.IGNORECASE)
        if preferred_title:
            preferred = preferred_title.strip()
            if preferred and not re.search(r"\b(head|director|vp|chief)\b", preferred, re.IGNORECASE):
                return preferred
    return cleaned


def build_direction_context(direction: str, html_content: str) -> str:
    if not direction or not direction.strip():
        return "(none)"
    lines = [line.strip() for line in direction.splitlines() if line.strip()]
    if not lines:
        return "(none)"
    parsed = []
    for line in lines:
        if ":" in line:
            section, detail = line.split(":", 1)
            section = section.strip()
            detail = detail.strip()
            parsed.append(f"- {section}: {detail}")
        else:
            parsed.append(f"- {line}")
    if html_content:
        parsed.append("")
        parsed.append("BASE HTML STRUCTURE HINT:")
        parsed.append("- Preserve the existing section order and only rewrite visible text inside the matching section.")
        parsed.append("- Treat summary, experience, and skills as separate targets; do not mix section content.")
    return "\n".join(parsed)


def build_html_rewrite_prompt(html_content: str, jd: str, jd_keywords: list[str], preferred_title: str, direction: str) -> str:
    section_hint = build_direction_context(direction, html_content)
    return f"""You are rewriting a resume HTML document.

## GOAL
- Keep the exact HTML structure and CSS classes.
- Only rewrite visible text content.
- Follow the user direction by section.
- If the HTML has Summary / Experience / Skills sections, only edit content inside those matching sections.
- Do not add new sections or reorder the existing structure.

## USER DIRECTION BY SECTION
{section_hint}

## TARGET JOB
{jd}

## JD KEYWORDS
{', '.join(jd_keywords) if jd_keywords else '(none)'}

## PREFERRED ROLE LABEL
{preferred_title or '(none)'}

## REWRITE RULES
- Use the JD language only where it matches the source facts.
- Keep facts, metrics, employers, dates, and section structure unchanged.
- Make the most JD-relevant section the most direct and detailed.
- If direction mentions a section name, prioritize that section exactly.

## RESUME HTML
{html_content}
"""


def clean_overstated_language(data: dict, jd_tier: str, preferred_title: str, jd_keywords: list[str]) -> dict:
    """과장된 직급/리더십 표현을 최소한으로 정리"""
    senior_markers = [
        (re.compile(r"\bHead of\b", re.IGNORECASE), "Wallet Specialist"),
        (re.compile(r"\bDirector\b", re.IGNORECASE), "Specialist"),
        (re.compile(r"\bVP\b", re.IGNORECASE), "Specialist"),
        (re.compile(r"\bChief\b", re.IGNORECASE), "Specialist"),
        (re.compile(r"\bHead\b", re.IGNORECASE), "Wallet Specialist"),
        (re.compile(r"\bleaders?\b", re.IGNORECASE), "professional"),
        (re.compile(r"\bleader with\b", re.IGNORECASE), "professional with"),
        (re.compile(r"\bleader\b", re.IGNORECASE), "professional"),
        (re.compile(r"\bstrategic leader\b", re.IGNORECASE), "operations professional"),
        (re.compile(r"\bstrategic\b", re.IGNORECASE), "operational"),
        (re.compile(r"\bexecutive\b", re.IGNORECASE), "operations"),
    ]

    if jd_tier in {"specialist", "manager", "general"}:
        data["title"] = normalize_title_for_tier(data.get("title", ""), jd_tier, preferred_title)
        summary = data.get("summary", "")
        summary = re.sub(r"\bHead of Wallet Operations\b", "Wallet Specialist", summary, flags=re.IGNORECASE)
        summary = re.sub(r"\bHead of Wallet\b", "Wallet Specialist", summary, flags=re.IGNORECASE)
        for pattern, replacement in senior_markers:
            summary = pattern.sub(replacement, summary)
        summary = re.sub(r"\bHead of\b", "Wallet Specialist", summary, flags=re.IGNORECASE)
        summary = re.sub(r"\bleader\b", "", summary, flags=re.IGNORECASE)
        summary = re.sub(
            r"^\s*(Wallet Specialist|Wallet Operations Specialist)\s*(?:\-|—|:)?\s*(?:with|who|having)?\s*",
            r"Wallet Specialist with ",
            summary,
            flags=re.IGNORECASE,
        )
        summary = re.sub(
            r"^\s*(?:Head of|Director|VP|Chief)[^\.]*\.\s*",
            "",
            summary,
            flags=re.IGNORECASE,
        )
        summary = re.sub(r"\s{2,}", " ", summary).strip()
        if jd_tier == "specialist":
            summary = re.sub(r"^\s*Wallet Specialist\s+with\s+proven\s+success\s+in\s+", "Wallet Specialist with experience in ", summary, flags=re.IGNORECASE)
            summary = re.sub(
                r"^(I\s+)?(excel|specialize|specialise|have|bring|offer)\s+in\b",
                "Wallet Specialist with proven experience in",
                summary,
                flags=re.IGNORECASE,
            )
            summary = re.sub(r"\bwallet uptime\b", "operational reliability", summary, flags=re.IGNORECASE)
            summary = re.sub(r"\b99\.9%\s*operational reliability\b", "operational reliability", summary, flags=re.IGNORECASE)
            if summary and not re.match(r"^(Wallet Specialist|Wallet Operations Specialist)\b", summary, re.IGNORECASE):
                summary = re.sub(r"^[A-Z][^,\.]*,?\s*", "", summary).strip()
                summary = f"Wallet Specialist with {summary[0].lower() + summary[1:]}" if len(summary) > 1 else "Wallet Specialist with relevant wallet operations experience."
            summary = re.sub(r"^\s*Wallet Specialist\s+leader\s+with\b", "Wallet Specialist with", summary, flags=re.IGNORECASE)
            summary = re.sub(r"^\s*Wallet Specialist\s+with\s+proven\s+success\s+in\b", "Wallet Specialist with experience in", summary, flags=re.IGNORECASE)
            if not summary.lower().startswith("wallet specialist"):
                summary = f"Wallet Specialist with {summary[0].lower() + summary[1:]}" if summary else "Wallet Specialist with relevant wallet operations experience."
        data["summary"] = summary

        for exp in data.get("experience", []) or []:
            if not isinstance(exp, dict):
                continue
            bullets = reorder_and_dedupe_bullets(exp.get("bullets") or [], jd_keywords=jd_keywords)
            cleaned_bullets = []
            for bullet in bullets:
                if not isinstance(bullet, str):
                    cleaned_bullets.append(bullet)
                    continue
                updated = bullet
                for pattern, replacement in senior_markers:
                    updated = pattern.sub(replacement, updated)
                updated = re.sub(r"\s{2,}", " ", updated).strip()
                cleaned_bullets.append(updated)
            exp["bullets"] = cleaned_bullets
            key_outcomes = exp.get("key_outcomes") or []
            if isinstance(key_outcomes, list):
                deduped_outcomes = reorder_and_dedupe_bullets(key_outcomes, jd_keywords=jd_keywords)
                exp["key_outcomes"] = deduped_outcomes[:3]

        data["summary"] = trim_summary_redundancy(data.get("summary", ""), [b for exp in data.get("experience", []) or [] if isinstance(exp, dict) for b in (exp.get("bullets") or [])])
    return data


def extract_role_label(jd_text: str) -> str:
    """JD에서 적합한 역할 라벨(Head of ...)을 추출"""
    cleaned = re.sub(r"<[^>]+>", " ", jd_text).strip()
    generic_titles = {
        "company overview",
        "role overview",
        "key responsibilities",
        "requirements",
        "education",
        "reporting line",
        "compensation",
        "how to apply",
    }
    wallet_patterns = [
        r"(Wallet Operations Specialist)",
        r"(Wallet Specialist)",
        r"(Wallet Operations Lead)",
        r"(Wallet Operations)",
        r"(Wallet Operations Manager)",
    ]
    for pattern in wallet_patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            title = match.group(1).strip()
            if title.lower() == "wallet operations":
                return "Wallet Operations Specialist"
            return title.title()
    patterns = [
        r"(Head of [A-Za-z &/]+)",
        r"(VP [A-Za-z &/]+)",
        r"(Lead [A-Za-z &/]+)",
        r"(Director [A-Za-z &/]+)",
        r"(Chief [A-Za-z &/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            return match.group(1).strip().title()
    first_line = cleaned.splitlines()[0] if cleaned.splitlines() else ""
    words = re.findall(r"[A-Za-z0-9&/]+", first_line)
    if words:
        candidate = " ".join(words[:4]).strip()
        if candidate.lower() in generic_titles:
            return "Wallet Specialist"
        return candidate
    return ""


def strip_pasted_font_styles(html: str) -> str:
    """Remove font styles introduced by rich-text paste while keeping resume layout CSS."""
    if not html:
        return html

    html = re.sub(
        r"""(?is)<style\b[^>]*\bid=["']resumeEditorFontGuard["'][^>]*>.*?</style>""",
        "",
        html,
    )

    def clean_style_attr(match: re.Match) -> str:
        quote = match.group(1)
        style = match.group(2)
        kept_rules = []
        for rule in style.split(";"):
            if ":" not in rule:
                continue
            prop, value = rule.split(":", 1)
            prop_name = prop.strip().lower()
            if prop_name in {"font", "font-family", "font-size", "line-height"}:
                continue
            kept_rules.append(f"{prop.strip()}: {value.strip()}")
        return f' style={quote}{"; ".join(kept_rules)}{quote}' if kept_rules else ""

    html = re.sub(r"""\sstyle=(["'])(.*?)\1""", clean_style_attr, html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"""\s(?:face|size)=(?:"[^"]*"|'[^']*'|[^\s>]+)""", "", html, flags=re.IGNORECASE)
    return html


# ── HTML template for PDF ────────────────────────────────────────────

RESUME_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  @page {{ size: A4; margin: 12mm; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', sans-serif; color: #333; font-size: 10.5pt; line-height: 1.62; margin: 0; padding: 12mm 0 0; background: #fff; }}
  .content-shell {{ margin: -12mm 0 0; padding: 0 0 18mm; }}

  @media print {{
    body {{ margin: 12mm; }}
    * {{ outline: none !important; }}
  }}

  .resume-header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; margin-top: -12mm; margin-bottom: 0; padding-bottom: 0; border-bottom: 1px solid #d8d8d8; }}
  .header-left {{ flex: 1; }}
  .header-left h1 {{ font-size: 30pt; margin: 0 0 4px; color: #111; line-height: 1.1; }}
  .location-availability {{ font-size: 10pt; color: #6a6a6a; line-height: 1.4; margin-bottom: 2px; }}
  .header-right {{ width: 200px; text-align: right; font-size: 10pt; color: #444; line-height: 1.4; }}
  .contact-row {{ margin-bottom: 8px; }}
  .contact-row a {{ color: #1c6ce4; text-decoration: none; font-weight: 500; font-size: 11pt; }}
  .contact-row span {{ font-size: 10pt; color: #282828; }}
  .summary-block {{ margin-top: 0; margin-bottom: 0; color: #343434; font-size: 11pt; line-height: 1.6; width: 100%; }}
  .summary-block .position-title {{ margin: 0 0 8px; font-size: 15pt; font-weight: 600; color: #0066cc }}
  .summary-text {{ margin: 0; }}

  h2 {{ font-size: 11pt; font-weight: 600; color: #555; border-bottom: 1px solid #ddd; padding-bottom: 4px; margin: 14px 0 8px; letter-spacing: 0.5px; }}

  .exp-item {{ margin-bottom: 10px; }}
  .exp-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 2px; }}
  .exp-title {{ font-weight: 600; font-size: 10.5pt; color: #0066cc; }}
  .exp-company {{ font-size: 10pt; color: #666; }}
  .exp-period {{ font-size: 9.5pt; color: #888; }}
  .exp-meta {{ font-size: 9pt; color: #999; margin-bottom: 3px; }}
  .outcome-label {{ font-size: 9pt; letter-spacing: 0.5px; font-weight: 600; color: #555; margin: 8px 0 4px; }}
  .key-outcomes {{ padding-left: 18px; margin: 0 0 6px 0; }}
  .key-outcomes li {{ color: #444; font-weight: 500; margin-bottom: 4px; }}

  ul {{ padding-left: 18px; margin: 4px 0 0 0; }}
  li {{ margin-bottom: 3px; font-size: 10pt; color: #444; }}

  .skills {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }}
  .skill-tag {{ background: #f5f5f5; padding: 4px 10px; border-radius: 3px; font-size: 9.5pt; color: #555; border: 1px solid #e0e0e0; }}
</style>
</head>
<body>
  <div class="content-shell">
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
  </div>
</body>
</html>"""
#
# ── OpenRouter helper ───────────────────────────────────────────────────────

def call_deepseek(prompt: str) -> dict:
    """OpenRouter API 호출 (429 재시도 로직 포함)"""
    import httpx

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
            log.info("[llm] OpenRouter request start — model=%s chars=%s", OPENROUTER_MODEL, len(prompt))
            response = httpx.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
            if response.status_code == 429:
                wait_time = min(2 ** attempt, 30)  # 지수 백오프: 1초, 2초, 4초...
                if attempt < max_retries - 1:
                    log.warning(f"Rate limited (429). Retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait_time)
                    continue
            response.raise_for_status()
            log.info("[llm] OpenRouter response ok — status=%s", response.status_code)
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
    import pdfplumber

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
                "key_outcomes": [
                    sanitize(k) for k in (exp.get("key_outcomes") or [])
                ] if isinstance(exp, dict) and isinstance(exp.get("key_outcomes"), list) else [],
            }
            for exp in (data.get("experience") or [])
            if isinstance(exp, dict)
        ],
        "skills": [sanitize(s) for s in (data.get("skills") or []) if isinstance(s, str)],
    }




def parse_direction_sections(direction: str) -> dict[str, str]:
    sections = {"summary": "", "experience": "", "skills": "", "title": "", "general": ""}
    if not direction or not direction.strip():
        return sections
    for raw_line in direction.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        target = "general"
        if lower.startswith("summary"):
            target = "summary"
        elif lower.startswith("experience"):
            target = "experience"
        elif lower.startswith("skills"):
            target = "skills"
        elif lower.startswith("title"):
            target = "title"
        if ":" in line:
            _, content = line.split(":", 1)
            sections[target] += content.strip() + "\n"
        else:
            sections[target] += line + "\n"
    return {k: v.strip() for k, v in sections.items()}


def extract_between_comments(html_content: str, start: str, end: str | None = None) -> str:
    """Return an HTML chunk between section comments when the template uses comment markers."""
    end_pattern = rf"(?=<!--\s*{re.escape(end)}\s*-->)" if end else r"(?=</div>\s*</body>|</body>)"
    pattern = rf"(<!--\s*{re.escape(start)}\s*-->[\s\S]*?){end_pattern}"
    match = re.search(pattern, html_content, re.IGNORECASE)
    return match.group(1) if match else ""


def extract_html_section_snapshot(html_content: str) -> str:
    summary_match = re.search(r"(<div class=\"summary-block\">[\s\S]*?</div>)", html_content, re.IGNORECASE)
    experience_match = re.search(r"(<h2>Experience</h2>[\s\S]*?)(?=<h2>Skills</h2>|</div>\s*</body>)", html_content, re.IGNORECASE)
    skills_match = re.search(r"(<h2>Skills</h2>[\s\S]*?</div>)", html_content, re.IGNORECASE)
    summary_html = summary_match.group(1) if summary_match else extract_between_comments(html_content, "PROFESSIONAL SUMMARY", "CORE COMPETENCIES")
    experience_html = experience_match.group(1) if experience_match else extract_between_comments(html_content, "WORK EXPERIENCE", "SKILLS")
    skills_html = skills_match.group(1) if skills_match else extract_between_comments(html_content, "SKILLS", "EDUCATION")
    return "\n".join([
        f"SUMMARY_SNAPSHOT: {summary_html or '(none)'}",
        f"EXPERIENCE_SNAPSHOT: {experience_html or '(none)'}",
        f"SKILLS_SNAPSHOT: {skills_html or '(none)'}",
    ])


def split_html_sections(html_content: str) -> dict[str, str]:
    summary_match = re.search(r"(<div class=\"summary-block\">[\s\S]*?</div>)", html_content, re.IGNORECASE)
    exp_match = re.search(r"(<h2>Experience</h2>[\s\S]*?)(?=<h2>Skills</h2>|</div>\s*</body>)", html_content, re.IGNORECASE)
    skills_match = re.search(r"(<h2>Skills</h2>[\s\S]*?</div>)", html_content, re.IGNORECASE)
    return {
        "summary": summary_match.group(1) if summary_match else extract_between_comments(html_content, "PROFESSIONAL SUMMARY", "CORE COMPETENCIES"),
        "experience": exp_match.group(1) if exp_match else extract_between_comments(html_content, "WORK EXPERIENCE", "SKILLS"),
        "skills": skills_match.group(1) if skills_match else extract_between_comments(html_content, "SKILLS", "EDUCATION"),
    }


def build_html_rewrite_prompt(html_content: str, jd: str, jd_keywords: list[str], preferred_title: str, direction: str) -> str:
    sections = parse_direction_sections(normalize_direction_text(direction))
    section_snapshot = extract_html_section_snapshot(html_content)
    return f"""You are rewriting a resume HTML document.

## GOAL
- Keep the exact HTML structure and CSS classes.
- Only rewrite visible text content.
- Follow the user direction by section.
- If the HTML has Summary / Experience / Skills sections, only edit content inside those matching sections.
- Do not add new sections or reorder the existing structure.

## USER DIRECTION BY SECTION
SUMMARY: {sections.get('summary') or '(none)'}
EXPERIENCE: {sections.get('experience') or '(none)'}
SKILLS: {sections.get('skills') or '(none)'}
TITLE: {sections.get('title') or '(none)'}
GENERAL: {sections.get('general') or '(none)'}

## HTML STRUCTURE SNAPSHOT
{section_snapshot}

## TARGET JOB
{jd}

## JD KEYWORDS
{', '.join(jd_keywords) if jd_keywords else '(none)'}

## PREFERRED ROLE LABEL
{preferred_title or '(none)'}

## REWRITE RULES
- Rewrite each section independently.
- Summary: keep it short and direct.
- Experience: preserve facts, reorder bullets, and apply the user direction only inside the relevant role.
- Skills: keep only skills supported by the source and JD.
- If a section has no user direction, keep it close to the original.
- Do not move content across sections.

## RESUME HTML
{html_content}
"""


def rewrite_html_for_jd(
    html_content: str,
    jd: str,
    jd_keywords: list[str],
    preferred_title: str,
    direction: str = "",
    llm_mode: str = "ats",
) -> str:
    """career-ops HTML 포맷을 유지하면서 JD에 맞게 재작성 → HTML 반환

    <style> 블록을 제거해 토큰을 줄이고, LLM 응답에 원본 스타일을 다시 삽입한다.
    """
    style_blocks = re.findall(r"<style[^>]*>[\s\S]*?</style>", html_content, re.IGNORECASE)
    stripped_html = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", html_content, flags=re.IGNORECASE)
    sections = split_html_sections(stripped_html)
    dir_map = parse_direction_sections(normalize_direction_text(direction))
    template_mode = llm_mode if llm_mode in {"ats", "career_ops"} else LLM_MODE
    mode_guidance = (
        "Career-Ops ATS mode: keep text concise, keyword-rich, recruiter-readable, and operationally direct."
        if template_mode == "career_ops"
        else "General ATS mode: preserve the existing detail depth while aligning wording honestly to the job description."
    )
    matched_sections = [name for name, value in sections.items() if value]
    log.info("[4] HTML section matches — %s", ", ".join(matched_sections) or "none")

    if not matched_sections:
        prompt = build_html_rewrite_prompt(stripped_html, jd, jd_keywords, preferred_title, direction)
        prompt = prompt.replace("## REWRITE RULES", f"## WRITING MODE\n{mode_guidance}\n\n## REWRITE RULES")
        response = call_deepseek(prompt)
        choices = response.get("choices") or []
        if not choices:
            raise ValueError("LLM 응답에 선택지가 없습니다: full_html")
        content = choices[0].get("message", {}).get("content", "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[^\n]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content.rstrip())
        if style_blocks:
            styles_str = "\n".join(style_blocks)
            if "</head>" in content:
                content = content.replace("</head>", f"{styles_str}\n</head>", 1)
            else:
                content = styles_str + "\n" + content
        return content

    rewritten_sections = {}
    for section_name in ("summary", "experience", "skills"):
        section_html = sections.get(section_name, "")
        if not section_html:
            rewritten_sections[section_name] = ""
            continue
        section_direction = dir_map.get(section_name, "")
        prompt = f"""You are rewriting ONLY the {section_name.upper()} section of a resume HTML document.

## GOAL
- Keep the exact HTML structure inside this section.
- Only rewrite visible text in this section.
- Do not change tags, classes, or surrounding document structure.
- Follow the user direction for this section.
- Keep facts, metrics, employers, dates, and achievements unchanged.

## WRITING MODE
{mode_guidance}

## SECTION DIRECTION
{section_direction or '(none)'}

## TARGET JOB
{jd}

## JD KEYWORDS
{', '.join(jd_keywords) if jd_keywords else '(none)'}

## PREFERRED ROLE LABEL
{preferred_title or '(none)'}

## SECTION HTML
{section_html}
"""
        response = call_deepseek(prompt)
        choices = response.get("choices") or []
        if not choices:
            raise ValueError(f"LLM 응답에 선택지가 없습니다: {section_name}")
        content = choices[0].get("message", {}).get("content", "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```[^\n]*\n?", "", content)
            content = re.sub(r"\n?```$", "", content.rstrip())
        rewritten_sections[section_name] = content

    content = stripped_html
    for section_name, original in sections.items():
        if original and rewritten_sections.get(section_name):
            content = content.replace(original, rewritten_sections[section_name], 1)

    if style_blocks:
        styles_str = "\n".join(style_blocks)
        if "</head>" in content:
            content = content.replace("</head>", f"{styles_str}\n</head>", 1)
        else:
            content = styles_str + "\n" + content
    return content
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("LLM 응답에 선택지가 없습니다")
    content = choices[0].get("message", {}).get("content", "").strip()
    # 마크다운 펜스 제거 (혹시 모델이 감쌀 경우)
    if content.startswith("```"):
        content = re.sub(r"^```[^\n]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content.rstrip())

    # 원본 <style> 블록을 </head> 앞에 다시 삽입
    if style_blocks:
        styles_str = "\n".join(style_blocks)
        if "</head>" in content:
            content = content.replace("</head>", f"{styles_str}\n</head>", 1)
        else:
            # <head>가 없으면 <body> 앞에 삽입
            content = styles_str + "\n" + content
    return content


def rewrite_for_jd(
    raw_text: str,
    structured: dict,
    jd: str,
    jd_keywords: list[str],
    preferred_title: str,
    llm_mode: str = "ats",
) -> dict:
    """JD에 맞게 이력서 재작성 → JSON 반환"""
    resume_str = json.dumps(structured, ensure_ascii=False)
    if raw_text:
        resume_str += "\n\n[Original text for reference]\n" + raw_text

    jd_tier = infer_jd_tier(jd)
    role_guidance = build_role_guidance(structured, jd_keywords)
    role_label = preferred_title or "Wallet Specialist"

    template_mode = llm_mode if llm_mode in {"ats", "career_ops"} else LLM_MODE
    template = CAREER_OPS_REWRITE_PROMPT if template_mode == "career_ops" else ATS_REWRITE_PROMPT
    prompt = (
        template.replace("{resume}", resume_str)
        .replace("{jd}", jd)
        .replace("{jd_keywords}", format_keywords_for_prompt(jd_keywords))
        .replace("{preferred_title}", role_label)
        .replace("{jd_tier}", jd_tier)
        .replace("{role_guidance}", role_guidance or "No special guidance available.")
    )

    response = call_deepseek(prompt)
    log.info("[4] DeepSeek rewrite done")
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("DeepSeek 응답에 선택지가 없습니다")
    content = choices[0].get("message", {}).get("content", "")
    data = extract_json(content)
    validated = validate_resume(data)
    return clean_overstated_language(validated, jd_tier, preferred_title, jd_keywords)


def json_to_html(data: dict) -> str:
    """JSON → HTML 변환 (validate_resume에서 이미 이스케이프됨)"""

    # 연락처 정보 생성 (라벨 포함)
    contact_html = ""
    contact = data.get("contact", {}) or {}
    if isinstance(contact, dict):
        rows = []
        if contact.get("linkedin"):
            linkedin_val = contact["linkedin"]
            if linkedin_val.startswith("http"):
                linkedin_url = linkedin_val
            else:
                linkedin_url = f"https://linkedin.com/in/{linkedin_val}"
            rows.append(
                '<div class="contact-row">'
                f'<a href="{linkedin_url}" target="_blank" rel="noreferrer">Linkedin</a>'
                "</div>"
            )

        if contact.get("phone"):
            rows.append(
                '<div class="contact-row">'
                f'<span>{contact["phone"]}</span>'
                "</div>"
            )

        if contact.get("email"):
            rows.append(
                '<div class="contact-row">'
                f'<span>{contact["email"]}</span>'
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
            key_outcomes = exp.get("key_outcomes") or []

            exp_title = f"{role}"
            if company:
                exp_title = f"{role} | {company}"

            bullets_html = "".join(f"<li>{b}</li>" for b in bullets)
            outcomes_html = ""
            if key_outcomes:
                outcomes_html = (
                    '<p class="outcome-label">Key Outcomes</p>'
                    + "<ul class=\"key-outcomes\">"
                    + "".join(f"<li>{k}</li>" for k in key_outcomes)
                    + "</ul>"
                )

            exp_item = f'''<div class="exp-item">
            <div class="exp-header">
              <span class="exp-title">{exp_title}</span>
              <span class="exp-period">{period}</span>
            </div>
            {outcomes_html}
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


@app.route("/download-pdf", methods=["POST"])
def download_pdf():
    """HTML을 받아 playwright로 PDF 생성 후 career-ops 폴더에 저장 + 다운로드"""
    payload = request.json or {}
    html = payload.get("html")
    filename = (payload.get("filename") or "resume").strip() or "resume"
    wants_json = bool(payload.get("json_response"))

    if not html:
        return jsonify({"error": "HTML 콘텐츠가 제공되어야 합니다."}), 400
    html = strip_pasted_font_styles(html)

    # 임시 HTML을 파일 URL로 열어 로컬 폰트/상대 경로를 해소한다.
    safe_stem = re.sub(r"[^\w\-]", "-", filename)
    tmp_html = Path(tempfile.gettempdir()) / f"resume_pdf_{safe_stem}_{int(time.time())}.html"
    page = None
    try:
        tmp_html.write_text(html, encoding="utf-8")
        load_url = tmp_html.as_uri()

        browser = get_pdf_browser()
        page = browser.new_page()
        page.set_default_timeout(30_000)
        page.goto(load_url, wait_until="domcontentloaded", timeout=30_000)
        page.emulate_media(media="print")
        pdf_bytes = page.pdf(
            format="A4",
            margin={"top": "18mm", "bottom": "18mm", "left": "18mm", "right": "18mm"},
            print_background=True,
        )
    except Exception as exc:
        log.error("PDF 생성 실패", exc_info=exc)
        return jsonify({"error": f"PDF 생성 중 오류가 발생했습니다: {exc}"}), 500
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass
        if tmp_html and tmp_html.exists():
            tmp_html.unlink(missing_ok=True)

    pdf_path = None
    if CAREER_OPS_HTML_DIR.exists():
        pdf_path = CAREER_OPS_HTML_DIR / f"{safe_stem}.pdf"
        pdf_path.write_bytes(pdf_bytes)
        log.info(f"[pdf] saved → {pdf_path.name}")

    if wants_json:
        if not pdf_path:
            return jsonify({"error": "PDF 저장 디렉토리가 없습니다."}), 500
        return jsonify({"ok": True, "filename": pdf_path.name, "pdf_url": f"/career-ops/pdf/{pdf_path.name}"})

    buffer = BytesIO(pdf_bytes)
    buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name=f"{filename}.pdf", mimetype="application/pdf")


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
    career_ops_file = body.get("career_ops_file", "").strip()
    jd = body.get("jd_text", "").strip()
    direction = (body.get("direction") or "").strip()
    preserve_current = body.get("preserve_current", False)
    llm_mode = (body.get("llm_mode") or LLM_MODE or "ats").strip().lower()
    if llm_mode not in {"ats", "career_ops"}:
        llm_mode = LLM_MODE if LLM_MODE in {"ats", "career_ops"} else "ats"

    # [1] 베이스 이력서 로드 (career-ops HTML 우선, 없으면 JSON 샘플 fallback)
    raw_text = ""
    sample = {}
    if career_ops_file:
        path = CAREER_OPS_HTML_DIR / Path(career_ops_file).name
        if path.exists():
            html_content = path.read_text(encoding="utf-8")
            raw_text = _html_to_text(html_content)
            contact, location, availability = _extract_contact_from_html(html_content)
            sample = {"contact": contact, "location": location, "availability": availability}
            log.info(f"[1] career-ops HTML loaded — {career_ops_file}")
        elif preserve_current:
            # 현재 미리보기 HTML이 있으면 그걸 그대로 수정 대상으로 사용
            raw_text = body.get("current_html", "")
            sample = {"contact": {}, "location": "", "availability": ""}
            log.info(f"[1] using current preview HTML as base — {career_ops_file}")
        else:
            log.warning(f"[1] career-ops HTML missing, trying sample fallback — {career_ops_file}")
            if sample_name:
                try:
                    loaded = load_sample(sample_name)
                    if not loaded:
                        return jsonify({"error": f"'{sample_name}' 샘플을 찾을 수 없습니다"}), 404
                    sample = loaded
                except Exception as exc:
                    log.error(f"[1] Sample load failed ({sample_name})", exc_info=exc)
                    return jsonify({"error": "샘플 로드 중 오류가 발생했습니다"}), 500
                log.info(f"[1] JSON sample loaded — {sample_name}")
            else:
                return jsonify({"error": "베이스 이력서를 선택해주세요"}), 400
    elif sample_name:
        try:
            loaded = load_sample(sample_name)
            if not loaded:
                return jsonify({"error": f"'{sample_name}' 샘플을 찾을 수 없습니다"}), 404
            sample = loaded
        except Exception as exc:
            log.error(f"[1] Sample load failed ({sample_name})", exc_info=exc)
            return jsonify({"error": "샘플 로드 중 오류가 발생했습니다"}), 500
        log.info(f"[1] JSON sample loaded — {sample_name}")
    else:
        return jsonify({"error": "베이스 이력서를 선택해주세요"}), 400

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
    jd_keywords = extract_jd_keywords(jd)
    preferred_title = extract_role_label(jd)

    # [4] LLM으로 베이스 이력서 + JD → 재작성
    import httpx

    try:
        if career_ops_file:
            # career-ops HTML 포맷 유지: HTML → HTML 리라이트
            html = rewrite_html_for_jd(html_content, jd, jd_keywords, preferred_title, direction, llm_mode)
            log.info("[4] HTML rewrite done (format preserved)")
        else:
            rewritten = rewrite_for_jd(raw_text, sample, jd, jd_keywords, preferred_title, llm_mode)
            # [4.5] 샘플의 연락처 정보 병합
            rewritten["contact"] = sample.get("contact", {})
            rewritten["location"] = sample.get("location", "")
            rewritten["availability"] = sample.get("availability", "")
            # [5] JSON → HTML 생성
            html = json_to_html(rewritten)
            log.info("[5] HTML generated from JSON")
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

    # [6] HTML 저장 (브라우저에서 print-to-PDF 사용)
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{company}_{date_str}"

    html_path = CAREER_OPS_HTML_DIR / f"{filename}.html"
    html_path.write_text(html, encoding="utf-8")

    log.info(f"[6] HTML saved — career-ops/{filename}.html")

    return jsonify({
        "ok": True,
        "html": html,
        "filename": filename,
        "saved_filename": html_path.name,
        "pdf_path": f"{filename}.pdf",
        "sample": sample_name,
        "llm_mode": llm_mode,
    })


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


def _career_ops_sorted() -> list[dict]:
    """career-ops HTML 파일 목록을 수정시간 기준 내림차순으로 반환"""
    if not CAREER_OPS_HTML_DIR.exists():
        return []
    date_re = re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})")
    date_re_compact = re.compile(r"(\d{4})(\d{2})(\d{2})")
    items = []
    for p in CAREER_OPS_HTML_DIR.glob("*.html"):
        if p.name.startswith("_tmp_"):
            continue
        stat = p.stat()
        m = date_re.search(p.stem) or date_re_compact.search(p.stem)
        if m:
            date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            display_name = date_re.sub("", p.stem)
            display_name = date_re_compact.sub("", display_name)
        else:
            dt = datetime.fromtimestamp(stat.st_mtime)
            date_str = dt.strftime("%Y-%m-%d")
            display_name = p.stem
        display_name = display_name.strip("-").strip("_").strip()
        mtime = datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M")
        items.append({"filename": p.name, "mtime": stat.st_mtime, "display": f"[{date_str} {mtime}] {display_name}"})
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return [{k: v for k, v in item.items() if k != "mtime"} for item in items]


def _html_to_text(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def _extract_contact_from_html(html: str) -> tuple[dict, str, str]:
    """(contact dict, location, availability) 추출"""
    contact = {}
    linkedin_m = re.search(r'href="https://linkedin\.com/in/([^"]+)"', html)
    if linkedin_m:
        contact["linkedin"] = linkedin_m.group(1)
    email_m = re.search(r'[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}', html)
    if email_m:
        contact["email"] = email_m.group(0)
    phone_m = re.search(r'\+[\d\s\-()]{7,15}', html)
    if phone_m:
        contact["phone"] = phone_m.group(0).strip()
    loc_m = re.search(r'Based in ([^<\n]+)', html)
    location = loc_m.group(1).strip() if loc_m else ""
    availability = "Available to start immediately" if "immediately" in html else ""
    return contact, location, availability


@app.route("/career-ops/list")
def career_ops_list():
    """career-ops HTML 파일 목록 (날짜 내림차순)"""
    return jsonify(_career_ops_sorted())


@app.route("/career-ops/open/<path:filename>")
def career_ops_open(filename):
    """career-ops HTML 파일 내용 반환"""
    path = CAREER_OPS_HTML_DIR / Path(filename).name  # path traversal 방지
    if not path.exists() or path.suffix != ".html":
        return jsonify({"error": "파일을 찾을 수 없습니다"}), 404
    html = path.read_text(encoding="utf-8")
    return jsonify({"html": html, "filename": path.stem})


@app.route("/career-ops/pdf/<path:filename>")
def career_ops_pdf(filename):
    """저장된 career-ops PDF 다운로드"""
    safe_name = Path(filename).name
    path = CAREER_OPS_HTML_DIR / safe_name
    if not path.exists() or path.suffix.lower() != ".pdf":
        return jsonify({"error": "PDF 파일을 찾을 수 없습니다"}), 404
    return send_file(path, as_attachment=True, download_name=safe_name, mimetype="application/pdf")


@app.route("/career-ops/save", methods=["POST"])
def career_ops_save():
    """편집된 HTML을 career-ops output 디렉토리에 저장"""
    payload = request.json or {}
    html = payload.get("html", "").strip()
    name = (payload.get("filename") or "").strip()

    if not html:
        return jsonify({"error": "HTML 내용이 없습니다"}), 400
    if not name:
        return jsonify({"error": "파일명을 입력해주세요"}), 400
    html = strip_pasted_font_styles(html)

    # 확장자 / 경로 정규화
    safe_name = re.sub(r"[^\w\-.]", "-", Path(name).stem) + ".html"
    dest = CAREER_OPS_HTML_DIR / safe_name

    if not CAREER_OPS_HTML_DIR.exists():
        return jsonify({"error": "career-ops output 디렉토리가 없습니다"}), 500

    dest.write_text(html, encoding="utf-8")
    os.utime(dest, None)
    log.info(f"[career-ops] saved → {safe_name}")
    return jsonify({"ok": True, "filename": safe_name})


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(error):
    return jsonify({"error": "PDF 파일은 16MB 이하로 업로드해 주세요."}), 413


if __name__ == "__main__":
    debug_enabled = os.getenv("FLASK_DEBUG", "").lower() in {"1", "true", "yes", "on"}
    threading.Thread(target=warm_pdf_browser, daemon=True).start()
    app.run(
        host=os.getenv("FLASK_HOST", "127.0.0.1"),
        port=int(os.getenv("FLASK_PORT", "8080")),
        debug=debug_enabled,
        use_reloader=False,
        threaded=False,
    )
