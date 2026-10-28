from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

ALPHA_PAI_ABILITY_MAP: Dict[str, Dict[str, str]] = {
    "alpha_comments": {"api_name": "get_cmnt_info_client_mgc", "label": "Alpha Pai 点评数据"},
    "alpha_roadshows": {"api_name": "get_summary_roadshow_info_mgc", "label": "Alpha Pai 路演/纪要"},
    "alpha_domestic_reports": {"api_name": "get_nxny_report_main_mgc", "label": "Alpha Pai 内资研报"},
    "alpha_wechat_articles": {"api_name": "get_research_wechat_articles_mgc", "label": "Alpha Pai 公众号/资讯"},
    "alpha_foreign_reports": {"api_name": "get_report_main_us_mgc", "label": "Alpha Pai 外资研报"},
}

def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1","true","yes","y","on"}

def _env_int(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)).strip())
    except Exception: return default

def alpha_pai_enabled() -> bool:
    return _env_bool("ALPHA_PAI_ENABLED", True)

def now_ts() -> float:
    return time.time()

def normalize_stock_code(stock_code: str) -> str:
    return (stock_code or "").strip().upper()

def normalize_full_code(stock_code: str) -> str:
    code = (stock_code or "").strip()
    if not code: return ""
    low = code.lower()
    if re.fullmatch(r"(sz|sh)\d{6}", low): return low
    up = code.upper()
    if up.endswith(".SZ"): return "sz" + up[:-3]
    if up.endswith(".SH"): return "sh" + up[:-3]
    if re.fullmatch(r"\d{6}", code):
        return ("sz" if code.startswith(("0","3")) else "sh") + code
    return low

def _text(value: Any) -> str:
    if value is None: return ""
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False)
    return str(value)

def _dedupe_keep_order(items: List[str]) -> List[str]:
    out, seen = [], set()
    for item in items:
        item = (item or "").strip()
        if not item or item in {"[", "]", '"', "'", ",", "，", "、"}:
            continue
        if item not in seen:
            seen.add(item); out.append(item)
    return out

def split_keywords(value: Optional[Any]) -> List[str]:
    if not value: return []
    if isinstance(value, list):
        merged: List[str] = []
        for x in value: merged.extend(split_keywords(x))
        return _dedupe_keep_order(merged)
    return _dedupe_keep_order(re.split(r"[\n,，;；、|]+", str(value)))

def build_keyword_groups(stock_name: str, stock_code: str, extra_keywords: Optional[Any]=None) -> Tuple[List[str], List[str]]:
    stock_name = (stock_name or "").strip()
    code_up = normalize_stock_code(stock_code)
    code_num = code_up.split(".")[0] if "." in code_up else code_up
    full_code = normalize_full_code(stock_code)
    is_catl = stock_name == "宁德时代" or code_num == "300750" or full_code == "sz300750"

    direct = _dedupe_keep_order([
        stock_name, code_up, code_num, full_code, full_code.upper() if full_code else "",
        "宁德时代新能源科技股份有限公司" if is_catl else "",
        "CATL" if is_catl else "",
        "Contemporary Amperex" if is_catl else "",
    ])
    background = _dedupe_keep_order([
        "动力电池", "储能电池", "储能", "锂电", "钠电池", "固态电池",
        "麒麟电池", "神行", "超充", "算电协同",
    ] + split_keywords(extra_keywords))
    background = [x for x in background if x not in set(direct)]
    return direct, background

def row_text(row: Dict[str, Any]) -> str:
    fields = [
        "title","content","summary","abstract","digest","stock_name","stock_code","stock_names","stock_codes",
        "sec_name","sec_code","s_info_name","s_info_windcode","inst_cname","team_cname","psn_name",
        "author","analyst","report_title","report_content","roadshow_title",
    ]
    parts = [_text(row.get(k)) for k in fields if row.get(k) is not None]
    return "\n".join(parts) if parts else _text(row)

def match_row(row: Dict[str, Any], direct_keywords: List[str], background_keywords: List[str]) -> Dict[str, Any]:
    txt = row_text(row)
    direct_hits = [kw for kw in direct_keywords if kw and kw in txt]
    background_hits = [kw for kw in background_keywords if kw and kw in txt]
    if direct_hits: level = "company_direct"
    elif background_hits: level = "sector_background"
    else: level = "none"
    return {"match_level": level, "direct_keywords": direct_hits, "background_keywords": background_hits, "matched_keywords": direct_hits + background_hits}

def row_title(row: Dict[str, Any]) -> str:
    for k in ("title","report_title","roadshow_title","summary","abstract"):
        if row.get(k): return _text(row.get(k)).strip().replace("\n"," ")[:160]
    return "Untitled"

def row_date(row: Dict[str, Any]) -> str:
    for k in ("hcreatetime","cmnt_date","publish_date","report_date","date","hupdatetime","create_time"):
        if row.get(k): return _text(row.get(k))
    return ""

def row_source(row: Dict[str, Any]) -> str:
    for k in ("inst_cname","team_cname","source","publisher","org_name"):
        if row.get(k): return _text(row.get(k))
    return ""

def row_author(row: Dict[str, Any]) -> str:
    for k in ("psn_name","author","analyst","create_by"):
        if row.get(k): return _text(row.get(k))
    return ""

def row_content(row: Dict[str, Any]) -> str:
    for k in ("content","summary","abstract","digest","report_content"):
        if row.get(k): return _text(row.get(k))
    return row_text(row)

@dataclass
class AlphaPaiQueryRequest:
    ability: str
    stock_name: str
    stock_code: str
    start_time: str = ""
    end_time: str = ""
    size: int = 10
    fetch_size: int = 80
    fields: List[str] = field(default_factory=list)
    keywords: Optional[Any] = None
    extra_params: Dict[str, Any] = field(default_factory=dict)

class AlphaPaiProvider:
    def __init__(self) -> None:
        self.base_url = os.getenv("ALPHA_PAI_BASE_URL", "https://open-api.rabyte.cn").rstrip("/")
        self.app_id = os.getenv("ALPHA_PAI_APP_ID", "").strip()
        self.timeout_seconds = _env_int("ALPHA_PAI_TIMEOUT_SECONDS", 60)
        self.max_content_chars = _env_int("ALPHA_PAI_MAX_CONTENT_CHARS", 4000)
        self.default_start_time = os.getenv("ALPHA_PAI_DEFAULT_START_TIME", "2026-01-01 00:00:00")
        self.default_size = _env_int("ALPHA_PAI_DEFAULT_SIZE", 10)
        self.default_fetch_size = _env_int("ALPHA_PAI_FETCH_SIZE", 120)

    def _check_env(self) -> None:
        if not self.base_url: raise RuntimeError("Missing ALPHA_PAI_BASE_URL")
        if not self.app_id: raise RuntimeError("Missing ALPHA_PAI_APP_ID")

    def ability_meta(self, ability: str) -> Dict[str, str]:
        if ability not in ALPHA_PAI_ABILITY_MAP:
            raise ValueError(f"Unsupported Alpha Pai ability: {ability}")
        return ALPHA_PAI_ABILITY_MAP[ability]

    async def query(self, req: AlphaPaiQueryRequest) -> Dict[str, Any]:
        self._check_env()
        meta = self.ability_meta(req.ability)
        api_name = meta["api_name"]
        fetch_size = int(req.fetch_size or self.default_fetch_size)
        output_size = int(req.size or self.default_size)
        payload = {
            "apiName": api_name,
            "params": {"start_time": req.start_time or self.default_start_time, "end_time": req.end_time or "", "size": fetch_size} | (req.extra_params or {}),
            "fields": req.fields or [],
        }
        headers = {"Content-Type": "application/json", "app-agent": self.app_id}
        started = now_ts()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            resp = await client.post(f"{self.base_url}/alpha/open-api/v1/data-manager/query", headers=headers, json=payload)
            raw_text = resp.text
            resp.raise_for_status()
            obj = resp.json()
        elapsed = round(now_ts() - started, 2)
        code = obj.get("code")
        if code not in (200000, "200000", 0, "0", None):
            raise RuntimeError(f"Alpha Pai API returned code={code}: {obj}")
        data_obj = obj.get("data") or {}
        rows = data_obj.get("data") or []
        if not isinstance(rows, list): rows = []
        direct_kw, background_kw = build_keyword_groups(req.stock_name, req.stock_code, req.keywords)

        company_rows, background_rows, unmatched_rows = [], [], []
        for row in rows:
            if not isinstance(row, dict): continue
            enriched = dict(row)
            enriched.update(match_row(enriched, direct_kw, background_kw))
            if enriched["match_level"] == "company_direct": company_rows.append(enriched)
            elif enriched["match_level"] == "sector_background": background_rows.append(enriched)
            else: unmatched_rows.append(enriched)

        selected_company = company_rows[:output_size]
        selected_background = background_rows[:max(0, output_size - len(selected_company))]
        markdown = build_alpha_markdown_section(
            label=meta["label"], api_name=api_name, stock_name=req.stock_name, stock_code=req.stock_code,
            rows_count=len(rows), source_total_count=data_obj.get("count"), source_has_more=data_obj.get("hasMore"),
            elapsed_seconds=elapsed, company_rows=selected_company, background_rows=selected_background,
            company_direct_count=len(company_rows), sector_background_count=len(background_rows),
            max_content_chars=self.max_content_chars,
        )
        return {
            "ok": True, "ability": req.ability, "label": meta["label"], "api_name": api_name,
            "stock_name": req.stock_name, "stock_code": req.stock_code, "full_code": normalize_full_code(req.stock_code),
            "payload": payload, "elapsed_seconds": elapsed, "rows_count": len(rows),
            "matched_rows_count": len(company_rows)+len(background_rows),
            "company_direct_count": len(company_rows), "sector_background_count": len(background_rows),
            "selected_rows_count": len(selected_company)+len(selected_background),
            "source_total_count": data_obj.get("count"), "source_has_more": data_obj.get("hasMore"),
            "direct_keywords": direct_kw, "background_keywords": background_kw,
            "rows": rows[:min(len(rows), fetch_size)], "company_rows": company_rows[:output_size],
            "background_rows": background_rows[:output_size], "selected_rows": selected_company+selected_background,
            "answer_text": markdown, "markdown": markdown, "raw_preview": raw_text[:1000],
        }

def _append_row_markdown(lines: List[str], row: Dict[str, Any], idx: int, max_content_chars: int) -> None:
    lines.append(f"### {idx}. {row_title(row)}")
    if row_date(row): lines.append(f"- 日期: {row_date(row)}")
    if row_source(row): lines.append(f"- 机构/来源: {row_source(row)}")
    if row_author(row): lines.append(f"- 作者/人员: {row_author(row)}")
    lines.append(f"- 匹配级别: {row.get('match_level') or 'unknown'}")
    if row.get("direct_keywords"): lines.append(f"- 公司直接命中关键词: {', '.join(map(str, row.get('direct_keywords') or []))}")
    if row.get("background_keywords"): lines.append(f"- 行业背景命中关键词: {', '.join(map(str, row.get('background_keywords') or []))}")
    lines.append("")
    content = row_content(row).strip()
    if content:
        lines.append(content[:max_content_chars] + ("\n...(truncated)" if len(content) > max_content_chars else ""))
    else:
        lines.append("无正文内容。")
    lines.append("")

def build_alpha_markdown_section(*, label: str, api_name: str, stock_name: str, stock_code: str, rows_count: int, source_total_count: Any, source_has_more: Any, elapsed_seconds: float, company_rows: List[Dict[str, Any]], background_rows: List[Dict[str, Any]], company_direct_count: int, sector_background_count: int, max_content_chars: int=4000) -> str:
    lines = [
        f"## {label}", "",
        f"- apiName: `{api_name}`", f"- 对象: {stock_name} {stock_code}", f"- 拉取行数: {rows_count}",
        f"- 公司直接命中总数: {company_direct_count}", f"- 行业背景命中总数: {sector_background_count}",
        f"- 源数据总数 count: {source_total_count}", f"- hasMore: {source_has_more}", f"- elapsed_seconds: {elapsed_seconds}", "",
    ]
    if not company_rows and not background_rows:
        lines += ["> 本次 Alpha Pai 拉取结果没有命中公司关键词或行业背景关键词。该数据源本轮不应作为结论依据。", ""]
        return "\n".join(lines)
    if company_rows:
        lines += ["### A. 公司直接命中材料", ""]
        for i, row in enumerate(company_rows, 1): _append_row_markdown(lines, row, i, max_content_chars)
    if background_rows:
        lines += ["### B. 行业背景材料（仅可作为行业环境参考）", "", "> 以下材料未直接命中公司名称/代码，只命中行业背景关键词。不能直接作为公司结论依据。", ""]
        for i, row in enumerate(background_rows, 1): _append_row_markdown(lines, row, i, max_content_chars)
    return "\n".join(lines)

def build_alpha_pack(*, stock_name: str, stock_code: str, job_results: List[Dict[str, Any]]) -> str:
    lines = [
        f"# Alpha Pai Data API 底稿：{stock_name} {stock_code}", "",
        "说明：Alpha Pai 当前按 Data API feed 拉取数据，backend 会按公司名称、股票代码和相关关键词进行本地过滤。",
        "本底稿严格区分：A. 公司直接命中材料；B. 行业背景材料。",
        "只有公司直接命中材料可作为公司层面结论依据；行业背景材料只能用于行业环境、情绪和横向背景。", "",
    ]
    total_direct = sum(int(x.get("company_direct_count") or 0) for x in job_results)
    total_background = sum(int(x.get("sector_background_count") or 0) for x in job_results)
    lines += ["## Alpha Pai 数据质量总览", f"- 公司直接命中总数: {total_direct}", f"- 行业背景命中总数: {total_background}"]
    if total_direct == 0: lines.append("- 质量提醒: 本轮 Alpha Pai 未找到公司直接命中材料，不能作为公司结论主证据。")
    lines.append("")
    for result in job_results:
        lines += ["---", ""]
        if result.get("error"):
            lines += [f"## {result.get('label') or result.get('ability')}", "", f"ERROR: {result.get('error')}", ""]
        else:
            lines += [result.get("answer_text") or result.get("markdown") or "", ""]
    return "\n".join(lines).strip() + "\n"
