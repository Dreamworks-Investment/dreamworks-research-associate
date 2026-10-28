from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from urllib.parse import quote, urlparse


# =============================================================================
# App
# =============================================================================

app = FastAPI(title="MGC Modern Deep Value Report MVP", version="2026-04-27-forced-two-connectors")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MANUS_FILE_ALLOWED_HOSTS = {
    "private-us-east-1.manuscdn.com",
    "private-us-west-2.manuscdn.com",
    "private-ap-southeast-1.manuscdn.com",
}

# =============================================================================
# Authentication Gate
# =============================================================================
# Fail closed: if auth is enabled but credentials/secret are missing, startup must
# fail instead of silently exposing the research agent.
try:
    try:
        from .auth_gate import install_auth
    except ImportError:
        from auth_gate import install_auth

    install_auth(app)
    print("[startup] MGC auth gate installed")
except Exception as exc:
    print(f"[startup][fatal] MGC auth gate failed: {type(exc).__name__}: {exc}")
    raise

# =============================================================================
# Optional route installers
# =============================================================================
ALPHA_PAI_ROUTES_INSTALLED = False
ALPHA_PAI_ROUTES_ERROR: Optional[str] = None

# Alpha Pai Agent / Skill API 底稿 routes:
#   POST /api/alpha-pai/jobs
#   GET  /api/alpha-pai/jobs/{job_id}
#   POST /api/alpha-pai/pack
try:
    try:
        from .alpha_pai_routes import install_alpha_pai_routes
    except ImportError:
        from alpha_pai_routes import install_alpha_pai_routes

    install_alpha_pai_routes(app)
    ALPHA_PAI_ROUTES_INSTALLED = True
    ALPHA_PAI_ROUTES_ERROR = None
    print("[startup] Alpha Pai Agent / Skill API 底稿 routes installed")
except Exception as exc:
    ALPHA_PAI_ROUTES_INSTALLED = False
    ALPHA_PAI_ROUTES_ERROR = f"{type(exc).__name__}: {exc}"
    print(f"[startup][warning] Alpha Pai Agent / Skill API 底稿 routes not installed: {ALPHA_PAI_ROUTES_ERROR}")


ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
FRONTEND_STATIC_DIR = FRONTEND_DIR / "static"
EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "/tmp/mgc_exports"))
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

# File-backed session recovery store.
# This is intentionally separate from the auth token: users must re-authenticate,
# then provide the opaque Session ID to recover a running Manus task.
SESSION_STORE_DIR = Path(os.getenv("MGC_SESSION_STORE_DIR", str(EXPORT_DIR / "sessions")))
SESSION_STORE_DIR.mkdir(parents=True, exist_ok=True)
SESSION_ID_PREFIX = "mgcs_"

# Durable internal research-task store.
# MVP note: this is still file-backed, but it is no longer tied to the temporary
# auth token or to a browser session. Mount EXPORT_DIR or MGC_TASK_STORE_DIR on
# persistent disk in production. A later version can swap this for SQLite/Postgres
# without changing the frontend contract.
TASK_STORE_DIR = Path(os.getenv("MGC_TASK_STORE_DIR", str(EXPORT_DIR / "tasks")))
TASK_STORE_DIR.mkdir(parents=True, exist_ok=True)
TASK_ID_PREFIX = "mgct_"

if EXPORT_DIR.exists():
    app.mount("/exports", StaticFiles(directory=str(EXPORT_DIR)), name="exports")

if FRONTEND_STATIC_DIR.exists():
    app.mount(
        "/static",
        StaticFiles(directory=str(FRONTEND_STATIC_DIR)),
        name="frontend_static",
    )


# =============================================================================
# Env
# =============================================================================

MANUS_API_BASE = os.getenv("MANUS_API_BASE", "https://api.manus.ai/v2").rstrip("/")
MANUS_API_KEY = os.getenv("MANUS_API_KEY", "").strip()
MANUS_AGENT_PROFILE = os.getenv("MANUS_AGENT_PROFILE", "manus-1.6-max").strip()

# Manus text parts are limited to ~5,000 estimated tokens. Large prompts,
# ComeIn packs, Alpha Pai packs, and documented template rules must travel as
# file attachments instead of being pasted into message.content text.
MANUS_HEADER_PROMPT_MAX_CHARS = int(os.getenv("MANUS_HEADER_PROMPT_MAX_CHARS", "4200"))
MANUS_ATTACHMENT_MIN_CHARS = int(os.getenv("MANUS_ATTACHMENT_MIN_CHARS", "1"))
MANUS_FILE_UPLOAD_READY_TIMEOUT_SECONDS = float(os.getenv("MANUS_FILE_UPLOAD_READY_TIMEOUT_SECONDS", "15"))
MANUS_EXTENDED_CONTEXT_FILENAME_PREFIX = os.getenv(
    "MANUS_EXTENDED_CONTEXT_FILENAME_PREFIX",
    "mgc_manus_extended_context",
).strip() or "mgc_manus_extended_context"

# =============================================================================
# Connectors Selection Feature
# =============================================================================
# New behavior:
# - The backend no longer hard-forces two fixed connector IDs.
# - The frontend may list every installed Manus connector.
# - The user can select A only, B only, both A+B, or any other available connector.
# - The backend injects exactly the connector IDs selected by the user.
# - If connector usage is disabled, no message.connectors field is sent.

def _split_connector_ids(value: str) -> List[str]:
    ids: List[str] = []
    for item in (value or "").replace("\n", ",").split(","):
        cid = item.strip()
        if cid and cid not in ids:
            ids.append(cid)
    return ids


COMEIN_MCP_TEST_1_CONNECTOR_ID = "793ed313-d5b6-44b2-9181-084d8f73b266"
ALPHA_PAI_G5_RECALL_CONNECTOR_ID = "4ba61ce5-9f6e-459b-a317-10f39507dacc"
Yaozh_MCP_HTTP_Test_0_CONNECTOR_ID = "81d57dbd-044f-451f-857c-abb7ec718166"
PHARMCUBE_MCP_TEST_0_CONNECTOR_ID = "59c79bb7-e1ab-4ba8-8ce3-0520ee0b2f8e"
ALPHA_PAI_LAST_CHANCE_CONNECTOR_ID = "dac71788-be3d-4c95-9737-c22432ba190a"
ALPHA_PAI_SKILL_API_MGC_TRIAL_CONNECTOR_ID = "f44322f5-09d3-4074-b2e7-120994c70a2a"

KNOWN_CONNECTOR_LABELS = {
    COMEIN_MCP_TEST_1_CONNECTOR_ID: "ComeIn MCP - Test 1",
    ALPHA_PAI_G5_RECALL_CONNECTOR_ID: "Alpha Pai G5 Recall - MGC",
    Yaozh_MCP_HTTP_Test_0_CONNECTOR_ID: "Yaozh MCP HTTP Test 0",
    PHARMCUBE_MCP_TEST_0_CONNECTOR_ID: "PharmCube MCP - Test 0",
    ALPHA_PAI_LAST_CHANCE_CONNECTOR_ID: "Alpha派_Last_Chance",
    ALPHA_PAI_SKILL_API_MGC_TRIAL_CONNECTOR_ID: "Alpha Pai Skill API - MGC Trial",
    "b1809ea8-5847-4492-8e35-46482e2c119e": "ComeIn Agent API Test 0",
    "b663734c-db59-4b8f-ae13-50064d83002a": "ComeIn Agent API",
}

RECOMMENDED_DEFAULT_CONNECTOR_IDS = _split_connector_ids(
    os.getenv(
        "MANUS_RECOMMENDED_DEFAULT_CONNECTOR_IDS",
        f"{COMEIN_MCP_TEST_1_CONNECTOR_ID},{ALPHA_PAI_G5_RECALL_CONNECTOR_ID}",
    )
)

# Backward-compatible alias. Do not treat this as a hard allowlist.
DEFAULT_REPORT_CONNECTOR_IDS = RECOMMENDED_DEFAULT_CONNECTOR_IDS
MANUS_DEFAULT_CONNECTOR_IDS = RECOMMENDED_DEFAULT_CONNECTOR_IDS

COMEIN_MODEL = os.getenv("COMEIN_MODEL", "DEEPSEEK-NOTHINK").strip()


# =============================================================================
# Models
# =============================================================================

class ComeInJobRequest(BaseModel):
    ability: str = Field(
        ...,
        description=(
            "chain_overview | chart_search_phase1 | chart_search_phase2 | "
            "company_overview | financial_search | industry_sheet_paper | "
            "performance_review | relative_valuation | seller_deep_research_report | "
            "enterprise_chart_view | viewpoint_compare | stock_review_phase1 | stock_review_phase2"
        ),
    )

    stock_name: str
    stock_code: str
    max_wait_seconds: int = 180

    # Generic override
    query: Optional[str] = None
    model: Optional[str] = None

    # Common / company params
    industry_text: Optional[str] = None
    focus_point: Optional[str] = None

    # Performance review
    fiscal_year: Optional[int] = None
    report_type: Optional[str] = None
    focus_text: Optional[str] = None
    compact: Optional[bool] = None
    release_ids: Optional[List[int]] = None

    # Seller report
    length_requirement: Optional[str] = None
    role_name: Optional[str] = None

    # Time-window agents
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    date_text: Optional[str] = None

    # Chart phase 2
    record_id: Optional[str] = None
    chunk_text: Optional[str] = None
    chunk_ids: Optional[List[str]] = None

    # Stock review
    auto_detect_trend: Optional[str] = None
    frequency_type: Optional[str] = None
    trend_result_list: Optional[List[Dict[str, Any]]] = None

    # Escape hatch for newly discovered provider fields
    extra_agent_params: Optional[Dict[str, Any]] = None





class ChatRequest(BaseModel):
    message: str
    stock_name: Optional[str] = None
    stock_code: Optional[str] = None
    provider: str = "manus"
    manus_skill_ids: Optional[list[str]] = None

    # Long context that should not count against Manus message.content text limit.
    # Frontend sends the short header prompt in `message`, and sends the long
    # template/pack material here so the backend uploads it as a .txt file and
    # attaches it by file_id.
    documented_prompt: Optional[str] = None
    comein_pack: Optional[str] = None
    alpha_pai_pack: Optional[str] = None
    # Optional attachment layout selector. Keep innovative-drug behavior unchanged
    # while allowing the general DreamWorks flow to use a cleaner two-layer file.
    attachment_layout: Optional[str] = None

    # Accept both old and new frontend field names.
    use_manus_connectors: bool = True
    use_connectors: Optional[bool] = None
    connector_ids: Optional[List[str]] = None
    connectors: Optional[List[str]] = None

    include_connector_instruction: bool = True
    locale: str = "zh-CN"
    title: Optional[str] = None
    session_id: Optional[str] = None


class ConnectorSmokeTestRequest(BaseModel):
    stock_name: str = "宁德时代"
    stock_code: str = "300750.SZ"
    connector_ids: Optional[List[str]] = None
    connectors: Optional[List[str]] = None
    locale: str = "zh-CN"


class ExportRequest(BaseModel):
    report_markdown: str
    format: str = "md"


class SessionCreateRequest(BaseModel):
    title: Optional[str] = None


class SessionUpdateRequest(BaseModel):
    # Internal durable task ID, e.g. mgct_xxx. This links the UI session to the
    # permanent research-task record.
    mgc_task_id: Optional[str] = None

    # External provider task ID, e.g. Manus task_id.
    task_id: Optional[str] = None
    task_url: Optional[str] = None
    share_url: Optional[str] = None
    stock_name: Optional[str] = None
    stock_code: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    latest_text: Optional[str] = None
    report_ready: Optional[bool] = None
    connector_ids: Optional[List[str]] = None
    active_tab: Optional[str] = None


class TaskPatchRequest(BaseModel):
    session_id: Optional[str] = None
    provider: Optional[str] = None
    provider_task_id: Optional[str] = None
    task_url: Optional[str] = None
    share_url: Optional[str] = None
    stock_name: Optional[str] = None
    stock_code: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    latest_text: Optional[str] = None
    report_markdown: Optional[str] = None
    report_ready: Optional[bool] = None
    connector_ids: Optional[List[str]] = None
    messages: Optional[List[Dict[str, Any]]] = None
    artifacts: Optional[List[Dict[str, Any]]] = None
    active_tab: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None


class ManusSendMessageRequest(BaseModel):
    message: str
    locale: str = "zh-CN"
    connectors: Optional[List[str]] = None
    enable_skills: Optional[List[str]] = None
    force_skills: Optional[List[str]] = None
    agent_profile: Optional[str] = None
    structured_output_schema: Optional[Dict[str, Any]] = None


class ReportQualityValidateRequest(BaseModel):
    report_markdown: str
    stock_name: Optional[str] = None
    stock_code: Optional[str] = None
    innovative_drug_mode: bool = False
    connector_ids: Optional[List[str]] = None
    previous_report_markdown: Optional[str] = None


class ReportQualityIssue(BaseModel):
    severity: str  # P0 | P1 | P2
    code: str
    message: str
    suggestion: str = ""


class ReportQualityValidateResponse(BaseModel):
    ok: bool
    grade: str
    issues: List[ReportQualityIssue]
    metrics: Dict[str, Any]
    repair_prompt: str


# =============================================================================
# In-memory job store for ComeIn Agent API jobs
# =============================================================================

COMEIN_JOBS: Dict[str, Dict[str, Any]] = {}


# =============================================================================
# Utilities
# =============================================================================

def now_ts() -> float:
    return time.time()


def normalize_full_code(stock_code: str) -> str:
    """
    Convert common code formats into ComeIn MCP / ComeIn Agent fullCode format:
      300750.SZ -> sz300750
      600309.SH -> sh600309
      sz300750 -> sz300750
      sh600309 -> sh600309
    """
    code = (stock_code or "").strip()
    if not code:
        return ""

    lower = code.lower()
    if re.fullmatch(r"(sz|sh)\d{6}", lower):
        return lower

    if re.fullmatch(r"hk\d{4,5}", lower):
        return lower

    upper = code.upper()
    if re.fullmatch(r"\d{6}\.SZ", upper):
        return "sz" + upper[:6]
    if re.fullmatch(r"\d{6}\.SH", upper):
        return "sh" + upper[:6]
    if re.fullmatch(r"\d{4,5}\.HK", upper):
        return "hk" + upper.split(".", 1)[0]

    if re.fullmatch(r"\d{6}", code):
        if code.startswith(("0", "3")):
            return "sz" + code
        if code.startswith("6"):
            return "sh" + code

    return code


def display_company_text(stock_name: str, stock_code: str) -> str:
    return f"{stock_name}({stock_code})" if stock_name and stock_code else stock_name or stock_code


def report_type_label(report_type: str) -> str:
    rt = (report_type or "").strip().upper()
    mapping = {
        "Q1": "一季报",
        "Q2": "中报",
        "H1": "中报",
        "S1": "中报",
        "Q3": "三季报",
        "A": "年报",
        "ANNUAL": "年报",
    }
    return mapping.get(rt, rt or "报告期")


def safe_json_preview(obj: Any, limit: int = 4000) -> str:
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = str(obj)
    return text[:limit]


def extract_answer_text(obj: Any) -> str:
    """
    Robust extractor for different provider/result shapes.
    """
    if obj is None:
        return ""

    if isinstance(obj, str):
        return obj.strip()

    if not isinstance(obj, dict):
        return str(obj).strip()

    direct_keys = [
        "answer_text",
        "answer",
        "text",
        "content",
        "message",
        "reply",
        "report_markdown",
        "markdown",
        "output",
    ]
    for key in direct_keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    for key in ["data", "result", "payload", "response"]:
        val = obj.get(key)
        if isinstance(val, dict):
            nested = extract_answer_text(val)
            if nested:
                return nested
        elif isinstance(val, str) and val.strip():
            return val.strip()

    # Some SSE formats put content in event lists.
    events = obj.get("events")
    if isinstance(events, list):
        parts: List[str] = []
        for event in events:
            if isinstance(event, dict):
                nested = extract_answer_text(event)
                if nested:
                    parts.append(nested)
        if parts:
            return "\n".join(parts).strip()

    return ""


def extract_thinking_text(obj: Any) -> str:
    if not isinstance(obj, dict):
        return ""

    for key in ["thinking_text", "thinking", "reasoning", "trace"]:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    result = obj.get("result")
    if isinstance(result, dict):
        return extract_thinking_text(result)

    return ""


def extract_meta(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        meta = obj.get("meta")
        if isinstance(meta, dict):
            return meta
    return {}


def _attachment_name(att: Dict[str, Any]) -> str:
    return str(
        att.get("name")
        or att.get("filename")
        or att.get("file_name")
        or att.get("title")
        or "artifact"
    )


def _attachment_source_url(att: Dict[str, Any]) -> str:
    return str(
        att.get("url")
        or att.get("download_url")
        or att.get("file_url")
        or att.get("href")
        or ""
    )


def _is_markdown_artifact(att: Dict[str, Any]) -> bool:
    name = _attachment_name(att).lower()
    content_type = str(att.get("content_type") or att.get("mime_type") or att.get("type") or "").lower()
    return (
        name.endswith(".md")
        or name.endswith(".markdown")
        or "markdown" in content_type
        or content_type == "text/plain"
    )


NON_FINAL_MARKDOWN_NAME_PATTERNS = [
    r"quality[_ -]?gate",
    r"self[_ -]?check",
    r"validation[_ -]?self[_ -]?check",
    r"repair[_ -]?prompt",
    r"repair[_ -]?plan",
    r"changelog",
    r"template[_ -]?contract",
    r"report[_ -]?quality[_ -]?gate",
    r"sample[_ -]?repair",
    r"connector[_ -]?ledger",
    r"fundamentals",
    r"key[_ -]?facts",
    r"part[1-9]",
    r"draft",
    r"summary",
]

NON_FINAL_MARKDOWN_CONTENT_PATTERNS = [
    r"^\s*#\s*质量门自检报告",
    r"^\s*#\s*Quality Gate Self-Check",
    r"##\s*检查清单与结果",
    r"##\s*修复计划",
    r"Repair Plan",
    r"FINAL TEMPLATE ACCEPTED",
    r"FINAL TEMPLATE NOT ACCEPTED",
    r"SKILL_CHANGELOG",
    r"TEMPLATE_CONTRACT",
    r"REPORT_QUALITY_GATE",
    r"VALIDATION_SELF_CHECK",
    r"SAMPLE_REPAIR_PROMPT",
    r"Connector Usage Ledger",
    r"MCP调用台账",
    r"数据源调用台账",
]

FINAL_REPORT_NAME_BONUS_PATTERNS = [
    r"final[_ -]?report",
    r"MGC[_ -]?Final[_ -]?Report",
    r"Deep[_ -]?Value[_ -]?Report",
    r"确定性狠值.*报告",
    r"创新药.*深度.*报告",
    r"深度投研报告",
]


def _looks_like_non_final_markdown_text(text: str, name: str = "") -> bool:
    body = (text or "").strip()
    fname = (name or "").lower()
    head = body[:3000]

    if any(re.search(p, fname, flags=re.IGNORECASE) for p in NON_FINAL_MARKDOWN_NAME_PATTERNS):
        return True

    bad_content = any(
        re.search(p, head, flags=re.IGNORECASE | re.MULTILINE)
        for p in NON_FINAL_MARKDOWN_CONTENT_PATTERNS
    )

    good_report_signal = any(
        re.search(p, body, flags=re.IGNORECASE | re.MULTILINE)
        for p in [
            r"第一章\s*[：:]",
            r"结论与狠值度摘要",
            r"第二章\s*[：:]",
            r"第三章\s*[：:]",
            r"Step\s*1\s*[：:].*中国流行病学",
            r"第五章\s*[：:].*红蓝军",
            r"第六章\s*[：:].*最终投资建议",
        ]
    )

    return bad_content and not good_report_signal


def _score_markdown_report_candidate(name: str, text: str) -> int:
    if _looks_like_non_final_markdown_text(text, name):
        return -100000

    score = 0
    fname = name or ""
    body = text or ""

    for p in FINAL_REPORT_NAME_BONUS_PATTERNS:
        if re.search(p, fname, flags=re.IGNORECASE):
            score += 10000

    # 正式报告结构信号
    for p in [
        r"第一章\s*[：:]",
        r"第二章\s*[：:]",
        r"第三章\s*[：:]",
        r"第四章\s*[：:]",
        r"第五章\s*[：:]",
        r"第六章\s*[：:]",
        r"Step\s*1\s*[：:].*中国流行病学",
        r"Step\s*7\s*[：:].*全球销售额",
    ]:
        if re.search(p, body, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
            score += 500

    # 长度加分，但不能让 QA 长文超过真正报告
    score += min(len(body), 80000) // 100

    return score


def _assert_safe_manus_file_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="Only https Manus file URLs are allowed.")
    if parsed.hostname not in MANUS_FILE_ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail=f"File host not allowed: {parsed.hostname}")


def _proxied_file_url(source_url: str, name: str) -> str:
    return f"/api/proxy-file?url={quote(source_url, safe='')}&name={quote(name, safe='')}"


def _get_first_str(obj: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _attachment_source_url(att: Dict[str, Any]) -> str:
    return _get_first_str(
        att,
        [
            "url",
            "download_url",
            "downloadUrl",
            "file_url",
            "fileUrl",
            "href",
            "source_url",
            "sourceUrl",
            "signed_url",
            "signedUrl",
        ],
    )


def _attachment_name(att: Dict[str, Any]) -> str:
    raw = _get_first_str(
        att,
        [
            "name",
            "filename",
            "file_name",
            "fileName",
            "title",
            "display_name",
            "displayName",
            "path",
        ],
    )
    if not raw:
        return "artifact"
    return raw.split("/")[-1] or "artifact"


def _collect_attachments_deep(obj: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(d: Dict[str, Any]) -> None:
        source_url = _attachment_source_url(d)
        if not source_url:
            return

        key = source_url
        if key in seen:
            return
        seen.add(key)

        found.append(_normalize_attachment(d))

    def walk(x: Any) -> None:
        if isinstance(x, list):
            for item in x:
                walk(item)
            return

        if not isinstance(x, dict):
            return

        add_candidate(x)

        for key in [
            "attachments",
            "files",
            "file",
            "artifact",
            "artifacts",
            "asset",
            "assets",
            "resource",
            "resources",
            "content",
            "data",
            "payload",
            "message",
            "result",
            "results",
            "metadata",
            "meta",
        ]:
            if key in x:
                walk(x.get(key))

        # Last-resort recursive scan because Manus message schemas vary.
        for val in x.values():
            if isinstance(val, (dict, list)):
                walk(val)

    walk(obj)
    return found


async def _fetch_manus_file_bytes(source_url: str) -> tuple[bytes, str]:
    _assert_safe_manus_file_url(source_url)

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        resp = await client.get(source_url)

    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=f"Manus file fetch failed: {resp.text[:500]}")

    content_type = resp.headers.get("content-type") or "application/octet-stream"
    return resp.content, content_type


async def _fetch_manus_markdown_text(source_url: str) -> str:
    raw, _content_type = await _fetch_manus_file_bytes(source_url)

    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except Exception:
            continue

    return raw.decode("utf-8", errors="replace")


def _normalize_attachment(att: Dict[str, Any]) -> Dict[str, Any]:
    name = _attachment_name(att)
    source_url = _attachment_source_url(att)
    out = dict(att)
    out["name"] = name
    out["source_url"] = source_url
    if source_url:
        out["download_url"] = _proxied_file_url(source_url, name)
    return out


INNOVATIVE_DRUG_ALLOWED_CONNECTOR_IDS = {
    Yaozh_MCP_HTTP_Test_0_CONNECTOR_ID,
    PHARMCUBE_MCP_TEST_0_CONNECTOR_ID,
}

PLACEHOLDER_PATTERNS = [
    r"____+",
    r"\bTODO\b",
    r"待补",
    r"未填",
    r"待填写",
    r"\[填入\]",
    r"\{\{.*?\}\}",
]

REQUIRED_INNOVATIVE_DRUG_MODULES = [
    ("conclusion", ["结论", "狠值度", "最终评级"]),
    ("pipeline_overview", ["管线全景", "Pipeline", "核心管线"]),
    ("step_analysis", ["Step 1", "Step1", "Step 2", "Step2", "Step 7", "Step7"]),
    ("grs", ["G/R/S", "增长率", "回报率", "可持续"]),
    ("rnpv", ["rNPV", "三情景", "中性估值", "内在价值"]),
    ("red_blue_pk", ["红蓝军", "PK"]),
    ("decision", ["五重", "买入滤网", "股票池", "卖出纪律", "后续验证"]),
]


def _rq_issue(severity: str, code: str, message: str, suggestion: str = "") -> ReportQualityIssue:
    return ReportQualityIssue(
        severity=severity,
        code=code,
        message=message,
        suggestion=suggestion,
    )


def _has_any(text: str, needles: List[str]) -> bool:
    return any(n in text for n in needles)


def _first_chars(text: str, n: int = 3500) -> str:
    return (text or "")[:n]


def _count_regex(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text or "", flags=re.IGNORECASE | re.MULTILINE))


def _extract_first_float_after(label: str, text: str) -> Optional[float]:
    # Handles: 狠值度：0.63 / 狠值度 = 1.02 / 狠值度 0.58
    pattern = rf"{re.escape(label)}[^\d\-]{{0,30}}(-?\d+(?:\.\d+)?)"
    m = re.search(pattern, text or "", flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


FORBIDDEN_FILLER_HEADINGS = [
    r"深度附加分析",
    r"中国创新药宏观政策与出海逻辑",
    r"Macro\s*&\s*Go[- ]Global\s*Strategy",
    r"附录与深度扩展分析",
    r"补充论述",
    r"行业纵深与竞争全景",
    r"补充深度分析",
    r"扩展论述",
    r"额外补充",
    r"为满足篇幅",
    r"重复论述",
    r"通用行业背景长文",
    r"非模板章节",
    r"补充材料一",
    r"补充材料二",
]

STEP_REQUIRED_FIELDS = {
    1: [
        r"mini[- ]?prompt|mini-prompt",
        r"AI内置公式",
        r"参数追踪表",
        r"计算过程",
        r"输出",
        r"深度分析.*Layer\s*2|Layer\s*2.*扩充|深度分析（Layer 2 扩充）",
    ],
    2: [
        r"临床数据解读表",
        r"有效性数据",
        r"安全性数据",
        r"关键风险点",
        r"综合判断",
        r"判断主要依据",
        r"PoS取值|PoS",
        r"深度分析.*Layer\s*2|Layer\s*2.*扩充|深度分析（Layer 2 扩充）",
    ],
    3: [
        r"同靶点在研管线竞争格局表",
        r"核心管线与竞品对比表",
        r"竞争地位判定",
        r"深度分析.*Layer\s*2|Layer\s*2.*扩充|深度分析（Layer 2 扩充）",
    ],
    4: [
        r"渗透率调整规则",
        r"峰值渗透率及达峰时间测算表",
        r"深度分析.*Layer\s*2|Layer\s*2.*扩充|深度分析（Layer 2 扩充）",
    ],
    5: [
        r"mini[- ]?prompt|mini-prompt",
        r"AI内置公式",
        r"中国区域销售预测表",
        r"参数追踪表",
        r"计算过程",
        r"中国峰值年销售额",
        r"深度分析.*Layer\s*2|Layer\s*2.*扩充|深度分析（Layer 2 扩充）",
    ],
    6: [
        r"海外权益类型",
        r"海外销售额",
        r"海外峰值净收入",
        r"深度分析.*Layer\s*2|Layer\s*2.*扩充|深度分析（Layer 2 扩充）",
    ],
    7: [
        r"AI内置公式",
        r"全球销售额汇总表",
        r"参数追踪表",
        r"计算过程",
        r"全球峰值年销售额",
        r"输出至rNPV模型|输出至\s*rNPV",
    ],
}

STEP_TITLE_PATTERNS = {
    1: r"Step\s*1[：:：].*?(中国流行病学|目标患者)",
    2: r"Step\s*2[：:：].*?临床数据解读",
    3: r"Step\s*3[：:：].*?竞争格局分析",
    4: r"Step\s*4[：:：].*?渗透率预测",
    5: r"Step\s*5[：:：].*?中国区域销售预测",
    6: r"Step\s*6[：:：].*?(海外销售预测|BD估值)",
    7: r"Step\s*7[：:：].*?全球销售额",
}

CHAPTER_3_1_START_PATTERNS = [
    r"3\.1\s*[：:：]?.*?(增长率|G).*?(管线|拆解)",
    r"3\.1\s*[：:：]?.*?管线",
    r"第三章.*?3\.1.*?管线",
]

CHAPTER_3_1_END_PATTERNS = [
    r"\n#{1,6}\s*3\.2\b",
    r"\n3\.2\s*[：:：]",
    r"\n#{1,6}\s*第四章",
    r"\n第四章",
]

MIN_CHAPTER_3_1_CHAR_RATIO = 0.35
MIN_STEP_FIELD_COVERAGE_RATIO = 0.95


def _find_first_match(text: str, patterns: List[str], start: int = 0) -> Optional[re.Match]:
    for pat in patterns:
        m = re.search(pat, text[start:], flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m:
            # Convert relative match to absolute-ish object by returning a lightweight tuple is annoying;
            # instead callers can use .start() plus start from a second search if needed.
            return m
    return None


def _extract_between_patterns(text: str, start_patterns: List[str], end_patterns: List[str]) -> str:
    body = text or ""
    start_pos = -1

    for pat in start_patterns:
        m = re.search(pat, body, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m:
            start_pos = m.start()
            break

    if start_pos < 0:
        return ""

    end_pos = len(body)
    tail = body[start_pos + 1:]

    for pat in end_patterns:
        m = re.search(pat, tail, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if m:
            end_pos = start_pos + 1 + m.start()
            break

    return body[start_pos:end_pos]


def _extract_chapter_3_1(md: str) -> str:
    return _extract_between_patterns(
        md,
        CHAPTER_3_1_START_PATTERNS,
        CHAPTER_3_1_END_PATTERNS,
    )


def _find_forbidden_filler_hits(md: str) -> List[str]:
    hits: List[str] = []
    for pat in FORBIDDEN_FILLER_HEADINGS:
        if re.search(pat, md or "", flags=re.IGNORECASE | re.MULTILINE):
            hits.append(pat)
    return hits


def _estimate_forbidden_filler_chars(md: str) -> int:
    """
    Conservative approximation: if forbidden filler headings exist, count from each heading
    until the next markdown heading. This does not need to be perfect; any hit is P0.
    """
    text = md or ""
    total = 0

    for pat in FORBIDDEN_FILLER_HEADINGS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE | re.MULTILINE):
            start = m.start()
            tail = text[start + 1:]
            next_heading = re.search(r"\n#{1,6}\s+|\n第[一二三四五六七八九十〇零0]+章", tail)
            end = start + 1 + next_heading.start() if next_heading else min(len(text), start + 3000)
            total += max(0, end - start)

    return total


def _extract_step_loops(chapter_3_1: str) -> List[str]:
    """
    A loop begins at each canonical Step 1 title. It ends before the next Step 1 title.
    This approximates one core pipeline loop.
    """
    text = chapter_3_1 or ""
    starts = [m.start() for m in re.finditer(STEP_TITLE_PATTERNS[1], text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)]

    loops: List[str] = []
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(text)
        loops.append(text[start:end])

    return loops


def _extract_step_window(loop_text: str, step_no: int) -> str:
    text = loop_text or ""
    start_pat = STEP_TITLE_PATTERNS[step_no]
    m = re.search(start_pat, text, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if not m:
        return ""

    start = m.start()
    end = len(text)

    for next_step in range(step_no + 1, 8):
        nm = re.search(STEP_TITLE_PATTERNS[next_step], text[m.end():], flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if nm:
            end = m.end() + nm.start()
            break

    return text[start:end]


def _check_step_field_coverage(chapter_3_1: str) -> Dict[str, Any]:
    loops = _extract_step_loops(chapter_3_1)
    required = 0
    fulfilled = 0
    missing_items: List[str] = []

    for loop_idx, loop in enumerate(loops, start=1):
        for step_no in range(1, 8):
            step_window = _extract_step_window(loop, step_no)

            if not step_window:
                # Count the step title itself as missing, plus all fields.
                required += 1 + len(STEP_REQUIRED_FIELDS[step_no])
                missing_items.append(f"PipelineLoop{loop_idx}.Step{step_no}.TITLE")
                for field_pat in STEP_REQUIRED_FIELDS[step_no]:
                    missing_items.append(f"PipelineLoop{loop_idx}.Step{step_no}.{field_pat}")
                continue

            required += 1
            fulfilled += 1

            for field_pat in STEP_REQUIRED_FIELDS[step_no]:
                required += 1
                if re.search(field_pat, step_window, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
                    fulfilled += 1
                else:
                    missing_items.append(f"PipelineLoop{loop_idx}.Step{step_no}.{field_pat}")

    ratio = (fulfilled / required) if required else 0.0

    return {
        "core_pipeline_count": len(loops),
        "step_loop_count": len(loops),
        "required_step_field_count": required,
        "fulfilled_step_field_count": fulfilled,
        "step_field_coverage_ratio": ratio,
        "missing_step_fields": missing_items[:80],
    }


def _build_repair_prompt(issues: List[ReportQualityIssue], stock_name: str, stock_code: str) -> str:
    issue_lines = "\n".join(
        f"{idx+1}. [{i.severity}/{i.code}] {i.message} 修复建议：{i.suggestion or '请按模板修复。'}"
        for idx, i in enumerate(issues)
    )
    return f"""请不要新建报告。请在当前 Manus task 内修复已经生成的 Markdown 报告。

研究对象：{stock_name or ''} {stock_code or ''}

【篇幅与深度修复要求】
- 完整 Markdown 正文必须 ≥ 40,000 字符；
- 预计导出页数必须 ≥ 50 页；
- 每条进入 Step1-7 的核心管线必须完整跑完 Step1-7；
- Step1 与 Step7 数量必须一致；
- 第五章红蓝军PK必须扩写为完整辩论章节，不少于25,000字符；
- 第五章必须包含 5.1 角色规则、5.2 观点对比、5.3 假设与估值PK、5.4 蓝军攻击、5.5 市场观点差异、5.6 结论与风险监控；
- 修复后返回完整Markdown，不要只返回摘要。

【v3.2 有效内容优先修复政策】
如果失败原因是字符数或页数不足，禁止新增非模板补充章节。
不得新增：
- 附录与深度扩展分析（补充论述）
- 行业纵深与竞争全景
- 补充深度分析
- 扩展论述
- 额外补充
- 为满足篇幅要求补充
- 重复论述
- 通用行业背景长文
- 非模板章节

必须只通过以下路径扩写：
1. 补齐第三章3.1每条核心管线的Step字段；
2. 补齐mini-prompt、AI内置公式、参数追踪表、计算过程、输出、Layer 2深度分析；
3. 补齐Step2临床数据表、Step3竞品表、Step4渗透率表、Step5中国销售预测表、Step7全球销售额表；
4. 补齐第四章rNPV参数和敏感性分析；
5. 补齐第五章红蓝军PK 5.1-5.6；
6. 补齐第六章卖出纪律和后续验证问题。

如果已经存在非模板补充章节，必须删除该章节，并将其中有效内容归并回正式模板章节。

修复原则：
1. 不要重新跑通用报告；
2. 不要新增未授权 connector；
3. 保留原报告中正确的数据、估值和表格；
4. 只修复以下质量校验失败项；
5. 修复后必须返回完整 Markdown 正文。

质量校验失败项：
{issue_lines}

强制规则：
- 红军必须代表看多方，蓝军必须代表看空/风险方；
- 必须保留 dreamworks-innovative-drug-report-xu-prefin-v3.3 的核心章节；
- 所有核心管线必须有 Step1-7，或给出明确的“未进入 Step1-7”豁免表；
- 封面、摘要、估值章、结论章中的当前市值、rNPV中性估值、狠值度、目标价、评级必须一致；
- 清除所有 TODO、____、待补、未填、[填入] 等占位符。
"""


def validate_report_quality_core(req: ReportQualityValidateRequest) -> Dict[str, Any]:
    md = req.report_markdown or ""
    first = _first_chars(md, 3500)
    issues: List[ReportQualityIssue] = []
    connector_ids = set(req.connector_ids or [])

    if _looks_like_non_final_markdown_text(md, "report_markdown"):
        issue = _rq_issue(
            "P0",
            "NON_FINAL_MARKDOWN_ARTIFACT_VALIDATED",
            "当前被验证的 Markdown 看起来是质量门自检、修复计划、changelog、contract、ledger 或摘要文件，不是最终研报正文。",
            "请加载真正的 final_report.md / 确定性狠值深度报告 Markdown 主文件后重新验证。",
        )

        return {
            "ok": False,
            "grade": "FAIL",
            "issues": [issue.model_dump() if hasattr(issue, "model_dump") else issue.dict()],
            "metrics": {
                "char_count": len(md),
                "estimated_pages": _estimate_pages_from_markdown(md),
                "non_final_artifact": True,
            },
            "repair_prompt": "当前验证对象不是最终研报正文。请先加载真正的 final_report.md 后再运行质量门。",
        }

    if _looks_like_non_final_markdown_text(md, "report_markdown"):
        issues.append(_rq_issue(
            "P0",
            "NON_FINAL_MARKDOWN_ARTIFACT_VALIDATED",
            "当前被验证的 Markdown 看起来是质量门自检、修复计划、changelog、contract、ledger 或摘要文件，不是最终研报正文。",
            "请加载真正的 final_report.md / 确定性狠值深度报告 Markdown 主文件后重新验证。",
        ))

    chapter_3_1 = _extract_chapter_3_1(md)
    off_template_filler_hits = _find_forbidden_filler_hits(md)
    off_template_filler_chars = _estimate_forbidden_filler_chars(md)
    step_coverage = _check_step_field_coverage(chapter_3_1)

    FORBIDDEN_MERGED_STEP_PATTERNS = [
        r"Step\s*1\s*[-–—]\s*7",
        r"Step\s*2\s*[-–—]\s*7",
        r"Step\s*3\s*[-–—]\s*7",
        r"Step\s*4\s*[-–—]\s*7",
        r"Step\s*5\s*[-–—]\s*7",
        r"Step\s*6\s*[-–—]\s*7",
        r"Step\s*[1-7]\s*[-–—]\s*[1-7]",
        r"Step\s*4\s*[-–—]\s*7[：:].*?(销售预测|汇总)",
    ]

    FORBIDDEN_LEDGER_IN_REPORT_PATTERNS = [
        r"第[〇零0]章",
        r"Connector Usage Ledger",
        r"connector_ledger",
        r"MCP调用台账",
        r"数据源调用台账",
        r"工具调用台账",
        r"实际调用过的 connector",
        r"已传入但未实际调用",
        r"connector / MCP / tool",
    ]

    red_blue_section = _extract_section_between(
        md,
        [
            r"第五章.*?(红蓝军|自以为非|外部验证)",
            r"红蓝军\s*PK",
            r"Red\s*vs\.?\s*Blue",
        ],
        [
            r"第六章",
            r"第七章",
            r"附录",
        ],
    )

    metrics: Dict[str, Any] = {
        "char_count": len(md),
        "estimated_pages": _estimate_pages_from_markdown(md),
        "table_count": md.count("|"),
        "step1_count": _count_regex(md, r"\bStep\s*1\b|Step1|步骤\s*1"),
        "step7_count": _count_regex(md, r"\bStep\s*7\b|Step7|步骤\s*7"),
        "rnpv_mentions": _count_regex(md, r"rNPV|内在价值|中性估值"),
        "red_blue_mentions": _count_regex(md, r"红蓝军|红军|蓝军"),
        "red_blue_pk_chars": len(red_blue_section),
        "red_blue_pk_table_rows": _count_markdown_table_rows(red_blue_section),
        "decisive_value_ratio": _extract_first_float_after("狠值度", md),
    }

    metrics.update({
        "chapter_3_1_chars": len(chapter_3_1),
        "chapter_3_1_char_ratio": (len(chapter_3_1) / len(md)) if md else 0.0,
        "off_template_filler_chars": off_template_filler_chars,
        "off_template_filler_hits": off_template_filler_hits,
        **step_coverage,
    })

    merged_step_hits = []
    for pat in FORBIDDEN_MERGED_STEP_PATTERNS:
        if re.search(pat, chapter_3_1 or "", flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
            merged_step_hits.append(pat)

    metrics["merged_step_hits"] = merged_step_hits

    ledger_in_report_hits = []
    for pat in FORBIDDEN_LEDGER_IN_REPORT_PATTERNS:
        if re.search(pat, md or "", flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
            ledger_in_report_hits.append(pat)

    metrics["ledger_in_report_hits"] = ledger_in_report_hits

    if req.innovative_drug_mode and len(md.strip()) < MIN_INNOVATIVE_DRUG_REPORT_CHARS:
        issues.append(_rq_issue(
            "P0",
            "REPORT_BELOW_40000_CHARS",
            f"创新药深度报告正文仅 {len(md.strip())} 字符，低于 40,000 字符硬标准。",
            "请扩写为完整机构级深度报告；逐章填满模板、补足Step1-7、rNPV、红蓝军PK、卖出纪律和附录。",
        ))

    if req.innovative_drug_mode and metrics["estimated_pages"] < MIN_INNOVATIVE_DRUG_ESTIMATED_PAGES:
        issues.append(_rq_issue(
            "P0",
            "REPORT_BELOW_50_PAGE_PROXY",
            f"估算页数约 {metrics['estimated_pages']} 页，低于 50 页硬标准。",
            "请参考 P112石药集团 深度版本扩写，目标为导出 PDF/DOCX 后不少于 50 页。",
        ))

    # Connector gate check.
    if req.innovative_drug_mode:
        unexpected = connector_ids - INNOVATIVE_DRUG_ALLOWED_CONNECTOR_IDS
        missing = INNOVATIVE_DRUG_ALLOWED_CONNECTOR_IDS - connector_ids
        if unexpected:
            issues.append(_rq_issue(
                "P0",
                "UNEXPECTED_CONNECTOR_IDS",
                f"创新药模式出现非授权 connector IDs: {sorted(unexpected)}",
                "创新药模式只允许 Yaozh + PharmCube。",
            ))
        if missing:
            issues.append(_rq_issue(
                "P0",
                "MISSING_BIOTECH_CONNECTOR_IDS",
                f"创新药模式缺少必要 connector IDs: {sorted(missing)}",
                "请确认前端传入 Yaozh + PharmCube 两个 connector。",
            ))

    # Placeholder scan.
    for pat in PLACEHOLDER_PATTERNS:
        if re.search(pat, md, flags=re.IGNORECASE):
            issues.append(_rq_issue(
                "P0",
                "PLACEHOLDER_FOUND",
                f"发现未清理占位符模式：{pat}",
                "替换为具体内容；如果条件未触发，写“未触发”或“不适用原因”。",
            ))
            break

    # Required modules.
    for code, needles in REQUIRED_INNOVATIVE_DRUG_MODULES:
        if req.innovative_drug_mode and not _has_any(md, needles):
            issues.append(_rq_issue(
                "P0",
                f"MISSING_MODULE_{code.upper()}",
                f"缺少必要模块：{code}",
                f"报告必须包含这些关键词之一：{', '.join(needles)}",
            ))

    if req.innovative_drug_mode and merged_step_hits:
        issues.append(_rq_issue(
            "P0",
            "MERGED_STEP_BLOCK_FOUND",
            f"第三章3.1发现合并Step写法：{', '.join(merged_step_hits[:3])}",
            "严禁写成 Step4-7、Step1-3、销售预测与汇总等合并段落。每条核心管线必须独立输出 Step1、Step2、Step3、Step4、Step5、Step6、Step7。",
        ))

    # Red/blue polarity check.
    inverted_patterns = [
        r"蓝军\s*[（(]?\s*看多",
        r"蓝军.*看多方",
        r"蓝军.*看涨",
        r"红军\s*[（(]?\s*看空",
        r"红军.*看空方",
        r"红军.*风险方",
        r"红军.*看跌",
    ]
    for pat in inverted_patterns:
        if re.search(pat, md, flags=re.IGNORECASE | re.DOTALL):
            issues.append(_rq_issue(
                "P0",
                "RED_BLUE_POLARITY_INVERTED",
                "发现红蓝军立场疑似反转：红军必须看多，蓝军必须看空/风险。",
                "将红军统一为看多方，将蓝军统一为看空/风险方，并重写裁决表。",
            ))
            break

    # Step depth check.
    if req.innovative_drug_mode and metrics["step1_count"] < 2:
        issues.append(_rq_issue(
            "P1",
            "INSUFFICIENT_STEP_ANALYSIS",
            f"Step1 出现次数偏少：{metrics['step1_count']}。",
            "每条核心管线应执行 Step1-7；若不适用，需增加管线范围桥接/豁免表。",
        ))
    if req.innovative_drug_mode and metrics["step7_count"] < 1:
        issues.append(_rq_issue(
            "P1",
            "MISSING_STEP7",
            "未发现 Step7 或管线级最终汇总。",
            "补齐核心管线 Step7：全球销售额/rNPV输入/红蓝军结论。",
        ))

    # Template drift warning.
    if req.innovative_drug_mode and re.search(r"第九章|九章|9\s*章", md):
        issues.append(_rq_issue(
            "P1",
            "POSSIBLE_GENERIC_9_CHAPTER_DRIFT",
            "报告疑似漂移为九章通用分析结构。",
            "确认是否严格服从 innovative-drug v2 skill 原始章节；不适用章节也应保留并说明。",
        ))

    if req.innovative_drug_mode:
        step1 = metrics["step1_count"]
        step7 = metrics["step7_count"]

        if step1 != step7:
            issues.append(_rq_issue(
                "P0",
                "STEP_LOOP_COUNT_MISMATCH",
                f"Step1 出现 {step1} 次，但 Step7 仅出现 {step7} 次，说明至少部分管线没有完整跑完 Step1-7。",
                "每条进入核心管线筛选表的管线，必须完整执行 Step1、Step2、Step3、Step4、Step5、Step6、Step7。",
            ))

        if step1 < 3:
            issues.append(_rq_issue(
                "P0",
                "TOO_FEW_CORE_PIPELINE_LOOPS",
                f"核心管线 Step1 循环仅 {step1} 条，低于完整创新药深研标准。",
                "除非是单管线公司，否则至少应选择3条以上核心管线进入Step1-7，或在管线范围桥接表中解释豁免。",
            ))

    RED_BLUE_PK_REQUIRED_PATTERNS = [
        (r"5\.1.*?(红蓝军角色|角色与规则|角色定义)", "缺少5.1红蓝军角色与规则"),
        (r"5\.2.*?(核心观点对比|观点对比表)", "缺少5.2红蓝军核心观点对比"),
        (r"5\.3.*?(关键假设|置信度|估值PK|估值\s*PK)", "缺少5.3关键假设置信度与估值PK"),
        (r"5\.4.*?(蓝军重点攻击|蓝军攻击|薄弱环节)", "缺少5.4蓝军重点攻击"),
        (r"5\.5.*?(主流市场观点|市场观点|外部观点|我们的差异)", "缺少5.5主流市场观点与我们的差异"),
        (r"5\.6.*?(PK结论|未解决分歧|风险监控)", "缺少5.6 PK结论与未解决问题"),
    ]

    if req.innovative_drug_mode:
        if not red_blue_section:
            issues.append(_rq_issue(
                "P0",
                "RED_BLUE_PK_SECTION_MISSING",
                "未识别到第五章红蓝军PK完整章节。",
                "必须按泡泡玛特级别展开红蓝军PK，包含角色、观点、估值、蓝军攻击、市场差异、未解决问题和风险监控。",
            ))
        else:
            if metrics["red_blue_pk_chars"] < MIN_RED_BLUE_PK_CHARS:
                issues.append(_rq_issue(
                    "P0",
                    "RED_BLUE_PK_TOO_SHORT",
                    f"红蓝军PK章节仅 {metrics['red_blue_pk_chars']} 字符，低于 {MIN_RED_BLUE_PK_CHARS} 字符标准。",
                    "请扩写为完整辩论章节，不得只给一张表或短结论。",
                ))

            if metrics["red_blue_pk_table_rows"] < MIN_RED_BLUE_PK_TABLE_ROWS:
                issues.append(_rq_issue(
                    "P1",
                    "RED_BLUE_PK_TABLE_TOO_LIGHT",
                    f"红蓝军PK章节表格行数仅 {metrics['red_blue_pk_table_rows']} 行，低于 {MIN_RED_BLUE_PK_TABLE_ROWS} 行参考标准。",
                    "至少补充商业模式/管线/临床/估值/财务/BD/风险/市场观点等多维表格。",
                ))

            for pat, msg in RED_BLUE_PK_REQUIRED_PATTERNS:
                if not re.search(pat, red_blue_section, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL):
                    issues.append(_rq_issue(
                        "P0",
                        "RED_BLUE_PK_SUBSECTION_MISSING",
                        msg,
                        "按泡泡玛特红蓝军PK结构补齐5.1-5.6。",
                    ))
                    break

    # v3.2: non-template filler section gate.
    if req.innovative_drug_mode and off_template_filler_hits:
        issues.append(_rq_issue(
            "P0",
            "OFF_TEMPLATE_FILLER_SECTION_FOUND",
            f"发现非模板扩写章节或凑字数标题：{', '.join(off_template_filler_hits[:5])}",
            "删除该非模板章节，并将有效信息归并回第三章3.1、第四章估值、第五章红蓝军或第六章后续验证。",
        ))

    # v3.2: third-chapter 3.1 existence and density.
    if req.innovative_drug_mode and not chapter_3_1.strip():
        issues.append(_rq_issue(
            "P0",
            "CHAPTER_3_1_MISSING",
            "未识别到第三章3.1管线拆解模块。",
            "必须按模板建立第三章3.1，并对进入核心筛选的管线逐条执行Step1-7。",
        ))

    if req.innovative_drug_mode and step_coverage["core_pipeline_count"] == 0:
        issues.append(_rq_issue(
            "P0",
            "NO_CORE_PIPELINE_STEP_LOOPS_FOUND",
            "第三章3.1中未识别到任何以“Step 1：中国流行病学与目标患者测算”开头的核心管线循环。",
            "必须为每条进入Step1-7的核心管线建立完整Step1-7循环。",
        ))

    if req.innovative_drug_mode and step_coverage["step_field_coverage_ratio"] < MIN_STEP_FIELD_COVERAGE_RATIO:
        issues.append(_rq_issue(
            "P0",
            "STEP_FIELD_COVERAGE_BELOW_95",
            f"第三章3.1 Step字段覆盖率仅 {step_coverage['step_field_coverage_ratio']:.1%}，低于95%硬标准。",
            "补齐每条核心管线每个Step的必填字段：mini-prompt、AI内置公式、参数追踪表、计算过程、输出、Layer 2等。",
        ))

    if req.innovative_drug_mode and metrics["chapter_3_1_char_ratio"] < MIN_CHAPTER_3_1_CHAR_RATIO:
        issues.append(_rq_issue(
            "P1",
            "CHAPTER_3_1_DENSITY_LOW",
            f"第三章3.1仅占报告正文 {metrics['chapter_3_1_char_ratio']:.1%}，低于35%参考标准。",
            "报告重心可能偏离管线拆解。优先扩写第三章3.1 Step字段、表格、参数追踪表和Layer 2深度分析。",
        ))

    if (
        req.innovative_drug_mode
        and metrics.get("red_blue_pk_chars", 0) > 25000
        and metrics["chapter_3_1_char_ratio"] < MIN_CHAPTER_3_1_CHAR_RATIO
    ):
        issues.append(_rq_issue(
            "P1",
            "PK_TOO_LARGE_RELATIVE_TO_PIPELINE_ANALYSIS",
            "红蓝军PK超过25,000字符，但第三章3.1占比低于35%，报告重心可能被PK挤占。",
            "保持PK深度，但应优先补足第三章3.1管线Step1-7有效内容。",
        ))

    if req.innovative_drug_mode:
        ok_order, missing_patterns = _check_ordered_patterns(md, DREAMWORKS_260612_REQUIRED_ORDER)

        if not ok_order:
            issues.append(_rq_issue(
                "P0",
                "DREAMWORKS_260612_TEMPLATE_ORDER_FAIL",
                f"报告未按《梦工场创新药AI投研框架-260612(2).docx》Level 1→Level 7顺序一对一填充，缺失/错序项数量：{len(missing_patterns)}。",
                "必须按第一章至第六章、2.5管线桥接、3.1 Step1-7、3.2R、3.3S、3.4评分、3.5现金流、第四章估值、第五章PK、第六章决策重排。",
            ))

    p0_count = sum(1 for i in issues if i.severity == "P0")
    p1_count = sum(1 for i in issues if i.severity == "P1")

    grade = "PASS"
    if p0_count:
        grade = "FAIL"
    elif p1_count:
        grade = "WARN"

    repair_prompt = _build_repair_prompt(issues, req.stock_name or "", req.stock_code or "")

    return {
        "ok": p0_count == 0,
        "grade": grade,
        "issues": [i.model_dump() if hasattr(i, "model_dump") else i.dict() for i in issues],
        "metrics": metrics,
        "repair_prompt": repair_prompt,
    }


def _estimate_pages_from_markdown(md: str) -> int:
    text_len = len((md or "").strip())
    if text_len <= 0:
        return 0
    return max(1, int((text_len + ESTIMATED_CHARS_PER_PAGE - 1) / ESTIMATED_CHARS_PER_PAGE))


def _extract_section_between(
    md: str,
    start_patterns: List[str],
    end_patterns: List[str],
) -> str:
    text = md or ""
    start_pos = -1

    for pat in start_patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            start_pos = m.start()
            break

    if start_pos < 0:
        return ""

    end_pos = len(text)
    tail = text[start_pos + 1:]

    for pat in end_patterns:
        m = re.search(pat, tail, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            end_pos = start_pos + 1 + m.start()
            break

    return text[start_pos:end_pos]


def _count_markdown_table_rows(section: str) -> int:
    count = 0
    for line in (section or "").splitlines():
        s = line.strip()
        if s.startswith("|") and s.endswith("|") and "---" not in s:
            count += 1
    return count
    

# =============================================================================
# Session recovery store
# =============================================================================

def _session_timestamp() -> float:
    return time.time()


def _request_user_email(request: Request) -> str:
    user = getattr(request.state, "user", None)
    email = (getattr(user, "email", "") or "").strip().lower()
    return email or "auth-disabled@local"


def _validate_session_id(session_id: str) -> str:
    sid = (session_id or "").strip()
    if not re.fullmatch(r"mgcs_[A-Za-z0-9_-]{24,96}", sid):
        raise HTTPException(status_code=400, detail="Invalid session ID format.")
    return sid


def _new_session_id() -> str:
    return SESSION_ID_PREFIX + secrets.token_urlsafe(24)


def _session_path(session_id: str) -> Path:
    sid = _validate_session_id(session_id)
    return SESSION_STORE_DIR / f"{sid}.json"


def _load_session(session_id: str) -> Dict[str, Any]:
    path = _session_path(session_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Session ID was not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Session file is unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Session file is invalid.")
    return data


def _save_session(data: Dict[str, Any]) -> Dict[str, Any]:
    sid = _validate_session_id(str(data.get("session_id") or ""))
    data["updated_at"] = _session_timestamp()
    path = _session_path(sid)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _assert_session_owner(data: Dict[str, Any], request: Request) -> None:
    owner = (data.get("owner_email") or "").strip().lower()
    current = _request_user_email(request)
    if owner and owner != current:
        raise HTTPException(status_code=403, detail="This session belongs to another authenticated user.")


def _public_session_view(data: Dict[str, Any]) -> Dict[str, Any]:
    allowed = {
        "session_id",
        "owner_email",
        "created_at",
        "updated_at",
        "title",
        "mgc_task_id",
        "task_id",
        "task_url",
        "share_url",
        "stock_name",
        "stock_code",
        "status",
        "latest_text",
        "report_ready",
        "connector_ids",
        "active_tab",
    }
    return {k: data.get(k) for k in allowed if k in data}


def _create_session_record(request: Request, title: Optional[str] = None) -> Dict[str, Any]:
    sid = _new_session_id()
    now = _session_timestamp()
    data: Dict[str, Any] = {
        "session_id": sid,
        "owner_email": _request_user_email(request),
        "created_at": now,
        "updated_at": now,
        "title": title or "Untitled research session",
        "mgc_task_id": None,
        "task_id": None,
        "task_url": None,
        "share_url": None,
        "stock_name": None,
        "stock_code": None,
        "status": "created",
        "latest_text": "Session created. No Manus task has been submitted yet.",
        "report_ready": False,
        "connector_ids": [],
        "active_tab": "final",
    }
    return _save_session(data)


def _patch_session_record(data: Dict[str, Any], patch: SessionUpdateRequest | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(patch, dict):
        raw = patch
    elif hasattr(patch, "model_dump"):
        raw = patch.model_dump(exclude_unset=True)
    else:
        raw = patch.dict(exclude_unset=True)
    mutable_keys = {
        "mgc_task_id",
        "task_id",
        "task_url",
        "share_url",
        "stock_name",
        "stock_code",
        "title",
        "status",
        "latest_text",
        "report_ready",
        "connector_ids",
        "active_tab",
    }
    for key in mutable_keys:
        if key in raw:
            data[key] = raw[key]
    return _save_session(data)


DREAMWORKS_260612_REQUIRED_ORDER = [
    r"第一章.*?(结论|狠值度|核心结论)",
    r"1\.1.*?(核心投资逻辑|一句话)",
    r"1\.2.*?(价值三要素)",
    r"1\.3.*?(估值区间|狠值度)",
    r"1\.4.*?(综合置信度)",
    r"1\.5.*?(最终评级)",
    r"1\.6.*?(关键风险)",

    r"第二章.*?(公司基本信息|产品管线全景图|公司基础)",
    r"2\.1.*?(发展历程|公司概况|公司基础)",
    r"2\.2.*?(股权结构|管理层|核心团队)",
    r"2\.3.*?(核心管理团队|财务|管理层)",
    r"2\.4.*?(财务数据|财务)",
    r"2\.5.*?(产品管线全景图|管线全景)",
    r"(核心管线筛选结果|管线范围桥接表)",

    r"第三章.*?(价值三要素|G/R/S|增长率|管线)",
    r"3\.1.*?(增长率|G).*?(管线|拆解)",
    r"Step\s*1|Step1|步骤\s*1",
    r"Step\s*7|Step7|步骤\s*7",
    r"3\.2.*?(回报率|R)",
    r"3\.3.*?(可持续|S)",
    r"3\.4.*?(评分|G/R/S)",
    r"3\.5.*?(未来现金流|现金流预测)",

    r"第四章.*?(估值|狠值度|rNPV)",
    r"(rNPV|管线估值)",
    r"(三情景|情景分析)",
    r"(狠值度|安全边际)",

    r"第五章.*?(红蓝军|自以为非|外部验证)",
    r"第六章.*?(最终投资建议|股票池|组合管理|卖出纪律|附录)",
]

def _check_ordered_patterns(text: str, patterns: List[str]) -> tuple[bool, List[str]]:
    pos = 0
    missing: List[str] = []

    for pat in patterns:
        m = re.search(pat, text[pos:], flags=re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if not m:
            missing.append(pat)
            continue
        pos += m.end()

    return len(missing) == 0, missing


# =============================================================================
# Durable internal research tasks
# =============================================================================

def _validate_task_id(mgc_task_id: str) -> str:
    tid = (mgc_task_id or "").strip()
    if not re.fullmatch(r"mgct_[A-Za-z0-9_-]{24,96}", tid):
        raise HTTPException(status_code=400, detail="Invalid MGC task ID format.")
    return tid


def _validate_provider_task_id(provider_task_id: str) -> str:
    # Provider IDs are external opaque IDs. Keep them path-safe because this ID may
    # appear in lookup URLs, but do not overfit to one vendor's exact format.
    pid = (provider_task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", pid):
        raise HTTPException(status_code=400, detail="Invalid provider task ID format.")
    return pid


def _new_task_id() -> str:
    return TASK_ID_PREFIX + secrets.token_urlsafe(24)


def _task_path(mgc_task_id: str) -> Path:
    tid = _validate_task_id(mgc_task_id)
    return TASK_STORE_DIR / f"{tid}.json"


def _load_task(mgc_task_id: str) -> Dict[str, Any]:
    path = _task_path(mgc_task_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="MGC task ID was not found.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Task file is unreadable: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Task file is invalid.")
    return data


def _save_task(data: Dict[str, Any]) -> Dict[str, Any]:
    tid = _validate_task_id(str(data.get("mgc_task_id") or ""))
    data["updated_at"] = _session_timestamp()
    path = _task_path(tid)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def _assert_task_owner(data: Dict[str, Any], request: Request) -> None:
    owner = (data.get("owner_email") or "").strip().lower()
    current = _request_user_email(request)
    if owner and owner != current:
        raise HTTPException(status_code=403, detail="This task belongs to another authenticated user.")


def _public_task_view(data: Dict[str, Any], include_payload: bool = True) -> Dict[str, Any]:
    allowed = {
        "mgc_task_id",
        "session_id",
        "owner_email",
        "created_at",
        "updated_at",
        "provider",
        "provider_task_id",
        "task_url",
        "share_url",
        "stock_name",
        "stock_code",
        "title",
        "status",
        "latest_text",
        "report_ready",
        "connector_ids",
        "active_tab",
        "prompt_chars",
        "request_id",
        "completed_at",
        "meta",
    }
    if include_payload:
        allowed.update({"report_markdown", "messages", "artifacts"})
    return {k: data.get(k) for k in allowed if k in data}


def _create_task_record(
    request: Request,
    *,
    provider_task_id: str,
    session_id: Optional[str] = None,
    task_url: Optional[str] = None,
    share_url: Optional[str] = None,
    stock_name: Optional[str] = None,
    stock_code: Optional[str] = None,
    title: Optional[str] = None,
    status: str = "submitted",
    latest_text: str = "",
    connector_ids: Optional[List[str]] = None,
    prompt_chars: Optional[int] = None,
    request_id: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = _session_timestamp()
    data: Dict[str, Any] = {
        "mgc_task_id": _new_task_id(),
        "session_id": session_id,
        "owner_email": _request_user_email(request),
        "created_at": now,
        "updated_at": now,
        "provider": "manus",
        "provider_task_id": provider_task_id,
        "task_url": task_url,
        "share_url": share_url,
        "stock_name": stock_name,
        "stock_code": stock_code,
        "title": title or f"MGC research task - {stock_name or stock_code or provider_task_id}",
        "status": status,
        "latest_text": latest_text or "Task submitted.",
        "report_markdown": "",
        "report_ready": False,
        "connector_ids": connector_ids or [],
        "messages": [],
        "artifacts": [],
        "active_tab": "final",
        "prompt_chars": prompt_chars,
        "request_id": request_id,
        "completed_at": None,
        "meta": meta or {},
    }
    return _save_task(data)


def _patch_task_record(data: Dict[str, Any], patch: TaskPatchRequest | Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(patch, dict):
        raw = patch
    elif hasattr(patch, "model_dump"):
        raw = patch.model_dump(exclude_unset=True)
    else:
        raw = patch.dict(exclude_unset=True)

    mutable_keys = {
        "session_id",
        "provider",
        "provider_task_id",
        "task_url",
        "share_url",
        "stock_name",
        "stock_code",
        "title",
        "status",
        "latest_text",
        "report_markdown",
        "report_ready",
        "connector_ids",
        "messages",
        "artifacts",
        "active_tab",
        "meta",
    }

    for key in mutable_keys:
        if key not in raw:
            continue

        value = raw[key]

        # Keep the JSON file small enough for MVP file storage.
        if key == "messages" and isinstance(value, list):
            value = value[-200:]
        elif key == "artifacts" and isinstance(value, list):
            value = value[-100:]

        data[key] = value

    status_text = str(data.get("status") or "").lower()
    report_ready = bool(data.get("report_ready")) or bool((data.get("report_markdown") or "").strip())
    if report_ready:
        data["report_ready"] = True
    if not data.get("completed_at") and (
        report_ready or any(x in status_text for x in ["finished", "completed", "complete", "success", "done", "failed", "error"])
    ):
        data["completed_at"] = _session_timestamp()

    return _save_task(data)


def _iter_task_records() -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for path in TASK_STORE_DIR.glob(f"{TASK_ID_PREFIX}*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(item, dict):
            records.append(item)
    records.sort(key=lambda x: float(x.get("updated_at") or x.get("created_at") or 0), reverse=True)
    return records


def _list_tasks_for_owner(request: Request, limit: int = 20) -> List[Dict[str, Any]]:
    owner = _request_user_email(request)
    out: List[Dict[str, Any]] = []
    for item in _iter_task_records():
        if (item.get("owner_email") or "").strip().lower() != owner:
            continue
        out.append(_public_task_view(item, include_payload=False))
        if len(out) >= limit:
            break
    return out


def _find_task_by_provider_task_id(provider_task_id: str) -> Optional[Dict[str, Any]]:
    pid = _validate_provider_task_id(provider_task_id)
    for item in _iter_task_records():
        if str(item.get("provider_task_id") or "") == pid:
            return item
    return None


def _patch_task_by_provider_id(provider_task_id: str, request: Request, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    task = _find_task_by_provider_task_id(provider_task_id)
    if not task:
        return None
    _assert_task_owner(task, request)
    return _patch_task_record(task, patch)




# =============================================================================
# ComeIn Agent API provider adapter
# =============================================================================

_PROVIDER_SINGLETON: Any = None


def get_comein_provider() -> Any:
    """
    Uses your existing backend/providers/comein_agent_provider.py.

    Expected class:
      ComeInAgentProvider(appid, phone, public_key, private_key, token_url, chat_url, ...)
    Expected usable method:
      _post_agent(payload, hard_deadline_seconds=..., line_wait_seconds=...)
    The adapter below is intentionally tolerant so your existing provider does not need to be renamed.
    """
    global _PROVIDER_SINGLETON
    if _PROVIDER_SINGLETON is not None:
        return _PROVIDER_SINGLETON

    try:
        try:
            from backend.providers.comein_agent_provider import ComeInAgentProvider
        except Exception:
            from providers.comein_agent_provider import ComeInAgentProvider
    except Exception as exc:
        raise RuntimeError(
            "Cannot import ComeInAgentProvider from backend/providers/comein_agent_provider.py. "
            "Keep that file in place, or add it back before using ComeIn Agent API buttons."
        ) from exc

    appid = (
        os.getenv("COMEIN_APPID")
        or os.getenv("COMEIN_USER_CHANNEL")
        or os.getenv("COMEIN_CHANNEL")
        or os.getenv("COMEIN_APP_ID")
        or ""
    ).strip()
    phone = os.getenv("COMEIN_PHONE", "").strip()
    public_key = os.getenv("COMEIN_PUBLIC_KEY", "").strip()
    private_key = os.getenv("COMEIN_PRIVATE_KEY", "").strip()
    token_url = os.getenv("COMEIN_TOKEN_URL", "").strip()
    chat_url = os.getenv("COMEIN_CHAT_URL", "https://sse.comein.cn/open/sse/chat").strip()

    missing = [
        name
        for name, value in {
            "COMEIN_APPID/COMEIN_USER_CHANNEL": appid,
            "COMEIN_PHONE": phone,
            "COMEIN_PUBLIC_KEY": public_key,
            "COMEIN_PRIVATE_KEY": private_key,
            "COMEIN_TOKEN_URL": token_url,
            "COMEIN_CHAT_URL": chat_url,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError("Missing ComeIn env vars: " + ", ".join(missing))

    _PROVIDER_SINGLETON = ComeInAgentProvider(
        appid=appid,
        phone=phone,
        public_key=public_key,
        private_key=private_key,
        token_url=token_url,
        chat_url=chat_url,
    )
    return _PROVIDER_SINGLETON


async def call_provider_method(provider: Any, payload: Dict[str, Any], max_wait_seconds: int) -> Dict[str, Any]:
    method_names = [
        "post_agent",
        "_post_agent",
        "run_agent",
        "call_agent",
        "chat",
    ]

    last_error: Optional[Exception] = None

    for name in method_names:
        method = getattr(provider, name, None)
        if method is None:
            continue

        attempts = [
            ((), {"payload": payload, "hard_deadline_seconds": float(max_wait_seconds), "line_wait_seconds": 15.0}),
            ((payload,), {"hard_deadline_seconds": float(max_wait_seconds), "line_wait_seconds": 15.0}),
            ((payload,), {"hard_deadline_seconds": float(max_wait_seconds)}),
            ((payload,), {"max_wait_seconds": int(max_wait_seconds)}),
            ((payload,), {}),
        ]

        for args, kwargs in attempts:
            try:
                result = method(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(result, dict):
                    return result
                return {"answer_text": extract_answer_text(result), "raw": result}
            except TypeError as exc:
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                raise

    raise RuntimeError(f"No compatible ComeIn provider method found. Last error: {last_error}")


def infer_market_type_from_full_code(full_code: str) -> str:
    """
    ComeIn company_overview expects marketType as a flat string such as sz/sh/hk.
    """
    code = (full_code or "").strip().lower()
    if code.startswith("sz"):
        return "sz"
    if code.startswith("sh"):
        return "sh"
    if code.startswith("hk"):
        return "hk"
    return ""


COMEIN_AGENT_LABELS: Dict[str, str] = {
    "chain_overview": "主题产业链",
    "chart_search_phase1": "搜图表 第一阶段",
    "chart_search_phase2": "搜图表 第二阶段",
    "company_overview": "公司速览",
    "financial_search": "财务查询",
    "industry_sheet_paper": "行业扫描",
    "performance_review": "业绩点评",
    "relative_valuation": "相对估值",
    "seller_deep_research_report": "深度报告",
    "enterprise_chart_view": "个股事件",
    "viewpoint_compare": "观点梳理",
    "stock_review_phase1": "走势复盘 第一阶段",
    "stock_review_phase2": "走势复盘 第二阶段",
}

COMEIN_DEFAULT_CORE_ABILITIES: List[str] = [
    "company_overview",
    "financial_search",
    "performance_review",
    "relative_valuation",
    "industry_sheet_paper",
]

COMEIN_ADVANCED_ABILITIES: List[str] = [
    "chain_overview",
    "chart_search_phase1",
    "chart_search_phase2",
    "seller_deep_research_report",
    "enterprise_chart_view",
    "viewpoint_compare",
    "stock_review_phase1",
    "stock_review_phase2",
]


def _today_date_str() -> str:
    return time.strftime("%Y-%m-%d")


def _date_days_ago_str(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() - days * 86400))


def _date_str_to_ms(value: str) -> str:
    """
    Accept YYYY-MM-DD or already-ms string.
    viewpoint_compare doc example uses millisecond timestamps.
    """
    value = (value or "").strip()
    if not value:
        return ""
    if re.fullmatch(r"\d{12,14}", value):
        return value

    try:
        # Local midnight is enough for provider filtering.
        dt = time.strptime(value, "%Y-%m-%d")
        return str(int(time.mktime(dt) * 1000))
    except Exception:
        return value


def _merge_extra_params(base: Dict[str, Any], req: ComeInJobRequest) -> Dict[str, Any]:
    """
    Allow a controlled escape hatch for newly discovered provider params,
    while keeping normalized production defaults.
    """
    out = dict(base)
    for k, v in (req.extra_agent_params or {}).items():
        if v is not None and v != "":
            out[k] = v
    return out


def _payload(
    *,
    query: str,
    agent_type: str,
    agent_params: Optional[Dict[str, Any]] = None,
    model: Optional[str] = None,
    use_rag: Optional[int] = None,
    reference_type: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model or COMEIN_MODEL,
        "query": query,
        "agentType": agent_type,
    }

    if agent_params:
        payload["agentParams"] = agent_params

    if use_rag is not None:
        payload["useRag"] = use_rag

    if reference_type:
        payload["referenceType"] = reference_type

    if extra:
        payload.update(extra)

    return payload


def build_comein_agent_payload(req: ComeInJobRequest) -> Dict[str, Any]:
    ability = req.ability.strip()
    stock_name = req.stock_name.strip()
    stock_code = req.stock_code.strip()
    full_code = normalize_full_code(stock_code)
    company_text = display_company_text(stock_name, stock_code)
    market_type = infer_market_type_from_full_code(full_code)

    model = req.model or COMEIN_MODEL

    base_params: Dict[str, Any] = {
        "fullCode": full_code,
        "companyText": company_text,
    }

    if ability not in COMEIN_AGENT_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported ComeIn ability: {ability}. Supported: {list(COMEIN_AGENT_LABELS.keys())}",
        )

    # 1. 主题产业链 / chain_overview
    # Doc says agent_param = null.
    if ability == "chain_overview":
        topic = req.industry_text or stock_name or company_text
        query = req.query or f"请围绕{topic}梳理主题产业链，输出上中下游、关键公司、核心驱动因素和主要风险。"
        return _payload(
            query=query,
            agent_type="chain_overview",
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 2. 搜图表 第一阶段 / chart_search phase 1
    # Doc says first phase params are null.
    if ability == "chart_search_phase1":
        topic = req.industry_text or req.focus_point or stock_name or company_text
        query = req.query or f"请搜索与{topic}相关的投研图表，优先返回可用于深度研究的图表和出处。"
        return _payload(
            query=query,
            agent_type="chart_search",
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 3. 搜图表 第二阶段 / chart_search phase 2
    # Requires recordId, chunkText, chunkIds from selected chart/search result.
    if ability == "chart_search_phase2":
        if not req.record_id or not req.chunk_text or not req.chunk_ids:
            raise HTTPException(
                status_code=400,
                detail="chart_search_phase2 requires record_id, chunk_text, and chunk_ids from phase 1 result.",
            )

        agent_params = _merge_extra_params(
            {
                "recordId": req.record_id,
                "chunkText": req.chunk_text,
                "chunkIds": req.chunk_ids,
            },
            req,
        )
        query = req.query or "请基于选中的图表内容进行解读，提炼核心结论、数据趋势和对投资判断的启示。"
        return _payload(
            query=query,
            agent_type="chart_search",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 4. 公司速览 / company_overview
    # Verified production shape: flat dict, with marketType.
    if ability == "company_overview":
        query = req.query or f"请给我介绍一下{company_text}的基本情况"
        agent_params = _merge_extra_params(
            {
                **base_params,
                "marketType": market_type,
            },
            req,
        )
        return _payload(
            query=query,
            agent_type="company_overview",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 5. 财务查询 / financial_search
    # Verified production shape: no agentParams required.
    if ability == "financial_search":
        query = req.query or (
            f"请对{company_text}做财务查询，重点输出近三年收入、利润、毛利率、"
            f"经营现金流、资本开支、资产负债率、ROE和主要财务变化。"
        )
        return _payload(
            query=query,
            agent_type="financial_search",
            model=model,
        )

    # 6. 行业扫描 / industry_sheet_paper
    if ability == "industry_sheet_paper":
        industry = req.industry_text or stock_name or company_text
        query = req.query or f"请生成{industry}行业扫描，覆盖产业格局、供需变化、竞争格局、核心公司和风险点。"
        agent_params = _merge_extra_params(
            {
                "industry": industry,
            },
            req,
        )
        return _payload(
            query=query,
            agent_type="industry_sheet_paper",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 7. 业绩点评 / performance_review
    # Uses known-good production payload shared by ComeIn.
    if ability == "performance_review":
        fiscal_year = int(req.fiscal_year or time.localtime().tm_year)
        report_type = (req.report_type or "Q1").strip().upper()
        report_label = report_type_label(report_type)
        focus_text = req.focus_text or ""
        compact = True if req.compact is None else bool(req.compact)
        release_text = "精简版" if compact else "详细版"
        release_ids = req.release_ids or [2]

        query = req.query or (
            f"帮我点评一下{company_text}的业绩情况{fiscal_year}年-{report_label}"
            f"请按照{release_text}风格输出"
        )

        agent_params = _merge_extra_params(
            {
                **base_params,
                "fiscalYear": fiscal_year,
                "reportType": report_type,
                "releaseIds": release_ids,
                "reportText": f"{fiscal_year}年-{report_label}",
                "focusText": focus_text,
                "releaseText": release_text,
            },
            req,
        )

        return _payload(
            query=query,
            agent_type="performance_review",
            agent_params=agent_params,
            model=req.model or "Auto",
        )

    # 8. 相对估值 / relative_valuation
    if ability == "relative_valuation":
        query = req.query or f"请对{company_text}做相对估值分析，比较同行估值、盈利能力、成长性和估值折溢价原因。"
        agent_params = _merge_extra_params(base_params, req)
        return _payload(
            query=query,
            agent_type="relative_valuation",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 9. 深度报告 / seller_deep_research_report
    if ability == "seller_deep_research_report":
        industry_text = req.industry_text or ""
        focus_point = req.focus_point or "市场定位、主营业务、竞争格局、盈利能力、估值和主要风险"
        length_requirement = req.length_requirement or "短篇"
        role_name = req.role_name or "sell_side"

        query = req.query or (
            f"我是研究员，请帮我写一篇关于{company_text}的深度报告，"
            f"侧重{focus_point}，要求输出{length_requirement}报告。"
        )
        agent_params = _merge_extra_params(
            {
                **base_params,
                "industryText": industry_text,
                "roleName": role_name,
                "lengthRequirement": length_requirement,
                "focusPoint": focus_point,
            },
            req,
        )
        return _payload(
            query=query,
            agent_type="seller_deep_research_report",
            agent_params=agent_params,
            model=model,
        )

    # 10. 个股事件 / enterprise_chart_view
    if ability == "enterprise_chart_view":
        start_time = req.start_time or _date_days_ago_str(180)
        end_time = req.end_time or _today_date_str()

        query = req.query or f"请梳理{company_text}在{start_time}至{end_time}期间的重要个股事件，并提炼对投资判断的影响。"
        agent_params = _merge_extra_params(
            {
                **base_params,
                "startTime": start_time,
                "endTime": end_time,
            },
            req,
        )
        return _payload(
            query=query,
            agent_type="enterprise_chart_view",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 11. 观点梳理 / viewpoint_compare
    # Doc example uses dateText and millisecond start/end timestamps.
    if ability == "viewpoint_compare":
        date_text = req.date_text or "最近半年"
        start_ms = _date_str_to_ms(req.start_time or _date_days_ago_str(180))
        end_ms = _date_str_to_ms(req.end_time or _today_date_str())

        query = req.query or f"请梳理{company_text}{date_text}的市场观点变化、分歧点、乐观/悲观点和关键验证指标。"
        agent_params = _merge_extra_params(
            {
                **base_params,
                "dateText": date_text,
                "startTime": start_ms,
                "endTime": end_ms,
            },
            req,
        )
        return _payload(
            query=query,
            agent_type="viewpoint_compare",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 12. 走势复盘 第一阶段 / stock_review phase 1
    if ability == "stock_review_phase1":
        start_time = req.start_time or _date_days_ago_str(730)
        end_time = req.end_time or _today_date_str()

        agent_params: Dict[str, Any] = {
            **base_params,
            "autoDetectTrend": req.auto_detect_trend or "1",
            "frequencyType": req.frequency_type or "3",
            "startTime": start_time,
            "endTime": end_time,
        }

        if req.trend_result_list:
            agent_params["trendResultList"] = req.trend_result_list

        agent_params = _merge_extra_params(agent_params, req)

        query = req.query or f"请对{company_text}在{start_time}至{end_time}期间的股价走势进行阶段划分和复盘。"
        return _payload(
            query=query,
            agent_type="stock_review",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    # 13. 走势复盘 第二阶段 / stock_review phase 2
    if ability == "stock_review_phase2":
        start_time = req.start_time or _date_days_ago_str(730)
        end_time = req.end_time or _today_date_str()

        agent_params = _merge_extra_params(
            {
                **base_params,
                "autoDetectTrend": req.auto_detect_trend or "0",
                "startTime": start_time,
                "endTime": end_time,
            },
            req,
        )

        query = req.query or f"请对{company_text}在{start_time}至{end_time}期间的走势做复盘，解释主要上涨/下跌阶段的原因。"
        return _payload(
            query=query,
            agent_type="stock_review",
            agent_params=agent_params,
            model=model,
            use_rag=1,
            reference_type="chunk",
        )

    raise HTTPException(status_code=400, detail=f"Unsupported ComeIn ability: {ability}")


async def run_comein_job(job_id: str, req: ComeInJobRequest) -> None:
    job = COMEIN_JOBS[job_id]
    job["status"] = "running"
    job["started_at"] = now_ts()

    try:
        payload = build_comein_agent_payload(req)
        job["payload"] = payload

        provider = get_comein_provider()
        result = await call_provider_method(provider, payload, req.max_wait_seconds)

        answer_text = extract_answer_text(result)
        thinking_text = extract_thinking_text(result)
        meta = extract_meta(result)

        job.update(
            {
                "status": "finished",
                "finished_at": now_ts(),
                "result": result,
                "answer_text": answer_text,
                "thinking_text": thinking_text,
                "meta": {
                    **meta,
                    "elapsed_seconds": round(now_ts() - job["started_at"], 2),
                    "has_answer_text": bool(answer_text),
                    "has_thinking_text": bool(thinking_text),
                },
            }
        )

        if not answer_text and thinking_text:
            job["meta"]["note"] = (
                "Provider returned planning/thinking text but no final answer_text. "
                "This is retained for progress visibility."
            )

    except Exception as exc:
        job.update(
            {
                "status": "failed",
                "finished_at": now_ts(),
                "error": str(exc),
                "meta": {
                    "elapsed_seconds": round(now_ts() - job.get("started_at", now_ts()), 2),
                },
            }
        )


# =============================================================================
# Manus connector-native path
# =============================================================================

def normalize_selected_connector_ids(connector_ids: Optional[List[str]] = None) -> List[str]:
    """
    Return exactly the connector IDs selected by the user, after trimming,
    de-duplicating, and removing blanks.

    Important:
    - Do NOT filter against a fixed allowlist.
    - Do NOT fall back to the historical two connectors when the user selected none.
    - The user selection is the source of truth.
    """
    cleaned: List[str] = []

    for cid in connector_ids or []:
        cid = str(cid).strip()
        if cid and cid not in cleaned:
            cleaned.append(cid)

    return cleaned


# Backward-compatible function name, but new behavior.
def normalize_report_connectors(connector_ids: Optional[List[str]] = None) -> List[str]:
    return normalize_selected_connector_ids(connector_ids)


def describe_report_connectors(connector_ids: Optional[List[str]] = None) -> List[Dict[str, str]]:
    ids = normalize_selected_connector_ids(connector_ids)
    return [
        {
            "id": cid,
            "label": KNOWN_CONNECTOR_LABELS.get(cid, "User-selected Manus connector"),
        }
        for cid in ids
    ]


def build_manus_connector_prompt(
    req: ChatRequest,
    connector_ids: Optional[List[str]] = None,
    use_connectors: bool = True,
) -> str:
    stock_name = (req.stock_name or "").strip()
    stock_code = (req.stock_code or "").strip()
    full_code = normalize_full_code(stock_code)
    company_text = display_company_text(stock_name, stock_code)

    # For skill-facing display:
    # sz300750 -> SZ300750
    # sh600309 -> SH600309
    skill_code = full_code.upper() if full_code else stock_code.upper()
    display_code = stock_code.upper()

    # Skill invocation should be clean:
    # 宁德时代 SZ300750
    skill_company_name = stock_name or company_text

    selected_connectors = describe_report_connectors(connector_ids or [])

    if use_connectors and selected_connectors:
        connector_lines = "\n".join(
            f"{idx + 1}. {c['label']} / {c['id']}"
            for idx, c in enumerate(selected_connectors)
        )
        biotech_connector_ids = {Yaozh_MCP_HTTP_Test_0_CONNECTOR_ID, PHARMCUBE_MCP_TEST_0_CONNECTOR_ID}
        biotech_connector_instruction = ""
        if biotech_connector_ids.intersection(set(connector_ids or [])):
            biotech_connector_instruction = """
创新药 / 生物医药垂直数据源使用要求：
- Yaozh MCP HTTP Test 0 和 PharmCube MCP - Test 0 仅在用户行业主题包含“创新药”并由前端 gate 放行时使用。
- 它们优先用于创新药管线、临床阶段、适应症、竞争格局、药品/靶点/适应症交叉验证、BD/授权交易、研发催化剂和行业纵深背景。
- 不得用它们替代 dreamworks skill 模板；它们只是补充数据源。
"""
        connector_instruction = f"""
【内部工具边界】
本次 Manus message.connectors 已由系统传入。
你只能使用本次 message.connectors 中实际传入的工具。
不得调用其他工具。

本段是执行约束，不是报告内容。
最终报告只输出正式模板正文，不输出工具使用清单、审计说明或执行过程说明。

如果工具返回为空、超时、未授权或参数错误，只能在对应分析处说明“该项数据未返回可验证结果”，不得编造。
"""
    else:
        connector_instruction = """
本次报告未启用 Manus message.connectors。
请不要声称已经调用 Manus connector / MCP server / tool。
如需外部数据，只能使用用户提供的 ComeIn Agent API 底稿、Alpha Pai Agent / Skill API 底稿或其他已明确提供的材料。
"""

    return f"""
{req.message}

{connector_instruction}
"""


async def manus_post(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not MANUS_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="MANUS_API_KEY is empty. Export MANUS_API_KEY before redeploying.",
        )

    headers = {
        "Content-Type": "application/json",
        "x-manus-api-key": MANUS_API_KEY,
    }

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(f"{MANUS_API_BASE}/{path.lstrip('/')}", headers=headers, json=payload)

    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if resp.status_code >= 400 or data.get("ok") is False:
        raise HTTPException(status_code=resp.status_code, detail=data)

    return data


async def manus_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not MANUS_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="MANUS_API_KEY is empty. Export MANUS_API_KEY before redeploying.",
        )

    headers = {"x-manus-api-key": MANUS_API_KEY}

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{MANUS_API_BASE}/{path.lstrip('/')}", headers=headers, params=params)

    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text}

    if resp.status_code >= 400 or data.get("ok") is False:
        raise HTTPException(status_code=resp.status_code, detail=data)

    return data


def _safe_attachment_basename(*parts: str) -> str:
    raw = "_".join(str(p or "").strip() for p in parts if str(p or "").strip())
    raw = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", raw).strip("._-")
    return raw[:80] or "extended_context"


def _split_header_and_overflow(text: str, max_chars: int) -> tuple[str, str]:
    raw = str(text or "").strip()
    if max_chars <= 0 or len(raw) <= max_chars:
        return raw, ""

    marker = "\n\n[Extended prompt/content moved to attached .txt file because Manus limits message.content text to about 5,000 estimated tokens. Read the attached file before drafting.]\n"
    keep = max(1000, max_chars - len(marker))
    head = raw[:keep].rstrip() + marker
    tail = raw[keep:].lstrip()
    return head, tail


def _append_section(lines: List[str], title: str, body: Optional[str]) -> None:
    text = str(body or "").strip()
    if not text:
        return
    lines.append("")
    lines.append("=" * 88)
    lines.append(title)
    lines.append("=" * 88)
    lines.append("")
    lines.append(text)


def build_manus_extended_context_text(
    *,
    req: ChatRequest,
    prompt_overflow: str = "",
) -> str:
    general_v2 = (req.attachment_layout or "").strip() == "dreamworks_general_v2"

    if general_v2:
        lines: List[str] = [
            "MGC / Manus Extended Context File — DreamWorks General v2",
            "",
            "Purpose:",
            "- The short Manus message text is the controlling Header Prompt and is NOT duplicated here.",
            "- The Header Prompt owns the skill invocation and the external-tool boundary.",
            "- SECTION 1 contains only documented/non-header task and template rules.",
            "- SECTION 2 contains designated data packs only: ComeIn Agent API and Alpha Pai Agent / Skill API.",
            "- SECTION 3 is a safety-only Header overflow area and should normally be absent.",
            "- Read SECTION 1 and SECTION 2 before drafting. Do not paste this file verbatim into the final report.",
        ]

        _append_section(
            lines,
            "SECTION 1 — DOCUMENTED / NON-HEADER TEMPLATE RULES",
            req.documented_prompt,
        )

        pack_lines: List[str] = []
        if str(req.comein_pack or "").strip():
            pack_lines.extend([
                "## 2A — ComeIn Agent API Pack",
                "",
                str(req.comein_pack).strip(),
            ])
        if str(req.alpha_pai_pack or "").strip():
            if pack_lines:
                pack_lines.extend(["", "-" * 88, ""])
            pack_lines.extend([
                "## 2B — Alpha Pai Agent / Skill API Pack",
                "",
                str(req.alpha_pai_pack).strip(),
            ])

        _append_section(
            lines,
            "SECTION 2 — DESIGNATED DATA PACKS",
            "\n".join(pack_lines),
        )

        # Do not mix accidental Header overflow with either rules or data packs.
        # This section exists only as a backend safety fallback.
        _append_section(
            lines,
            "SECTION 3 — HEADER PROMPT OVERFLOW / SAFETY FALLBACK",
            prompt_overflow,
        )

        rendered = "\n".join(lines).strip() + "\n"
        if not any(
            str(x or "").strip()
            for x in [req.documented_prompt, req.comein_pack, req.alpha_pai_pack, prompt_overflow]
        ):
            return ""
        return rendered

    # Legacy/default layout retained for the innovative-drug workflow and older callers.
    lines: List[str] = [
        "MGC / Manus Extended Context File",
        "",
        "Purpose:",
        "- This .txt file contains documented prompt sections and data packs that are too long for Manus message.content.",
        "- The short message header is the controlling execution prompt.",
        "- SECTION 1 (DOCUMENTED PROMPT / NON-HEADER TEMPLATE RULES) is a binding execution constraint and must be read before drafting.",
        "- ComeIn / Alpha Pai sections are designated data-source and verification material; they do not replace the selected DreamWorks skill template.",
        "- Do not paste this file verbatim into the final report.",
    ]

    _append_section(lines, "SECTION 1 — DOCUMENTED PROMPT / NON-HEADER TEMPLATE RULES", req.documented_prompt)
    _append_section(lines, "SECTION 2 — PROMPT OVERFLOW FROM HEADER MESSAGE", prompt_overflow)
    _append_section(lines, "SECTION 3 — COMEIN PACK MARKDOWN", req.comein_pack)
    _append_section(lines, "SECTION 4 — ALPHA PAI PACK MARKDOWN", req.alpha_pai_pack)

    rendered = "\n".join(lines).strip() + "\n"
    if not any(str(x or "").strip() for x in [req.documented_prompt, prompt_overflow, req.comein_pack, req.alpha_pai_pack]):
        return ""
    return rendered


async def manus_upload_text_file(filename: str, text: str) -> Dict[str, Any]:
    body = str(text or "")
    if not body.strip():
        raise HTTPException(status_code=400, detail="Cannot upload an empty Manus context file.")

    created = await manus_post("file.upload", {"filename": filename})
    file_obj = created.get("file") if isinstance(created.get("file"), dict) else {}
    file_id = str(file_obj.get("id") or created.get("file_id") or "").strip()
    upload_url = str(created.get("upload_url") or "").strip()

    if not file_id or not upload_url:
        raise HTTPException(status_code=502, detail={"message": "Manus file.upload did not return file.id/upload_url.", "raw": created})

    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        put_resp = await client.put(
            upload_url,
            content=body.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )

    if put_resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "PUT to Manus presigned upload_url failed.",
                "status_code": put_resp.status_code,
                "body": put_resp.text[:1000],
            },
        )

    detail: Dict[str, Any] = {}
    status = ""
    deadline = time.monotonic() + max(1.0, MANUS_FILE_UPLOAD_READY_TIMEOUT_SECONDS)

    while time.monotonic() < deadline:
        detail = await manus_get("file.detail", {"file_id": file_id})
        status = ""
        if isinstance(detail.get("file"), dict):
            status = str(detail["file"].get("status") or "").strip().lower()

        if status == "uploaded":
            break

        if status in {"error", "deleted"}:
            raise HTTPException(
                status_code=502,
                detail={"message": f"Manus file status is {status}.", "raw": detail},
            )

        await asyncio.sleep(0.5)

    if status != "uploaded":
        raise HTTPException(
            status_code=504,
            detail={
                "message": "Manus file upload did not reach status=uploaded before task.create.",
                "file_id": file_id,
                "last_status": status or "unknown",
                "timeout_seconds": MANUS_FILE_UPLOAD_READY_TIMEOUT_SECONDS,
                "raw": detail,
            },
        )

    return {
        "id": file_id,
        "filename": filename,
        "bytes": len(body.encode("utf-8")),
        "status": status,
        "detail": detail,
    }


def normalize_manus_task_create_response(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = data.get("data") if isinstance(data.get("data"), dict) else data

    return {
        "task_id": payload.get("task_id") or payload.get("id"),
        "task_title": payload.get("task_title") or payload.get("title"),
        "task_url": payload.get("task_url") or payload.get("url"),
        "share_url": payload.get("share_url"),
        "request_id": data.get("request_id") or payload.get("request_id"),
        "raw": data,
    }


def extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)

    if isinstance(content, dict):
        for key in ["text", "content", "message"]:
            if isinstance(content.get(key), str):
                return content[key]

    return ""


def normalize_manus_messages(data: Dict[str, Any]) -> Dict[str, Any]:
    raw_messages = (
        data.get("messages")
        or data.get("data")
        or data.get("items")
        or data.get("results")
        or []
    )

    if isinstance(raw_messages, dict):
        raw_messages = (
            raw_messages.get("messages")
            or raw_messages.get("items")
            or raw_messages.get("results")
            or raw_messages.get("data")
            or []
        )

    messages: List[Dict[str, Any]] = raw_messages if isinstance(raw_messages, list) else []

    latest_text = ""
    assistant_texts: List[str] = []
    timeline: List[Dict[str, Any]] = []
    attachments: List[Dict[str, Any]] = []
    agent_status = ""

    for m in messages:
        if not isinstance(m, dict):
            continue

        event_type = m.get("type") or m.get("event_type") or m.get("message_type") or ""
        role = m.get("role") or m.get("sender") or ""

        status_update = m.get("status_update") if isinstance(m.get("status_update"), dict) else {}

        if event_type == "status_update" or status_update or "status" in m:
            status = (
                status_update.get("agent_status")
                or status_update.get("status")
                or m.get("agent_status")
                or m.get("status")
                or m.get("status_detail")
                or ""
            )
            if status:
                agent_status = str(status)

            brief = status_update.get("brief") or ""
            desc = status_update.get("description") or ""
            status_text = brief or desc or safe_json_preview(m, 800)

            if status_text:
                latest_text = status_text

            timeline.append({
                "type": "status_update",
                "text": status_text,
            })

        content = m.get("content") or m.get("text") or m.get("message") or ""
        text = extract_text_from_content(content)

        if text:
            latest_text = text

            if role == "assistant" or event_type in {"assistant_message", "message"}:
                assistant_texts.append(text)

            timeline.append({
                "type": event_type or role or "message",
                "text": text[:2000],
            })

        for att in _collect_attachments_deep(m):
            key = att.get("source_url") or att.get("download_url") or att.get("name")
            if key and not any(
                (x.get("source_url") or x.get("download_url") or x.get("name")) == key
                for x in attachments
            ):
                attachments.append(att)

    report_markdown = "\n\n".join(assistant_texts).strip()

    return {
        "status": agent_status,
        "agent_status": agent_status,
        "latest_text": latest_text,
        "report_markdown": report_markdown,
        "timeline": timeline[-20:],
        "attachments": attachments,
        "raw": data,
        "debug": {
            "message_count": len(messages),
            "attachment_count": len(attachments),
            "assistant_text_count": len(assistant_texts),
            "agent_status": agent_status,
            "report_markdown_length": len(report_markdown),
        },
    }


async def hydrate_report_from_manus_artifact(normalized: Dict[str, Any]) -> Dict[str, Any]:
    # Assistant text wins if it is already a substantial report.
    existing = (normalized.get("report_markdown") or "").strip()
    if len(existing) >= 40000 and not _looks_like_non_final_markdown_text(existing, "assistant_text"):
        normalized["report_source"] = "assistant_text"
        normalized["report_artifact_name"] = "assistant_text"
        return normalized

    if existing and _looks_like_non_final_markdown_text(existing, "assistant_text"):
        normalized.setdefault("ignored_markdown_artifacts", []).append({
            "name": "assistant_text",
            "reason": "non_final_assistant_text",
        })
        return normalized

    attachments = normalized.get("attachments") or []

    markdown_candidates = [
        att for att in attachments
        if isinstance(att, dict)
        and _is_markdown_artifact(att)
        and _attachment_source_url(att)
    ]

    scored_markdown_candidates = []

    for att in markdown_candidates:
        source_url = _attachment_source_url(att)
        name = _attachment_name(att)

        try:
            md = (await _fetch_manus_markdown_text(source_url)).strip()
        except Exception as exc:
            normalized.setdefault("artifact_fetch_errors", []).append(
                {"name": name, "error": str(exc)}
            )
            continue

        if len(md) < 500:
            continue

        score = _score_markdown_report_candidate(name, md)

        if score <= -100000:
            normalized.setdefault("ignored_markdown_artifacts", []).append({
                "name": name,
                "reason": "non_final_markdown",
            })
            continue

        scored_markdown_candidates.append({
            "name": name,
            "text": md,
            "score": score,
            "source_url": source_url,
            "download_url": att.get("download_url"),
        })

    if scored_markdown_candidates:
        best = max(scored_markdown_candidates, key=lambda x: x["score"])

        normalized["report_markdown"] = best["text"]
        normalized["report_source"] = "manus_markdown_attachment"
        normalized["report_artifact_name"] = best["name"]
        normalized["report_download_url"] = best.get("download_url")
        normalized["latest_text"] = (
            normalized.get("latest_text")
            or f"Loaded final report from {best['name']}"
        )
        return normalized

    # Prefer filenames that look like final reports.
    markdown_candidates.sort(
        key=lambda a: (
            0 if any(k in _attachment_name(a).lower() for k in ["report", "deep", "value", "full"]) else 1,
            _attachment_name(a).lower(),
        )
    )

    for att in markdown_candidates:
        source_url = _attachment_source_url(att)
        try:
            md = (await _fetch_manus_markdown_text(source_url)).strip()
        except Exception as exc:
            normalized.setdefault("artifact_fetch_errors", []).append(
                {"name": _attachment_name(att), "error": str(exc)}
            )
            continue

        if len(md) >= 500:
            normalized["report_markdown"] = md
            normalized["report_source"] = "manus_markdown_attachment"
            normalized["report_artifact_name"] = _attachment_name(att)
            normalized["report_download_url"] = att.get("download_url")
            normalized["latest_text"] = normalized.get("latest_text") or f"Loaded final report from {_attachment_name(att)}"
            return normalized

    # Fallback: longest assistant-like text if no attachment was readable.
    if existing:
        normalized["report_source"] = "short_assistant_text"
    else:
        normalized["report_source"] = "none"

    return normalized
    
# =============================================================================
# Routes: frontend
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>MGC backend is running</h1><p>frontend/index.html not found.</p>")


# =============================================================================
# Routes: health/config
# =============================================================================

@app.get("/api/health")
async def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "auth_gate_installed": True,
        "time": now_ts(),
        "version": "2026-05-13-user-selectable-connectors",
        "has_manus_api_key": bool(MANUS_API_KEY),
        "manus_api_base": MANUS_API_BASE,
        "manus_agent_profile": MANUS_AGENT_PROFILE,
        "recommended_default_connector_ids": RECOMMENDED_DEFAULT_CONNECTOR_IDS,
        "recommended_default_connectors": describe_report_connectors(RECOMMENDED_DEFAULT_CONNECTOR_IDS),
        "connector_selection_mode": "user_selected_multi_select",
        "comein_model": COMEIN_MODEL,
        "alpha_pai_routes_installed": ALPHA_PAI_ROUTES_INSTALLED,
        "alpha_pai_routes_error": ALPHA_PAI_ROUTES_ERROR,
        "bridge_patch": "2026-05-13-selectable-connectors-v1",
    }


@app.get("/api/config")
async def config() -> Dict[str, Any]:
    return {
        "ok": True,
        "recommended_default_connector_ids": RECOMMENDED_DEFAULT_CONNECTOR_IDS,
        "recommended_default_connectors": describe_report_connectors(RECOMMENDED_DEFAULT_CONNECTOR_IDS),
        "connector_selection_mode": "user_selected_multi_select",
        "manus_agent_profile": MANUS_AGENT_PROFILE,
    }


# =============================================================================
# Routes: ComeIn Agent API jobs
# =============================================================================

@app.get("/api/comein-agent/abilities")
async def comein_agent_abilities() -> Dict[str, Any]:
    return {
        "ok": True,
        "labels": COMEIN_AGENT_LABELS,
        "core_abilities": COMEIN_DEFAULT_CORE_ABILITIES,
        "advanced_abilities": COMEIN_ADVANCED_ABILITIES,
        "notes": {
            "chart_search_phase2": "Requires record_id, chunk_text, chunk_ids from chart_search_phase1 result.",
            "stock_review_phase1": "Can auto-detect trend; optional trend_result_list can be passed if provider returns/needs it.",
            "stock_review_phase2": "Manual/non-auto trend review mode.",
            "financial_search": "Known-good payload uses no agentParams.",
            "company_overview": "Known-good payload uses flat dict agentParams with fullCode, companyText, marketType.",
            "performance_review": "Known-good payload uses releaseIds, reportText, focusText, releaseText.",
        },
    }


@app.post("/api/comein-agent/jobs")
async def create_comein_agent_job(req: ComeInJobRequest, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    job_id = str(uuid.uuid4())
    COMEIN_JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "ability": req.ability,
        "stock_name": req.stock_name,
        "stock_code": req.stock_code,
        "created_at": now_ts(),
        "started_at": None,
        "finished_at": None,
        "answer_text": "",
        "thinking_text": "",
        "meta": {},
    }

    background_tasks.add_task(run_comein_job, job_id, req)

    return {
        "job_id": job_id,
        "status": "queued",
        "poll_url": f"/api/comein-agent/jobs/{job_id}",
    }


@app.get("/api/comein-agent/jobs/{job_id}")
async def get_comein_agent_job(job_id: str) -> Dict[str, Any]:
    job = COMEIN_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"ComeIn job not found: {job_id}")
    return job


# =============================================================================
# Routes: resumable frontend sessions
# =============================================================================

@app.post("/api/sessions")
async def create_research_session(
    req: SessionCreateRequest,
    request: Request,
) -> Dict[str, Any]:
    session = _create_session_record(request, title=req.title)
    return {
        "ok": True,
        **_public_session_view(session),
    }


@app.get("/api/sessions/{session_id}")
async def get_research_session(
    session_id: str,
    request: Request,
) -> Dict[str, Any]:
    session = _load_session(session_id)
    _assert_session_owner(session, request)
    return {
        "ok": True,
        **_public_session_view(session),
    }


@app.patch("/api/sessions/{session_id}")
async def update_research_session(
    session_id: str,
    req: SessionUpdateRequest,
    request: Request,
) -> Dict[str, Any]:
    session = _load_session(session_id)
    _assert_session_owner(session, request)
    session = _patch_session_record(session, req)
    return {
        "ok": True,
        **_public_session_view(session),
    }


@app.get("/api/tasks")
async def list_research_tasks(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
) -> Dict[str, Any]:
    return {
        "ok": True,
        "tasks": _list_tasks_for_owner(request, limit=limit),
    }


@app.get("/api/tasks/by-provider/{provider_task_id}")
async def get_research_task_by_provider_id(
    provider_task_id: str,
    request: Request,
) -> Dict[str, Any]:
    task = _find_task_by_provider_task_id(provider_task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Provider task ID is not linked to an MGC task.")
    _assert_task_owner(task, request)
    return {
        "ok": True,
        **_public_task_view(task),
    }


@app.get("/api/tasks/{mgc_task_id}")
async def get_research_task(
    mgc_task_id: str,
    request: Request,
) -> Dict[str, Any]:
    task = _load_task(mgc_task_id)
    _assert_task_owner(task, request)
    return {
        "ok": True,
        **_public_task_view(task),
    }


@app.patch("/api/tasks/{mgc_task_id}")
async def update_research_task(
    mgc_task_id: str,
    req: TaskPatchRequest,
    request: Request,
) -> Dict[str, Any]:
    task = _load_task(mgc_task_id)
    _assert_task_owner(task, request)
    task = _patch_task_record(task, req)
    return {
        "ok": True,
        **_public_task_view(task),
    }


@app.post("/api/report-quality/validate")
async def validate_report_quality(
    req: ReportQualityValidateRequest,
    request: Request,
) -> Dict[str, Any]:
    # Auth is already enforced by auth_gate middleware.
    result = validate_report_quality_core(req)
    return {
        "ok": True,
        **result,
    }

MIN_INNOVATIVE_DRUG_REPORT_CHARS = 40000

# Calibrated proxy: Chinese Markdown with tables usually lands around
# 700 chars/page after DOCX/PDF rendering. Use 600 as a strict MVP proxy.
ESTIMATED_CHARS_PER_PAGE = 600
MIN_INNOVATIVE_DRUG_ESTIMATED_PAGES = 50

# Red-blue PK in 泡泡玛特 spans roughly pages 35-44,
# so it should be treated as a major section, not a short table.
MIN_RED_BLUE_PK_CHARS = 8000
MIN_RED_BLUE_PK_TABLE_ROWS = 40


# =============================================================================
# Routes: Manus connector-native task flow
# =============================================================================

@app.get("/api/manus/connectors")
async def list_manus_connectors() -> Dict[str, Any]:
    return await manus_get("connector.list")



@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request) -> Dict[str, Any]:
    if req.provider != "manus":
        raise HTTPException(status_code=400, detail="Only provider='manus' is supported.")

    # Frontend's explicit use_connectors flag wins.
    # Fall back to use_manus_connectors only for older clients.
    use_connectors = req.use_connectors if req.use_connectors is not None else req.use_manus_connectors

    requested_connector_ids = req.connector_ids if req.connector_ids is not None else req.connectors
    connector_ids = normalize_selected_connector_ids(requested_connector_ids)

    if use_connectors and not connector_ids:
        raise HTTPException(
            status_code=400,
            detail="use_connectors=true but no connector IDs were selected. Please select at least one connector or disable connector usage.",
        )

    content = build_manus_connector_prompt(req, connector_ids=connector_ids, use_connectors=bool(use_connectors))

    header_text, prompt_overflow = _split_header_and_overflow(content, MANUS_HEADER_PROMPT_MAX_CHARS)
    extended_context_text = build_manus_extended_context_text(req=req, prompt_overflow=prompt_overflow)
    uploaded_context_file: Optional[Dict[str, Any]] = None

    content_parts: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": header_text,
        }
    ]

    if len(extended_context_text.strip()) >= MANUS_ATTACHMENT_MIN_CHARS:
        filename = (
            f"{MANUS_EXTENDED_CONTEXT_FILENAME_PREFIX}_"
            f"{_safe_attachment_basename(req.stock_name or '', req.stock_code or '', uuid.uuid4().hex[:8])}.txt"
        )
        uploaded_context_file = await manus_upload_text_file(filename, extended_context_text)
        content_parts.append(
            {
                "type": "file",
                "file_id": uploaded_context_file["id"],
            }
        )

    message_obj: Dict[str, Any] = {"content": content_parts}
    if use_connectors and connector_ids:
        message_obj["connectors"] = connector_ids

    payload: Dict[str, Any] = {
        "message": message_obj,
        "locale": req.locale or "zh-CN",
        "agent_profile": MANUS_AGENT_PROFILE,
        "share_visibility": "private",
        "title": req.title or f"MGC Custom-Connector Deep Value - {req.stock_name or req.stock_code or 'Report'}",
    }

    data = await manus_post("task.create", payload)
    normalized = normalize_manus_task_create_response(data)

    if not normalized.get("task_id"):
        raise HTTPException(status_code=502, detail="Manus task.create did not return task_id.")

    task_title = req.title or f"MGC Custom-Connector Deep Value - {req.stock_name or req.stock_code or 'Report'}"
    mgc_task = _create_task_record(
        request,
        provider_task_id=normalized["task_id"],
        session_id=req.session_id,
        task_url=normalized["task_url"],
        share_url=normalized["share_url"],
        stock_name=req.stock_name,
        stock_code=req.stock_code,
        title=task_title,
        status="submitted",
        latest_text="Manus task submitted. You can recover this task with the MGC Task ID.",
        connector_ids=connector_ids if use_connectors else [],
        prompt_chars=len(header_text),
        request_id=normalized["request_id"],
        meta={
            "provider_raw": normalized["raw"],
            "manus_header_prompt_chars": len(header_text),
            "manus_original_prompt_chars": len(content),
            "manus_prompt_overflow_chars": len(prompt_overflow),
            "manus_extended_context_file": uploaded_context_file,
            "manus_extended_context_chars": len(extended_context_text),
            "manus_documented_prompt_chars": len(str(req.documented_prompt or "")),
            "manus_comein_pack_chars": len(str(req.comein_pack or "")),
            "manus_alpha_pai_pack_chars": len(str(req.alpha_pai_pack or "")),
        },
    )
    effective_session_id = req.session_id

    if req.session_id:
        try:
            session = _load_session(req.session_id)
            _assert_session_owner(session, request)
            _patch_session_record(
                session,
                {
                    "mgc_task_id": mgc_task["mgc_task_id"],
                    "task_id": normalized["task_id"],
                    "task_url": normalized["task_url"],
                    "share_url": normalized["share_url"],
                    "stock_name": req.stock_name,
                    "stock_code": req.stock_code,
                    "title": task_title,
                    "status": "submitted",
                    "latest_text": "Manus task submitted. Frontend can resume this task with either Session ID or MGC Task ID.",
                    "report_ready": False,
                    "connector_ids": connector_ids if use_connectors else [],
                    "active_tab": "final",
                },
            )
        except HTTPException as exc:
            if exc.status_code == 404:
                # Stale frontend session after redeploy / non-persistent session store.
                # Do not fail a successfully-created Manus task.
                effective_session_id = None
            else:
                raise

    return {
        "reply": "Manus task submitted with user-selected connectors.",
        "provider": "manus",
        "session_id": effective_session_id,
        "mgc_task_id": mgc_task["mgc_task_id"],
        "task_id": normalized["task_id"],
        "task_title": normalized["task_title"],
        "task_url": normalized["task_url"],
        "share_url": normalized["share_url"],
        "connectors_injected": bool(use_connectors and connector_ids),
        "connector_ids": connector_ids if use_connectors else [],
        "report_connectors": describe_report_connectors(connector_ids if use_connectors else []),
        "agent_profile": MANUS_AGENT_PROFILE,
        "prompt_chars": len(header_text),
        "original_prompt_chars": len(content),
        "documented_prompt_chars": len(str(req.documented_prompt or "")),
        "extended_context_chars": len(extended_context_text),
        "extended_context_file": uploaded_context_file,
        "request_id": normalized["request_id"],
        "raw": normalized["raw"],
    }



@app.post("/api/manus/connector-smoke-test")
async def connector_smoke_test(req: ConnectorSmokeTestRequest) -> Dict[str, Any]:
    requested_connector_ids = req.connector_ids if req.connector_ids is not None else req.connectors
    connector_ids = normalize_selected_connector_ids(requested_connector_ids)

    if not connector_ids:
        raise HTTPException(
            status_code=400,
            detail="No connector IDs selected for smoke test.",
        )

    stock_name = req.stock_name.strip() or "宁德时代"
    stock_code = req.stock_code.strip() or "300750.SZ"
    full_code = normalize_full_code(stock_code)
    skill_code = full_code.upper() if full_code else stock_code.upper()

    selected_connectors_text = "\n".join(
        f"{idx + 1}. {c['label']} / {c['id']}"
        for idx, c in enumerate(describe_report_connectors(connector_ids))
    )

    content = f"""
CONNECTOR_SELECTION_SMOKE_TEST：

请只测试本次 message.connectors 已传入的用户勾选 connectors：

{selected_connectors_text}

研究对象：{stock_name} {stock_code} / {skill_code}
ComeIn fullCode={full_code}

不要写长报告，不要深度分析，完成下面输出后立刻停止。

必须输出：
1. 实际启用 connectors/tools 清单。
2. 每个已勾选 connector 是否被实际调用。
3. 每个 connector 的 success/fail、tool/endpoint 名称、核心返回摘要。
4. 如果某个 connector 未被实际调用，请写明“该 connector 已传入但未被实际调用”。
5. 如果工具返回为空，请写明“该工具返回为空”，不要编造。
""".strip()

    payload: Dict[str, Any] = {
        "message": {
            "content": content,
            "connectors": connector_ids,
        },
        "locale": req.locale,
        "title": f"Connector Selection Smoke Test - {stock_name}",
        "agent_profile": MANUS_AGENT_PROFILE,
        "share_visibility": "private",
    }

    data = await manus_post("task.create", payload)
    normalized = normalize_manus_task_create_response(data)

    return {
        "reply": "Connector selection smoke test submitted.",
        "task_id": normalized["task_id"],
        "task_title": normalized["task_title"],
        "task_url": normalized["task_url"],
        "share_url": normalized["share_url"],
        "connectors_injected": True,
        "connector_ids": connector_ids,
        "report_connectors": describe_report_connectors(connector_ids),
        "request_id": normalized["request_id"],
        "raw": normalized["raw"],
    }


@app.get("/api/manus/tasks/{task_id}")
async def get_manus_task_messages(
    task_id: str,
    request: Request,
    order: str = "desc",
    limit: int = 50,
) -> Dict[str, Any]:
    data = await manus_get(
        "task.listMessages",
        params={
            "task_id": task_id,
            "order": order,
            "limit": limit,
        },
    )

    normalized = normalize_manus_messages(data)
    normalized = await hydrate_report_from_manus_artifact(normalized)

    patched_task = _patch_task_by_provider_id(
        task_id,
        request,
        {
            "status": normalized.get("status") or normalized.get("agent_status") or None,
            "latest_text": normalized.get("latest_text") or None,
            "report_markdown": normalized.get("report_markdown") or None,
            "report_ready": bool((normalized.get("report_markdown") or "").strip()),
            "artifacts": normalized.get("attachments") or [],
            "meta": {
                "last_provider_sync_at": _session_timestamp(),
                "debug": normalized.get("debug") or {},
                "report_source": normalized.get("report_source"),
                "report_artifact_name": normalized.get("report_artifact_name"),
            },
        },
    )
    if patched_task:
        normalized["mgc_task_id"] = patched_task.get("mgc_task_id")

    return normalized


@app.get("/api/manus/tasks/{task_id}/messages")
async def list_manus_task_messages(
    task_id: str,
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    cursor: str | None = None,
    order: str = "asc",
    verbose: bool = True,
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "task_id": task_id,
        "limit": limit,
        "order": order,
        "verbose": str(verbose).lower(),
    }

    if cursor:
        params["cursor"] = cursor

    data = await manus_get(
        "task.listMessages",
        params=params,
    )

    # Keep the raw Manus response intact, but add normalized artifact metadata
    # so the frontend can show downloadable follow-up files inside the chat tab.
    data["normalized_attachments"] = _collect_attachments_deep(data)

    messages = data.get("messages")
    if not isinstance(messages, list):
        raw_data = data.get("data")
        if isinstance(raw_data, dict) and isinstance(raw_data.get("messages"), list):
            messages = raw_data.get("messages")
        elif isinstance(raw_data, list):
            messages = raw_data
        else:
            messages = []

    patched_task = _patch_task_by_provider_id(
        task_id,
        request,
        {
            "messages": messages,
            "artifacts": data["normalized_attachments"],
            "meta": {"last_message_sync_at": _session_timestamp()},
        },
    )
    if patched_task:
        data["mgc_task_id"] = patched_task.get("mgc_task_id")

    return data


@app.post("/api/manus/tasks/{task_id}/send-message")
async def send_manus_task_message(
    task_id: str,
    req: ManusSendMessageRequest,
) -> Dict[str, Any]:
    text = (req.message or "").strip()

    if not text:
        raise HTTPException(status_code=400, detail="message is required")

    message_obj: Dict[str, Any] = {
        "content": [
            {
                "type": "text",
                "text": text,
            }
        ]
    }

    if req.connectors:
        message_obj["connectors"] = normalize_selected_connector_ids(req.connectors)

    if req.enable_skills:
        message_obj["enable_skills"] = req.enable_skills

    if req.force_skills:
        message_obj["force_skills"] = req.force_skills

    payload: Dict[str, Any] = {
        "task_id": task_id,
        "message": message_obj,
    }

    # For sendMessage, omit agent_profile unless explicitly supplied so Manus
    # keeps the task's current profile.
    if req.agent_profile:
        payload["agent_profile"] = req.agent_profile

    if req.structured_output_schema:
        payload["structured_output_schema"] = req.structured_output_schema

    return await manus_post("task.sendMessage", payload)


# =============================================================================
# Routes: export
# =============================================================================

@app.post("/api/export")
async def export_report(req: ExportRequest) -> Dict[str, Any]:
    fmt = (req.format or "md").lower().strip()
    if fmt not in {"md", "txt", "docx", "pdf"}:
        raise HTTPException(status_code=400, detail="format must be one of: md, txt, docx, pdf")

    ts = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"mgc_report_{ts}_{uuid.uuid4().hex[:8]}"
    md = req.report_markdown or ""

    if fmt in {"md", "txt"}:
        suffix = "md" if fmt == "md" else "txt"
        path = EXPORT_DIR / f"{base_name}.{suffix}"
        path.write_text(md, encoding="utf-8")
        return {"download_url": f"/exports/{path.name}", "filename": path.name}

    if fmt == "docx":
        try:
            from docx import Document
        except Exception as exc:
            raise HTTPException(status_code=500, detail="python-docx is not installed on the server.") from exc

        path = EXPORT_DIR / f"{base_name}.docx"
        doc = Document()
        for block in md.splitlines():
            line = block.rstrip()
            if line.startswith("# "):
                doc.add_heading(line[2:].strip(), level=1)
            elif line.startswith("## "):
                doc.add_heading(line[3:].strip(), level=2)
            elif line.startswith("### "):
                doc.add_heading(line[4:].strip(), level=3)
            elif line.strip():
                doc.add_paragraph(line)
            else:
                doc.add_paragraph("")
        doc.save(path)
        return {"download_url": f"/exports/{path.name}", "filename": path.name}

    if fmt == "pdf":
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen import canvas
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        except Exception as exc:
            raise HTTPException(status_code=500, detail="reportlab is not installed on the server.") from exc

        path = EXPORT_DIR / f"{base_name}.pdf"
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        c = canvas.Canvas(str(path), pagesize=A4)
        width, height = A4
        x = 40
        y = height - 50
        line_height = 16
        c.setFont("STSong-Light", 10)

        for raw_line in md.splitlines():
            line = raw_line.rstrip()
            chunks = [line[i:i + 52] for i in range(0, len(line), 52)] or [""]
            for chunk in chunks:
                if y < 50:
                    c.showPage()
                    c.setFont("STSong-Light", 10)
                    y = height - 50
                c.drawString(x, y, chunk)
                y -= line_height

        c.save()
        return {"download_url": f"/exports/{path.name}", "filename": path.name}

    raise HTTPException(status_code=500, detail="Unexpected export format.")


@app.get("/api/download/{filename}")
async def download_file(filename: str) -> FileResponse:
    path = EXPORT_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path), filename=filename)


@app.get("/api/proxy-file")
async def proxy_file(url: str = Query(...), name: str = Query("artifact")):
    raw, content_type = await _fetch_manus_file_bytes(url)

    safe_name = re.sub(r'[^A-Za-z0-9._\-\u4e00-\u9fff]+', "_", name or "artifact")
    headers = {
        "Content-Disposition": f'attachment; filename="{safe_name}"',
        "Cache-Control": "no-store",
    }

    return Response(content=raw, media_type=content_type, headers=headers)