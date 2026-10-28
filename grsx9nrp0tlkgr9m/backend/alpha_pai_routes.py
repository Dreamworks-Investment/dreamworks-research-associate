"""
FastAPI route installer for Alpha Pai Data API.

Install in backend/app.py after app creation and after auth setup:

    try:
        from .alpha_pai_routes import install_alpha_pai_routes
    except ImportError:
        from alpha_pai_routes import install_alpha_pai_routes

    install_alpha_pai_routes(app)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    from .providers.alpha_pai_provider import (
        ABILITY_LABELS,
        ABILITY_TO_API_NAME,
        build_pack_from_results,
        fetch_stock_reports,
        refresh_ability_to_api_name,
        run_ability,
    )
except ImportError:
    from providers.alpha_pai_provider import (
        ABILITY_LABELS,
        ABILITY_TO_API_NAME,
        build_pack_from_results,
        fetch_stock_reports,
        refresh_ability_to_api_name,
        run_ability,
    )


ALPHA_PAI_JOBS: Dict[str, Dict[str, Any]] = {}


class AlphaPaiJobRequest(BaseModel):
    ability: str = Field(
        ...,
        description=(
            "alpha_stock_performance_review / alpha_company_one_pager / "
            "alpha_research_outline / alpha_theme_stock / alpha_investment_logic / "
            "alpha_comparable_companies / alpha_viewpoint_challenge / alpha_industry_one_pager"
        ),
    )

    stock_name: str = ""
    stock_code: str = ""

    question: str = ""
    template: int = 0
    template_text: str = ""
    template_concern: str = ""

    input_industry: str = ""
    language: str = "中文"
    only_answer: bool = False

    report_type: str = ""
    stock_report_id: str = ""
    stock_report_title: str = ""
    stock_report_period: str = ""

    request_select_start_time: str = ""
    request_select_end_time: str = ""


class AlphaPaiPackRequest(BaseModel):
    stock_name: str = ""
    stock_code: str = ""

    # Existing workflow: assemble from already-run jobs.
    job_ids: Optional[List[str]] = None
    include_failed: bool = True

    # New workflow: if no job_ids, run missing/all five abilities and assemble.
    run_missing: bool = True
    abilities: Optional[List[str]] = None

    start_time: Optional[str] = None
    end_time: Optional[str] = None
    size: Optional[int] = Field(default=10, ge=1, le=100)
    fetch_size: Optional[int] = Field(default=500, ge=1, le=5000)

    fields: Optional[List[str]] = None
    keywords: Optional[Any] = None
    company_aliases: Optional[Any] = None
    research_focus: Optional[Any] = None
    extra_params: Optional[Dict[str, Any]] = None


class AlphaPaiReportListRequest(BaseModel):
    stock_name: str = ""
    stock_code: str


def _supported_abilities() -> List[str]:
    refresh_ability_to_api_name()
    return list(ABILITY_TO_API_NAME.keys())


def _latest_jobs_for_stock(stock_name: str, stock_code: str) -> List[Dict[str, Any]]:
    wanted_name = (stock_name or "").strip()
    wanted_code = (stock_code or "").strip().upper()

    by_ability: Dict[str, Dict[str, Any]] = {}
    for job in ALPHA_PAI_JOBS.values():
        if wanted_name and (job.get("stock_name") or "").strip() != wanted_name:
            continue
        if wanted_code and (job.get("stock_code") or "").strip().upper() != wanted_code:
            continue
        ability = job.get("ability")
        if not ability:
            continue
        prev = by_ability.get(ability)
        if prev is None or (job.get("created_at") or 0) > (prev.get("created_at") or 0):
            by_ability[ability] = job

    return [by_ability[a] for a in _supported_abilities() if a in by_ability]


async def _run_alpha_job(job_id: str, req: AlphaPaiJobRequest) -> None:
    job = ALPHA_PAI_JOBS.get(job_id)
    if not job:
        return

    job["status"] = "running"
    job["started_at"] = time.time()

    try:
        result = await run_ability(
            req.ability,
            stock_name=req.stock_name,
            stock_code=req.stock_code,
            question=req.question,
            template=req.template,
            template_text=req.template_text,
            template_concern=req.template_concern,
            input_industry=req.input_industry,
            language=req.language,
            only_answer=req.only_answer,
            report_type=req.report_type,
            stock_report_id=req.stock_report_id,
            stock_report_title=req.stock_report_title,
            stock_report_period=req.stock_report_period,
            request_select_start_time=req.request_select_start_time,
            request_select_end_time=req.request_select_end_time,
        )
        job.update(result)
        job["status"] = "finished"
        job["error"] = None
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
    finally:
        job["finished_at"] = time.time()
        if job.get("started_at"):
            job["elapsed_seconds"] = round(job["finished_at"] - job["started_at"], 2)


async def _run_alpha_job_inline(req: AlphaPaiJobRequest) -> Dict[str, Any]:
    """Create, run, store, and return one Alpha Pai job. Never raises."""
    refresh_ability_to_api_name()

    job_id = str(uuid.uuid4())
    now = time.time()
    job: Dict[str, Any] = {
        "job_id": job_id,
        "status": "queued",
        "ability": req.ability,
        "label": ABILITY_LABELS.get(req.ability, req.ability),
        "api_name": ABILITY_TO_API_NAME.get(req.ability),
        "stock_name": req.stock_name,
        "stock_code": req.stock_code,
        "created_at": now,
        "request": req.model_dump(),
    }
    ALPHA_PAI_JOBS[job_id] = job

    await _run_alpha_job(job_id, req)
    return ALPHA_PAI_JOBS[job_id]


def _summarize_pack_jobs(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "job_ids": [j.get("job_id") for j in jobs if j.get("job_id")],
        "abilities": [j.get("ability") for j in jobs],
        "succeeded_count": sum(1 for j in jobs if j.get("status") == "finished" and not j.get("error")),
        "failed_count": sum(1 for j in jobs if j.get("status") == "failed" or j.get("error")),
        "company_direct_count": sum(int(j.get("company_direct_count") or 0) for j in jobs),
        "sector_background_count": sum(int(j.get("sector_background_count") or 0) for j in jobs),
        "matched_rows_count": sum(int(j.get("matched_rows_count") or 0) for j in jobs),
        "rows_count": sum(int(j.get("rows_count") or 0) for j in jobs),
    }


def install_alpha_pai_routes(app: FastAPI) -> None:

    @app.get("/api/alpha-pai/health")
    async def alpha_pai_health():
        refresh_ability_to_api_name()
        return {
            "ok": True,
            "abilities": list(ABILITY_TO_API_NAME.keys()),
            "ability_to_api_name": dict(ABILITY_TO_API_NAME),
            "labels": dict(ABILITY_LABELS),
        }
    
    @app.post("/api/alpha-pai/stock-reports")
    async def alpha_pai_stock_reports(req: AlphaPaiReportListRequest) -> Dict[str, Any]:
        try:
            data = await fetch_stock_reports(req.stock_code, stock_name=req.stock_name)
            return {
                "ok": True,
                "stock_name": req.stock_name,
                "stock_code": req.stock_code,
                **data,
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))

    @app.post("/api/alpha-pai/jobs")
    async def create_alpha_pai_job(
        req: AlphaPaiJobRequest,
        background_tasks: BackgroundTasks,
    ):
        refresh_ability_to_api_name()

        if req.ability not in ABILITY_TO_API_NAME:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported Alpha Pai ability: {req.ability}. "
                    f"Supported: {list(ABILITY_TO_API_NAME.keys())}"
                ),
            )

        job_id = str(uuid.uuid4())
        now = time.time()
        ALPHA_PAI_JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "ability": req.ability,
            "label": ABILITY_LABELS.get(req.ability, req.ability),
            "api_name": ABILITY_TO_API_NAME.get(req.ability),
            "stock_name": req.stock_name,
            "stock_code": req.stock_code,
            "created_at": now,
            "request": req.model_dump(),
        }

        background_tasks.add_task(_run_alpha_job, job_id, req)

        return {
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/api/alpha-pai/jobs/{job_id}",
            "ability": req.ability,
            "api_name": ABILITY_TO_API_NAME.get(req.ability),
        }

    @app.get("/api/alpha-pai/jobs/{job_id}")
    async def get_alpha_pai_job(job_id: str):
        job = ALPHA_PAI_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Alpha Pai job not found: {job_id}")
        return job

    @app.post("/api/alpha-pai/pack")
    async def create_alpha_pai_pack(req: AlphaPaiPackRequest):
        refresh_ability_to_api_name()

        jobs: List[Dict[str, Any]] = []
        missing_job_ids: List[str] = []

        if req.job_ids:
            for jid in req.job_ids:
                job = ALPHA_PAI_JOBS.get(jid)
                if job:
                    jobs.append(job)
                else:
                    missing_job_ids.append(jid)
        else:
            jobs = _latest_jobs_for_stock(req.stock_name, req.stock_code)

        desired_abilities = req.abilities or _supported_abilities()
        desired_abilities = [a for a in desired_abilities if a in ABILITY_TO_API_NAME]

        existing_abilities = {j.get("ability") for j in jobs}

        # Assemble should not fail just because the user forgot to run all five buttons.
        # It will run missing abilities and include per-source failures in the pack.
        if req.run_missing:
            missing_abilities = [a for a in desired_abilities if a not in existing_abilities]
            inline_reqs = [
                AlphaPaiJobRequest(
                    ability=ability,
                    stock_name=req.stock_name,
                    stock_code=req.stock_code,
                )
                for ability in missing_abilities
            ]

            if inline_reqs:
                new_jobs = await asyncio.gather(
                    *[_run_alpha_job_inline(r) for r in inline_reqs],
                    return_exceptions=True,
                )
                for item in new_jobs:
                    if isinstance(item, Exception):
                        synthetic_id = str(uuid.uuid4())
                        failed_job = {
                            "job_id": synthetic_id,
                            "status": "failed",
                            "ability": "unknown",
                            "label": "Alpha Pai unknown",
                            "api_name": "",
                            "stock_name": req.stock_name,
                            "stock_code": req.stock_code,
                            "created_at": time.time(),
                            "error": str(item),
                        }
                        ALPHA_PAI_JOBS[synthetic_id] = failed_job
                        jobs.append(failed_job)
                    else:
                        jobs.append(item)

        if not req.include_failed:
            jobs = [j for j in jobs if j.get("status") == "finished" and not j.get("error")]

        order = {ability: idx for idx, ability in enumerate(_supported_abilities())}
        jobs.sort(key=lambda j: order.get(j.get("ability"), 999))

        pack = build_pack_from_results(
            jobs,
            stock_name=req.stock_name,
            stock_code=req.stock_code,
        )
        summary = _summarize_pack_jobs(jobs)

        return {
            "ok": True,
            "status": "finished",
            "stock_name": req.stock_name,
            "stock_code": req.stock_code,
            "missing_job_ids": missing_job_ids,
            **summary,
            "pack": pack,
            "pack_markdown": pack,
            "answer_text": pack,
            "text": pack,
        }
