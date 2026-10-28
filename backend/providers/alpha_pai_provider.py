from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple
import uuid
import httpx


# =============================================================================
# Alpha Pai Agent / Skill API abilities
# Main endpoint:
#   /alpha/open-api/v1/paipai/stock/agent
# =============================================================================

ALPHA_AGENT_ABILITIES: Dict[str, Dict[str, Any]] = {
    "alpha_stock_performance_review": {
        "label": "个股业绩点评",
        "agentMode": 1,
        "needs_stock": True,
        "needs_report": True,
    },
    "alpha_company_one_pager": {
        "label": "公司一页纸",
        "agentMode": 2,
        "needs_stock": True,
        "needs_report": False,
    },
    "alpha_research_outline": {
        "label": "个股调研大纲",
        "agentMode": 3,
        "needs_stock": True,
        "needs_report": False,
    },
    "alpha_theme_stock": {
        "label": "主题选股",
        "agentMode": 5,
        "needs_stock": False,
        "needs_report": False,
    },
    "alpha_investment_logic": {
        "label": "投资逻辑",
        "agentMode": 7,
        "needs_stock": True,
        "needs_report": False,
    },
    "alpha_comparable_companies": {
        "label": "可比公司",
        "agentMode": 8,
        "needs_stock": True,
        "needs_report": False,
    },
    "alpha_viewpoint_challenge": {
        "label": "观点Challenge",
        "agentMode": 9,
        "needs_stock": False,
        "needs_report": False,
    },
    "alpha_industry_one_pager": {
        "label": "行业一页纸",
        "agentMode": 11,
        "needs_stock": False,
        "needs_report": False,
    },
}

ABILITY_LABELS: Dict[str, str] = {
    ability: spec["label"]
    for ability, spec in ALPHA_AGENT_ABILITIES.items()
}

ABILITY_TO_API_NAME: Dict[str, str] = {}


def refresh_ability_to_api_name() -> None:
    """
    Keep this function because alpha_pai_routes.py already calls it.

    New meaning:
    - keys are our frontend/backend ability names
    - values are the actual AlphaPai Agent endpoint marker
    """
    ABILITY_TO_API_NAME.clear()
    for ability in ALPHA_AGENT_ABILITIES:
        ABILITY_TO_API_NAME[ability] = "/alpha/open-api/v1/paipai/stock/agent"


refresh_ability_to_api_name()

ALPHA_PAI_BASE_URL = os.getenv("ALPHA_PAI_BASE_URL", "https://open-api.rabyte.cn").rstrip("/")
ALPHA_PAI_APP_ID = os.getenv("ALPHA_PAI_APP_ID", "").strip()
ALPHA_PAI_APP_SECRET = os.getenv("ALPHA_PAI_APP_SECRET", "").strip()

ALPHA_PAI_USER_CODE = os.getenv("ALPHA_PAI_USER_CODE", "2024052701")
ALPHA_PAI_USER_NAME = os.getenv("ALPHA_PAI_USER_NAME", "mgc_user")
ALPHA_PAI_ROLE_NAME = os.getenv("ALPHA_PAI_ROLE_NAME", "基金经理")
ALPHA_PAI_ROLE_CODE = os.getenv("ALPHA_PAI_ROLE_CODE", "5")


def calculate_hmac(content: str, app_secret: str) -> str:
    key = app_secret.encode("utf-8")
    content_bytes = content.encode("utf-8")
    return hmac.new(key, content_bytes, hashlib.sha256).hexdigest()


def _alpha_headers(body_json: str) -> Dict[str, str]:
    if not ALPHA_PAI_APP_ID or not ALPHA_PAI_APP_SECRET:
        raise RuntimeError("Missing ALPHA_PAI_APP_ID or ALPHA_PAI_APP_SECRET")

    return {
        "app-agent": ALPHA_PAI_APP_ID,
        "sign": calculate_hmac(body_json, ALPHA_PAI_APP_SECRET),
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Connection": "keep-alive",
    }


def _alpha_user_info() -> Dict[str, str]:
    return {
        "userCode": ALPHA_PAI_USER_CODE,
        "userName": ALPHA_PAI_USER_NAME,
        "roleName": ALPHA_PAI_ROLE_NAME,
        "roleCode": ALPHA_PAI_ROLE_CODE,
    }


async def _post_alpha_json_or_sse(path: str, payload: Dict[str, Any], timeout: int = 300) -> Dict[str, Any]:
    body_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = _alpha_headers(body_json)
    url = f"{ALPHA_PAI_BASE_URL}{path}"

    answer_parts: List[str] = []
    references: List[Any] = []
    events: List[Any] = []
    raw_text_parts: List[str] = []

    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            content=body_json.encode("utf-8"),
        ) as resp:
            text = ""
            async for chunk in resp.aiter_text():
                if not chunk:
                    continue
                text += chunk
                raw_text_parts.append(chunk)

                # SSE-compatible parser: data: {...}
                while "\n\n" in text:
                    event, text = text.split("\n\n", 1)
                    event = event.strip()
                    if not event.startswith("data:"):
                        continue

                    line = event[5:].strip()
                    if not line:
                        continue

                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    events.append(obj)
                    piece = obj.get("data") if isinstance(obj, dict) else None
                    if isinstance(piece, dict):
                        if piece.get("answer") is not None:
                            answer_parts.append(str(piece.get("answer")))
                        if isinstance(piece.get("references"), list):
                            references.extend(piece["references"])

            raw_text = "".join(raw_text_parts).strip()

            # Non-SSE JSON fallback.
            if not events and raw_text:
                try:
                    obj = json.loads(raw_text)
                    data = obj.get("data") if isinstance(obj, dict) else None
                    if isinstance(data, dict):
                        return {
                            "ok": obj.get("code") == 200000,
                            "code": obj.get("code"),
                            "message": obj.get("message"),
                            "answer_text": data.get("answer") or "",
                            "references": data.get("references") or [],
                            "raw": obj,
                        }
                    return {"ok": True, "answer_text": "", "references": [], "raw": obj}
                except Exception:
                    pass

    return {
        "ok": True,
        "answer_text": "".join(answer_parts).strip(),
        "references": references,
        "events": events,
        "raw_text_preview": "".join(raw_text_parts)[:4000],
    }


# =============================================================================
# Normalization
# =============================================================================

def normalize_full_code(stock_code: str) -> str:
    """
    Generic exchange-code normalizer.
    Examples by shape only:
      6-digit.SZ -> sz + 6-digit
      6-digit.SH -> sh + 6-digit
      4/5-digit.HK -> hk + code
    """
    code = (stock_code or "").strip()
    if not code:
        return ""

    upper = code.upper()

    if re.fullmatch(r"(SZ|SH)\d{6}", upper):
        return upper[:2].lower() + upper[2:]

    if re.fullmatch(r"HK\d{4,5}", upper):
        return "hk" + upper[2:]

    if "." in upper:
        num, suffix = upper.split(".", 1)
        num = num.strip()
        suffix = suffix.strip().upper()
        if suffix == "SZ" and re.fullmatch(r"\d{6}", num):
            return f"sz{num}"
        if suffix == "SH" and re.fullmatch(r"\d{6}", num):
            return f"sh{num}"
        if suffix == "HK" and re.fullmatch(r"\d{4,5}", num):
            return f"hk{num}"

    if re.fullmatch(r"\d{6}", upper):
        if upper.startswith(("0", "3")):
            return f"sz{upper}"
        if upper.startswith(("6", "9")):
            return f"sh{upper}"

    if re.fullmatch(r"\d{4,5}", upper):
        return f"hk{upper}"

    return code


def normalize_display_code(stock_code: str) -> str:
    code = (stock_code or "").strip()
    if not code:
        return ""

    upper = code.upper()

    if re.fullmatch(r"\d{6}\.(SZ|SH)", upper):
        return upper

    if re.fullmatch(r"\d{4,5}\.HK", upper):
        return upper

    if re.fullmatch(r"(SZ|SH)\d{6}", upper):
        return f"{upper[2:]}.{upper[:2]}"

    if re.fullmatch(r"HK\d{4,5}", upper):
        return f"{upper[2:]}.HK"

    if re.fullmatch(r"\d{6}", upper):
        if upper.startswith(("0", "3")):
            return f"{upper}.SZ"
        if upper.startswith(("6", "9")):
            return f"{upper}.SH"

    if re.fullmatch(r"\d{4,5}", upper):
        return f"{upper}.HK"

    return upper


def _code_num(stock_code: str) -> str:
    display = normalize_display_code(stock_code)
    if display and "." in display:
        num = display.split(".", 1)[0]
        if re.fullmatch(r"\d{4,6}", num):
            return num

    full = normalize_full_code(stock_code).lower()
    if re.fullmatch(r"(sz|sh)\d{6}", full):
        return full[2:]
    if re.fullmatch(r"hk\d{4,5}", full):
        return full[2:]

    raw = (stock_code or "").strip()
    return raw if re.fullmatch(r"\d{4,6}", raw) else ""


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _dedupe_keep_order(items: Iterable[Any]) -> List[Any]:
    seen: set[str] = set()
    out: List[Any] = []
    for item in items:
        if item is None:
            continue
        key = str(item).strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _split_keywords(value: Any) -> List[str]:
    """Accept None, list/tuple/set, comma/semicolon/newline string. Never split a string into chars."""
    if not value:
        return []
    if isinstance(value, str):
        text = value.replace("，", ",").replace("；", ",").replace(";", ",")
        parts: List[str] = []
        for chunk in text.split(","):
            parts.extend(x.strip() for x in chunk.splitlines() if x.strip())
        return _dedupe_keep_order(parts)
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            out.extend(_split_keywords(item))
        return _dedupe_keep_order([x.strip() for x in out if x and x.strip()])
    text = _normalize_text(value).strip()
    return [text] if text else []


# =============================================================================
# Runtime-only keyword construction
# =============================================================================

DEFAULT_BACKGROUND_KEYWORDS: List[str] = []


def _strip_security_name_prefixes(name: str) -> str:
    name = (name or "").strip()
    return re.sub(
        r"^(?:S\*ST|\*ST|ST|SST|退市|N|C|U|W|XD|XR|DR)\s*",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()


def _company_code_variants(stock_code: str) -> List[str]:
    code_num = _code_num(stock_code)
    display_code = normalize_display_code(stock_code)
    full_code = normalize_full_code(stock_code)

    variants: List[str] = [
        code_num,
        display_code,
        display_code.upper() if display_code else "",
        display_code.lower() if display_code else "",
        full_code,
        full_code.upper() if full_code else "",
    ]

    if code_num and display_code and "." in display_code:
        market = display_code.split(".", 1)[1].upper()
        variants.extend([
            f"{market}{code_num}",
            f"{market.lower()}{code_num}",
            f"{code_num}{market}",
            f"{code_num} {market}",
        ])

        if market == "SH":
            variants.extend([f"{code_num}.SS", f"{code_num}CH", f"{code_num} CH"])
        elif market == "SZ":
            variants.extend([f"{code_num}CH", f"{code_num} CH"])
        elif market == "HK":
            variants.extend([f"{code_num}.HK", f"HK{code_num}", f"hk{code_num}"])

    return _dedupe_keep_order([x for x in variants if x])


def _generic_company_name_variants(stock_name: str) -> List[str]:
    raw = (stock_name or "").strip()
    clean = _strip_security_name_prefixes(raw)

    variants: List[str] = []
    if raw:
        variants.append(raw)
    if clean and clean != raw:
        variants.append(clean)

    # Generic legal-form variants only; no issuer-specific aliases are embedded.
    base_candidates = [x for x in [clean, raw] if x]
    for base in base_candidates:
        variants.extend([
            f"{base}股份",
            f"{base}集团",
            f"{base}股份有限公司",
            f"{base}集团股份有限公司",
            f"{base}有限公司",
            f"{base}控股",
            f"{base}国际",
        ])

    return _dedupe_keep_order(variants)


def _runtime_company_aliases(
    stock_name: str,
    stock_code: str,
    user_aliases: Any = None,
) -> List[str]:
    aliases: List[str] = []
    aliases.extend(_generic_company_name_variants(stock_name))
    aliases.extend(_company_code_variants(stock_code))
    aliases.extend(_split_keywords(user_aliases))

    # Short all-letter aliases are high risk and should not be auto-generated.
    # If a user enters them, they are allowed but later require co-occurrence.
    return _dedupe_keep_order([str(x).strip() for x in aliases if str(x).strip()])


def build_keyword_groups(
    stock_name: str,
    stock_code: str,
    user_keywords: Any = None,
    *,
    company_aliases: Any = None,
    research_focus: Any = None,
) -> Tuple[List[str], List[str], List[str]]:
    """
    Returns:
    - direct_keywords: runtime company/name/code aliases only
    - focus_keywords: research-focus terms; ranking only, never company evidence alone
    - background_keywords: sector terms; never company evidence alone
    """
    direct_keywords = _runtime_company_aliases(
        stock_name=stock_name,
        stock_code=stock_code,
        user_aliases=company_aliases,
    )

    focus_keywords = _split_keywords(research_focus)
    background_keywords: List[str] = list(DEFAULT_BACKGROUND_KEYWORDS)

    for kw in _split_keywords(user_keywords):
        kw_text = str(kw).strip()
        if not kw_text:
            continue

        kw_upper = kw_text.upper()
        is_direct = any(
            kw_text == str(alias).strip()
            or kw_upper == str(alias).strip().upper()
            for alias in direct_keywords
        )

        if is_direct:
            direct_keywords.append(kw_text)
        else:
            background_keywords.append(kw_text)

    return (
        _dedupe_keep_order(direct_keywords),
        _dedupe_keep_order(focus_keywords),
        _dedupe_keep_order(background_keywords),
    )


# =============================================================================
# Row normalization and matching
# =============================================================================

TEXT_FIELD_CANDIDATES: List[str] = [
    # Generic semantic fields
    "title", "show_title", "subject", "name", "file_name",
    "content", "content_cn", "body", "text",
    "summary", "abstract", "desc", "description", "contextInfo", "chunks",
    "comment", "comment_cn", "comment_en", "translate_content",
    # Report / roadshow
    "roadshow_title", "trans_title", "report_title", "research_title",
    "page_trans",
    # Public-account / article
    "arc_name", "arc_name_process",
    # Security/company metadata
    "stock_name", "stock_code", "sec_name", "sec_code", "sec_hcode",
    "company_name", "company", "company_short_name", "company_full_name",
    "sec_json", "ind_json",
    # Source/person
    "inst_cname", "institution", "source", "team_cname",
    "psn_name", "author", "author_p", "analyst", "broker", "org_name",
    # Fallback IDs; useful for display, but low-weight in matching
    "id", "cmnt_hcode", "rpt_hcode", "roadshow_id", "recordId", "record_id",
]

TITLE_FIELD_CANDIDATES: List[str] = [
    "title", "show_title", "roadshow_title", "trans_title", "report_title",
    "research_title", "arc_name", "arc_name_process", "subject", "file_name", "name",
]

COMPANY_META_FIELD_CANDIDATES: List[str] = [
    "stock_name", "stock_code", "sec_name", "sec_code", "sec_hcode",
    "company_name", "company", "company_short_name", "company_full_name", "sec_json",
]

DATE_FIELD_CANDIDATES: List[str] = [
    "hupdatetime", "hcreatetime", "cmnt_date", "publish_time", "publishDate",
    "spider_time", "create_time", "created_at", "date", "update_time",
]

LOW_SIGNAL_MATCH_KEY_RE = re.compile(
    r"(url|cover|pic|pics|image|img|html|path|biz|wxacc|rarc_code|arc_code|hcode|crawler_code)",
    re.IGNORECASE,
)

SAFE_NESTED_KEYS = {
    "sec_json",
    "ind_json",
    "chunks",
    "contextInfo",
    "content",
    "content_cn",
    "comment_cn",
    "summary",
    "abstract",
    "page_trans",
}


def _clean_match_text(text: str) -> str:
    text = text or ""
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b[A-Za-z0-9_\-=]{24,}\b", " ", text)
    return text


def _deep_text(obj: Any, *, max_chars: int = 200_000) -> str:
    """Flatten semantically useful fields. Avoid matching against URLs/images/random IDs."""
    parts: List[str] = []
    current_len = 0

    def add(s: str) -> None:
        nonlocal current_len
        if not s:
            return
        if current_len > max_chars:
            return
        parts.append(s)
        current_len += len(s)

    def walk(x: Any, key_hint: str = "") -> None:
        if current_len > max_chars:
            return
        if x is None:
            return

        if key_hint and LOW_SIGNAL_MATCH_KEY_RE.search(key_hint):
            return

        if isinstance(x, str):
            s = _clean_match_text(x.strip())
            if s:
                add(s)
            return

        if isinstance(x, (int, float, bool)):
            add(str(x))
            return

        if isinstance(x, dict):
            for key in TEXT_FIELD_CANDIDATES:
                if key in x and not LOW_SIGNAL_MATCH_KEY_RE.search(key):
                    walk(x.get(key), key)

            for key, value in x.items():
                if key in TEXT_FIELD_CANDIDATES:
                    continue
                if LOW_SIGNAL_MATCH_KEY_RE.search(key):
                    continue
                if key in SAFE_NESTED_KEYS or isinstance(value, (dict, list, tuple)):
                    walk(value, key)
            return

        if isinstance(x, (list, tuple, set)):
            for item in x:
                walk(item, key_hint)
            return

        add(_clean_match_text(str(x)))

    walk(obj)
    return "\n".join(parts)


def _row_text(row: Dict[str, Any]) -> str:
    return _deep_text(row)


def _row_title_text(row: Dict[str, Any]) -> str:
    return "\n".join(
        _normalize_text(row.get(k)).strip()
        for k in TITLE_FIELD_CANDIDATES
        if _normalize_text(row.get(k)).strip()
    )


def _row_company_meta_text(row: Dict[str, Any]) -> str:
    return "\n".join(
        _normalize_text(row.get(k)).strip()
        for k in COMPANY_META_FIELD_CANDIDATES
        if _normalize_text(row.get(k)).strip()
    )


def _is_short_english_alias(keyword: str) -> bool:
    k = str(keyword or "").strip()
    return bool(re.fullmatch(r"[A-Za-z]{1,3}", k))


def _is_numeric_code_keyword(keyword: str) -> bool:
    return bool(re.search(r"\d{4,6}", str(keyword or "")))


def _keyword_in_text(keyword: str, text: str) -> bool:
    if not keyword or not text:
        return False

    keyword = str(keyword).strip()
    text = str(text)

    if not re.search(r"[A-Za-z]", keyword):
        return keyword in text

    pattern = r"(?<![A-Za-z0-9])" + re.escape(keyword) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def classify_row(
    row: Dict[str, Any],
    direct_keywords: List[str],
    background_keywords: List[str],
    focus_keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    full_text = _row_text(row)
    title_text = _row_title_text(row)
    meta_text = _row_company_meta_text(row)
    focus_keywords = focus_keywords or []

    raw_direct_hits = [kw for kw in direct_keywords if _keyword_in_text(str(kw), full_text)]
    focus_hits = [kw for kw in focus_keywords if _keyword_in_text(str(kw), full_text)]
    background_hits = [kw for kw in background_keywords if _keyword_in_text(str(kw), full_text)]

    short_alias_hits = [kw for kw in raw_direct_hits if _is_short_english_alias(str(kw))]
    strong_direct_hits = [kw for kw in raw_direct_hits if not _is_short_english_alias(str(kw))]

    title_hits = [kw for kw in strong_direct_hits if _keyword_in_text(str(kw), title_text)]
    meta_hits = [kw for kw in strong_direct_hits if _keyword_in_text(str(kw), meta_text)]
    code_hits = [kw for kw in strong_direct_hits if _is_numeric_code_keyword(str(kw))]

    score = 0
    reasons: List[str] = []

    if title_hits:
        score += 70
        reasons.append("title_company_identity_hit")
    if meta_hits:
        score += 80
        reasons.append("company_metadata_hit")
    if code_hits:
        score += 60
        reasons.append("security_code_hit")
    if strong_direct_hits and len(strong_direct_hits) >= 2:
        score += 40
        reasons.append("multiple_runtime_company_hits")
    elif strong_direct_hits:
        score += 25
        reasons.append("single_body_company_hit")

    # Short all-letter aliases are accepted only when a stronger runtime identity also appears.
    if short_alias_hits and strong_direct_hits:
        score += 10
        reasons.append("short_alias_cooccurs_with_company_identity")
    elif short_alias_hits and not strong_direct_hits:
        return {
            "match_level": "none",
            "direct_keywords": [],
            "focus_keywords": _dedupe_keep_order(focus_hits),
            "background_keywords": [],
            "matched_keywords": [],
            "match_score": 0,
            "match_reasons": ["rejected_short_english_alias_only"],
            "rejected_keywords": _dedupe_keep_order(short_alias_hits),
        }

    if focus_hits:
        score += min(20, 5 * len(focus_hits))
        reasons.append("research_focus_hit")

    if score >= 60:
        level = "company_direct"
    elif background_hits:
        level = "sector_background"
    else:
        level = "none"

    return {
        "match_level": level,
        "direct_keywords": _dedupe_keep_order(strong_direct_hits + short_alias_hits if level == "company_direct" else strong_direct_hits),
        "focus_keywords": _dedupe_keep_order(focus_hits),
        "background_keywords": _dedupe_keep_order(background_hits),
        "matched_keywords": _dedupe_keep_order(strong_direct_hits + short_alias_hits + focus_hits + background_hits),
        "match_score": score,
        "match_reasons": reasons,
        "rejected_keywords": [],
    }


def _row_identity(row: Dict[str, Any]) -> str:
    for key in (
        "id", "cmnt_hcode", "rpt_hcode", "roadshow_id", "recordId", "record_id",
        "chunk_id", "chunkIds",
    ):
        val = row.get(key)
        if val is not None and str(val).strip():
            return f"{key}:{val}"
    title = _row_title(row)
    date = _row_date(row)
    src = _row_source(row)
    return f"fallback:{title}|{date}|{src}|{hash(_row_text(row)[:1000])}"


def _extract_rows(raw: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[int], Optional[bool]]:
    data = raw.get("data")
    total_count: Optional[int] = None
    has_more: Optional[bool] = None
    rows: Any = None

    if isinstance(data, dict):
        total_count = data.get("count") if isinstance(data.get("count"), int) else None
        has_more = data.get("hasMore") if isinstance(data.get("hasMore"), bool) else None
        for key in ("data", "list", "records", "rows"):
            if isinstance(data.get(key), list):
                rows = data.get(key)
                break
    elif isinstance(data, list):
        rows = data

    if rows is None:
        for key in ("rows", "records", "list", "data"):
            if isinstance(raw.get(key), list):
                rows = raw.get(key)
                break

    if not isinstance(rows, list):
        rows = []

    normalized: List[Dict[str, Any]] = []
    for item in rows:
        normalized.append(item if isinstance(item, dict) else {"value": item})
    return normalized, total_count, has_more


# =============================================================================
# Date / pagination helpers
# =============================================================================

def _parse_dt(value: Any) -> Optional[datetime]:
    text = _normalize_text(value).strip()
    if not text:
        return None

    text = text.replace("T", " ")
    text = re.sub(r"\.\d+", "", text)
    text = text.rstrip("Z")

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ):
        try:
            length = 19 if "%S" in fmt else 16 if "%M" in fmt else 10
            return datetime.strptime(text[:length], fmt)
        except Exception:
            pass

    return None


def _max_row_datetime(rows: List[Dict[str, Any]]) -> Optional[datetime]:
    best: Optional[datetime] = None
    for row in rows:
        for key in DATE_FIELD_CANDIDATES:
            dt = _parse_dt(row.get(key))
            if dt and (best is None or dt > best):
                best = dt
    return best


def _format_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _default_start_time() -> str:
    return os.getenv("ALPHA_PAI_DEFAULT_START_TIME", "")


def _default_size() -> int:
    return _int_env("ALPHA_PAI_DEFAULT_SIZE", 10)


def _default_fetch_size() -> int:
    return _int_env("ALPHA_PAI_FETCH_SIZE", 500)


def _default_max_pages() -> int:
    return _int_env("ALPHA_PAI_MAX_PAGES", 30)


def _default_target_direct() -> int:
    return _int_env("ALPHA_PAI_TARGET_DIRECT_ROWS", 10)


# =============================================================================
# Provider-side query / pushdown
# =============================================================================

def build_company_pushdown_candidates(
    *,
    stock_name: str,
    stock_code: str,
    direct_keywords: List[str],
) -> List[Dict[str, Any]]:
    """
    Candidate param variants. Each is smoke-tested because docs may only guarantee
    feed params. No issuer-specific candidate is stored here.
    """
    name = (stock_name or "").strip()
    stripped_name = _strip_security_name_prefixes(name)
    display_code = normalize_display_code(stock_code)
    full_code = normalize_full_code(stock_code)
    code = _code_num(stock_code)

    preferred_name = stripped_name or name
    aliases = [x for x in direct_keywords if isinstance(x, str) and x.strip()]
    keyword = preferred_name or (aliases[0] if aliases else "") or code

    candidates: List[Dict[str, Any]] = []

    if keyword:
        candidates.append({"keyword": keyword})
        candidates.append({"query": keyword})
        candidates.append({"search_key": keyword})
        candidates.append({"search_text": keyword})

    if code:
        candidates.extend([
            {"stock_code": code},
            {"sec_code": code},
            {"code": code},
            {"security_code": code},
        ])

    if display_code:
        candidates.extend([
            {"stock_code": display_code},
            {"sec_code": display_code},
            {"full_code": display_code},
            {"security_code": display_code},
        ])

    if full_code:
        candidates.extend([
            {"stock_code": full_code},
            {"sec_code": full_code},
            {"full_code": full_code},
            {"security_code": full_code},
        ])

    for candidate_name in _dedupe_keep_order([name, stripped_name]):
        if candidate_name:
            candidates.extend([
                {"stock_name": candidate_name},
                {"sec_name": candidate_name},
                {"company": candidate_name},
                {"company_name": candidate_name},
            ])

    combo: Dict[str, Any] = {}
    if keyword:
        combo["keyword"] = keyword
    if code:
        combo["stock_code"] = code
    if combo:
        candidates.append(combo)

    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for c in candidates:
        key = json.dumps(c, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


async def query_data_api(
    *,
    api_name: str,
    params: Dict[str, Any],
    fields: Optional[List[str]] = None,
) -> Dict[str, Any]:
    base_url = os.getenv("ALPHA_PAI_BASE_URL", "https://open-api.rabyte.cn").rstrip("/")
    app_id = os.getenv("ALPHA_PAI_APP_ID", "").strip()
    app_secret = os.getenv("ALPHA_PAI_APP_SECRET", "").strip()
    timeout_s = _int_env("ALPHA_PAI_TIMEOUT_SECONDS", 60)

    if not app_id:
        raise RuntimeError("Missing ALPHA_PAI_APP_ID")
    if not base_url:
        raise RuntimeError("Missing ALPHA_PAI_BASE_URL")

    url = f"{base_url}/alpha/open-api/v1/data-manager/query"
    payload = {
        "apiName": api_name,
        "params": params,
        "fields": fields or [],
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    headers = {
        "Content-Type": "application/json",
        "app-agent": app_id,
    }

    if app_secret:
        headers["sign"] = hmac.new(
            app_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, headers=headers, content=body.encode("utf-8"))

    if resp.status_code >= 400:
        raise RuntimeError(
            f"Alpha Pai HTTP {resp.status_code} for {api_name}: {resp.text[:800]}"
        )

    try:
        raw = resp.json()
    except Exception as exc:
        raise RuntimeError(f"Alpha Pai non-JSON response for {api_name}: {resp.text[:800]}") from exc

    code = raw.get("code")
    if code not in (None, 200000, "200000", 0, "0"):
        msg = raw.get("msg") or raw.get("message") or raw.get("error") or "unknown"
        raise RuntimeError(
            f"Alpha Pai API error for {api_name}: code={code}, msg={msg}, raw={_json_preview(raw, 1000)}"
        )

    return raw


def _base_params(
    start_time: Optional[str],
    end_time: Optional[str],
    fetch_n: int,
    extra_params: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "start_time": start_time or _default_start_time(),
        "end_time": end_time or "",
        "size": fetch_n,
    }
    if extra_params:
        params.update({k: v for k, v in extra_params.items() if v is not None and v != ""})
    return params


# =============================================================================
# Main ability runner
# =============================================================================

# =============================================================================
# Alpha Pai Agent / Skill API runner
# =============================================================================

def _stock_obj(stock_name: str, stock_code: str) -> Dict[str, str]:
    return {
        "code": (stock_code or "").strip().upper(),
        "name": (stock_name or "").strip(),
    }


def build_alpha_agent_payload(
    ability: str,
    *,
    stock_name: str = "",
    stock_code: str = "",
    question: str = "",
    template: int = 0,
    template_text: str = "",
    template_concern: str = "",
    input_industry: str = "",
    report_type: str = "",
    stock_report_id: str = "",
    stock_report_title: str = "",
    stock_report_period: str = "",
    request_select_start_time: str = "",
    request_select_end_time: str = "",
    language: str = "中文",
    only_answer: bool = False,
) -> Dict[str, Any]:
    if ability not in ALPHA_AGENT_ABILITIES:
        raise ValueError(f"Unsupported AlphaPai agent ability: {ability}")

    spec = ALPHA_AGENT_ABILITIES[ability]
    mode = int(spec["agentMode"])

    stock_name = (stock_name or "").strip()
    stock_code = (stock_code or "").strip().upper()
    input_industry = (input_industry or "").strip()
    template_text = (template_text or "").strip()
    template_concern = (template_concern or "").strip()
    question = (question or "").strip()

    if not question:
        if ability == "alpha_stock_performance_review":
            question = f"{stock_name}{stock_report_period or '最新报告期'}业绩点评"
        elif ability == "alpha_company_one_pager":
            question = f"{stock_name}（{stock_code}）的公司一页纸"
        elif ability == "alpha_research_outline":
            question = f"{stock_name}（{stock_code}）的调研问题大纲"
        elif ability == "alpha_investment_logic":
            question = f"{stock_name}（{stock_code}）的公司投资逻辑"
        elif ability == "alpha_comparable_companies":
            question = f"{stock_name}（{stock_code}）的可比公司"
        elif ability == "alpha_viewpoint_challenge":
            question = f"Challenge该观点：{template_text or template_concern or stock_name}"
        elif ability == "alpha_industry_one_pager":
            question = f"{input_industry or stock_name}的行业一页纸"
        elif ability == "alpha_theme_stock":
            question = template_text or f"和{input_industry or stock_name}相关的公司"

    payload: Dict[str, Any] = {
        "agentMode": mode,
        "question": question,
        "questionId": str(uuid.uuid4()),
        "userInfo": _alpha_user_info(),
        "template": int(template or 0),
        "templateText": template_text,
    }

    if spec.get("needs_stock"):
        if not stock_name or not stock_code:
            raise ValueError(f"{ability} requires stock_name and stock_code")
        payload["stock"] = _stock_obj(stock_name, stock_code)

    if ability == "alpha_stock_performance_review":
        missing = [
            name
            for name, val in {
                "report_type": report_type,
                "stock_report_id": stock_report_id,
                "stock_report_title": stock_report_title,
                "stock_report_period": stock_report_period,
            }.items()
            if not val
        ]
        if missing:
            raise ValueError(
                "个股业绩点评 requires report fields. Missing: " + ", ".join(missing)
            )

        payload.update({
            "reportType": report_type,
            "stockReportId": stock_report_id,
            "stockReportTitle": stock_report_title,
            "stockReportPeriod": stock_report_period,
            "templateConcern": template_concern,
        })

    if ability == "alpha_investment_logic":
        payload["onlyAnswer"] = bool(only_answer)

    if ability == "alpha_viewpoint_challenge":
        payload["template"] = 1
        payload["templateText"] = template_text or question.replace("Challenge该观点：", "")
        if template_concern:
            payload["templateConcern"] = template_concern
        if request_select_start_time:
            payload["requestSelectStartTime"] = request_select_start_time
        if request_select_end_time:
            payload["requestSelectEndTime"] = request_select_end_time

    if ability == "alpha_industry_one_pager":
        if not input_industry:
            raise ValueError("行业一页纸 requires input_industry")
        payload["inputIndustry"] = input_industry

    if ability == "alpha_company_one_pager" and language:
        payload["language"] = language

    return payload


def _alpha_pai_report_code_variants(stock_code: str) -> List[str]:
    code = (stock_code or "").strip().upper()
    variants: List[str] = []

    def add(x: str) -> None:
        x = (x or "").strip()
        if x and x not in variants:
            variants.append(x)

    add(code)

    # HK formats
    if re.fullmatch(r"\d{4,5}\.HK", code):
        num = code.split(".", 1)[0]
        add(f"{num}.HK")
        add(f"{num.zfill(5)}.HK")
        add(f"HK{num}")
        add(f"HK{num.zfill(5)}")
        add(f"hk{num}")
        add(f"hk{num.zfill(5)}")
        add(num)
        add(num.zfill(5))

    if re.fullmatch(r"HK\d{4,5}", code):
        num = code[2:]
        add(f"{num}.HK")
        add(f"{num.zfill(5)}.HK")
        add(f"HK{num.zfill(5)}")
        add(num)
        add(num.zfill(5))

    # A-share formats
    if re.fullmatch(r"\d{6}\.(SH|SZ)", code):
        num, market = code.split(".", 1)
        add(code)
        add(f"{market}{num}")
        add(f"{market.lower()}{num}")
        add(num)

    if re.fullmatch(r"(SH|SZ)\d{6}", code):
        market = code[:2]
        num = code[2:]
        add(f"{num}.{market}")
        add(num)

    return variants


def _extract_stock_report_rows(obj: Any) -> List[Dict[str, Any]]:
    """
    Extract AlphaPai stock-report rows from common nested response shapes.
    """
    candidates: List[Any] = []

    def add_candidate(x: Any) -> None:
        if x is not None:
            candidates.append(x)

    add_candidate(obj)

    if isinstance(obj, dict):
        add_candidate(obj.get("data"))
        add_candidate(obj.get("result"))
        add_candidate(obj.get("reports"))
        add_candidate(obj.get("rows"))
        add_candidate(obj.get("list"))
        add_candidate(obj.get("records"))

        raw = obj.get("raw")
        if isinstance(raw, dict):
            add_candidate(raw)
            add_candidate(raw.get("data"))
            add_candidate(raw.get("result"))
            add_candidate(raw.get("reports"))
            add_candidate(raw.get("rows"))
            add_candidate(raw.get("list"))
            add_candidate(raw.get("records"))

    for c in list(candidates):
        if isinstance(c, dict):
            for key in ["data", "result", "reports", "rows", "list", "records"]:
                if c.get(key) is not None:
                    add_candidate(c.get(key))

    for c in candidates:
        if isinstance(c, list):
            rows = [x for x in c if isinstance(x, dict)]
            if rows:
                return rows

    return []


async def fetch_stock_reports(stock_code: str, stock_name: str = "") -> Dict[str, Any]:
    attempts: List[Dict[str, Any]] = []

    for code in _alpha_pai_report_code_variants(stock_code):
        payload: Dict[str, Any] = {"code": code}

        # Some providers ignore name, but it is useful to include for debugging/future compatibility.
        if stock_name:
            payload["name"] = stock_name.strip()

        result = await _post_alpha_json_or_sse(
            "/alpha/open-api/v1/paipai/stock/report",
            payload,
            timeout=120,
        )

        rows = _extract_stock_report_rows(result)

        attempts.append({
            "code": code,
            "payload": payload,
            "rows_count": len(rows),
            "provider_code": result.get("code") if isinstance(result, dict) else None,
            "provider_message": result.get("message") if isinstance(result, dict) else None,
            "raw_preview": result,
        })

        if rows:
            return {
                "ok": True,
                "matched_code": code,
                "attempted_codes": [a["code"] for a in attempts],
                "rows_count": len(rows),
                "reports": rows,
                "attempts": attempts,
            }

    return {
        "ok": True,
        "matched_code": "",
        "attempted_codes": [a["code"] for a in attempts],
        "rows_count": 0,
        "reports": [],
        "attempts": attempts,
    }


async def run_ability(
    ability: str,
    *,
    stock_name: str = "",
    stock_code: str = "",
    question: str = "",
    template: int = 0,
    template_text: str = "",
    template_concern: str = "",
    input_industry: str = "",
    language: str = "中文",
    only_answer: bool = False,
    report_type: str = "",
    stock_report_id: str = "",
    stock_report_title: str = "",
    stock_report_period: str = "",
    request_select_start_time: str = "",
    request_select_end_time: str = "",
    **_ignored: Any,
) -> Dict[str, Any]:
    refresh_ability_to_api_name()

    if ability not in ALPHA_AGENT_ABILITIES:
        raise ValueError(f"Unsupported Alpha Pai Agent ability: {ability}")

    payload = build_alpha_agent_payload(
        ability,
        stock_name=stock_name,
        stock_code=stock_code,
        question=question,
        template=template,
        template_text=template_text,
        template_concern=template_concern,
        input_industry=input_industry,
        language=language,
        only_answer=only_answer,
        report_type=report_type,
        stock_report_id=stock_report_id,
        stock_report_title=stock_report_title,
        stock_report_period=stock_report_period,
        request_select_start_time=request_select_start_time,
        request_select_end_time=request_select_end_time,
    )

    started = time.time()
    result = await _post_alpha_json_or_sse(
        "/alpha/open-api/v1/paipai/stock/agent",
        payload,
        timeout=int(os.getenv("ALPHA_PAI_TIMEOUT_SECONDS", "300")),
    )

    return {
        "ability": ability,
        "label": ALPHA_AGENT_ABILITIES[ability]["label"],
        "api_name": "/alpha/open-api/v1/paipai/stock/agent",
        "agent_mode": ALPHA_AGENT_ABILITIES[ability]["agentMode"],
        "payload": payload,
        "answer_text": result.get("answer_text", ""),
        "references": result.get("references", []),
        "raw": result,
        "elapsed_seconds": round(time.time() - started, 2),
    }

def build_pack_from_results(
    jobs_or_results: List[Dict[str, Any]],
    *,
    stock_name: str = "",
    stock_code: str = "",
) -> str:
    title_target = f"{stock_name} {stock_code}".strip()

    lines: List[str] = [
        f"# Alpha Pai Agent / Skill API 底稿：{title_target}",
        "",
        "说明：本底稿来自 AlphaPai 机构 Skill / Stock Agent API，不是旧的五类数据流 feed。",
        "可用能力包括：公司一页纸、个股业绩点评、个股调研大纲、投资逻辑、观点Challenge、可比公司、行业一页纸。",
        "",
        "---",
        "",
    ]

    if not jobs_or_results:
        lines += [
            "## Alpha Pai Agent 数据状态",
            "",
            "没有可组装的 Alpha Pai Agent job/result。请先运行 Alpha Pai Agent 按钮。",
            "",
        ]
        return "\n".join(lines).strip() + "\n"

    for item in jobs_or_results:
        status = item.get("status", "finished")
        ability = item.get("ability", "")
        label = item.get("label") or ABILITY_LABELS.get(ability, ability or "Alpha Pai Agent")
        api_name = item.get("api_name") or ABILITY_TO_API_NAME.get(ability, "")
        agent_mode = item.get("agent_mode", "")
        answer_text = item.get("answer_text") or ""
        error = item.get("error") or ""

        lines += [
            f"## {label}",
            "",
            f"- ability: `{ability}`",
            f"- apiName: `{api_name}`",
            f"- agentMode: `{agent_mode}`",
            f"- 状态: `{status}`",
            f"- elapsed_seconds: `{item.get('elapsed_seconds', '')}`",
            "",
        ]

        if error:
            lines += [
                "### 调用失败",
                "",
                error,
                "",
                "---",
                "",
            ]
            continue

        if answer_text:
            lines += [
                "### Agent 输出",
                "",
                answer_text.strip(),
                "",
            ]
        else:
            lines += [
                "### Agent 输出",
                "",
                "本 Agent 未返回可提取的 answer_text。请检查 raw 字段或供应商返回结构。",
                "",
            ]

        references = item.get("references") or []
        if references:
            lines += [
                "### References",
                "",
                f"- references_count: {len(references)}",
                "",
            ]

        lines += ["---", ""]

    return "\n".join(lines).strip() + "\n"