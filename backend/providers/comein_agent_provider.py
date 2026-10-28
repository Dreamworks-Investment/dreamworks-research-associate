from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any, Dict

import httpx
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA


class ComeInAgentProvider:
    def __init__(
        self,
        *,
        appid: str,
        phone: str,
        public_key: str,
        private_key: str,
        token_url: str,
        chat_url: str,
        token_cache_seconds: int = 1800,
    ) -> None:
        self.appid = appid
        self.phone = phone
        self.public_key = public_key
        self.private_key = private_key
        self.token_url = token_url
        self.chat_url = chat_url
        self.token_cache_seconds = token_cache_seconds

        self._token: str | None = None
        self._token_expire_at: float = 0.0

    def _encrypt_userinfo(self, plain_text: str) -> str:
        public_pem = (
            "-----BEGIN PUBLIC KEY-----\n"
            + self.public_key.strip()
            + "\n-----END PUBLIC KEY-----"
        )
        public_key = RSA.import_key(public_pem)
        cipher = PKCS1_v1_5.new(public_key)
        encrypted_bytes = cipher.encrypt(plain_text.encode("utf-8"))
        return base64.b64encode(encrypted_bytes).decode("utf-8")

    @staticmethod
    def _report_type_label(report_type: str) -> tuple[str, str]:
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

        if rt == "H1":
            rt = "S1"

        return rt, mapping.get(rt, rt)

    async def get_token(self, force_refresh: bool = False) -> str:
        now = time.time()

        if not force_refresh and self._token and now < self._token_expire_at:
            return self._token

        if not self.token_url:
            raise RuntimeError("Missing ComeIn token_url")
        if not self.appid:
            raise RuntimeError("Missing ComeIn appid")
        if not self.phone:
            raise RuntimeError("Missing ComeIn phone")
        if not self.public_key:
            raise RuntimeError("Missing ComeIn public_key")

        plain_userinfo = json.dumps(
            {"phone": self.phone},
            ensure_ascii=False,
            separators=(",", ":"),
        )

        encrypted_userinfo = self._encrypt_userinfo(plain_userinfo)

        headers = {
            "app": "json",
            "mod": "user",
            "act": "orgchannellogin",
        }

        data = {
            "userInfo": encrypted_userinfo,
            "userChannel": self.appid,
        }

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                self.token_url,
                headers=headers,
                data=data,
            )
            resp.raise_for_status()
            obj = resp.json()

        token = obj.get("data", {}).get("token") or obj.get("data", {}).get("webtoken")

        if not token:
            raise RuntimeError(f"ComeIn token fetch failed: {obj}")

        self._token = token
        self._token_expire_at = time.time() + self.token_cache_seconds

        return token

    async def _post_agent(
        self,
        payload: Dict[str, Any],
        *,
        hard_deadline_seconds: float = 180.0,
        line_wait_seconds: float = 5.0,
    ) -> Dict[str, Any]:
        token = await self.get_token()

        headers = {
            "content-type": "text/plain;charset=UTF-8",
            "token": token,
        }

        timeout = httpx.Timeout(
            connect=40.0,
            read=600.0,
            write=40.0,
            pool=40.0,
        )

        started_at = time.time()

        events: list[dict[str, Any]] = []
        raw_lines: list[str] = []
        answer_parts: list[str] = []
        thinking_parts: list[str] = []
        parse_errors: list[str] = []

        last_status = ""
        saw_finish = False
        answer_started = False
        thinking_started = False
        tool_started = False
        document_search_started = False

        def remember_raw(line: str) -> None:
            # Keep diagnostics small. Do not store multi-MB raw lines.
            if len(raw_lines) < 30:
                raw_lines.append(line[:2000])

        def extract_message(data: Dict[str, Any]) -> str:
            for key in ("message", "text", "content", "answer", "output"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val

            # Some providers return nested payloads.
            result = data.get("result")
            if isinstance(result, dict):
                for key in ("message", "text", "content", "answer", "output"):
                    val = result.get(key)
                    if isinstance(val, str) and val:
                        return val

            return ""

        def handle_event(event: Dict[str, Any]) -> None:
            nonlocal last_status
            nonlocal saw_finish
            nonlocal answer_started
            nonlocal thinking_started
            nonlocal tool_started
            nonlocal document_search_started

            events.append(event)

            data = event.get("data", {})
            if not isinstance(data, dict):
                data = {}

            status = str(data.get("status") or "")
            message_type = str(data.get("messageType") or "")

            if status:
                last_status = status

            if status == "TOOL":
                tool_started = True

            if status == "DOCUMENT_SEARCH":
                document_search_started = True

            message = extract_message(data)

            if message_type == "TEXT":
                answer_started = True
                if message:
                    answer_parts.append(message)

            elif message_type == "THINK":
                thinking_started = True
                if message:
                    thinking_parts.append(message)

            elif message_type in ("THINK_START", "THINK_END"):
                thinking_started = True

            # Fallback:
            # Some ComeIn responses may not mark messageType correctly,
            # but status itself can imply THINK/TEXT-like content.
            elif message:
                if status in {"ANSWER", "TEXT", "FINISH"}:
                    answer_started = True
                    answer_parts.append(message)
                elif status in {"THINK", "PLANNING"}:
                    thinking_started = True
                    thinking_parts.append(message)

            if data.get("finish") is True or event.get("finish") is True:
                saw_finish = True

        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                self.chat_url,
                headers=headers,
                content=json.dumps(payload, ensure_ascii=False),
            ) as resp:
                resp.raise_for_status()

                line_iter = resp.aiter_lines()

                while time.time() - started_at < hard_deadline_seconds:
                    try:
                        line = await asyncio.wait_for(
                            line_iter.__anext__(),
                            timeout=line_wait_seconds,
                        )
                    except asyncio.TimeoutError:
                        # Do not break too early. Some ComeIn agents are quiet for long periods.
                        continue
                    except StopAsyncIteration:
                        break

                    if line is None:
                        continue

                    if line:
                        remember_raw(line)

                    if not line.startswith("data:"):
                        continue

                    raw = line[len("data:"):].strip()

                    if not raw:
                        continue

                    # Defensive parser:
                    # Large performance_review responses sometimes produce an incomplete
                    # or malformed JSON frame. This must not kill the whole job.
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        if len(parse_errors) < 10:
                            parse_errors.append(
                                f"{type(exc).__name__}: {exc}; raw_head={raw[:500]}"
                            )
                        continue
                    except Exception as exc:
                        if len(parse_errors) < 10:
                            parse_errors.append(
                                f"{type(exc).__name__}: {exc}; raw_head={raw[:500]}"
                            )
                        continue

                    if not isinstance(event, dict):
                        continue

                    handle_event(event)

                    if saw_finish:
                        break

        answer_text = "".join(answer_parts).strip()
        thinking_text = "".join(thinking_parts).strip()
        elapsed = round(time.time() - started_at, 2)

        last_error = None
        for event in events:
            code = str(event.get("code", ""))
            if code not in ("", "0"):
                last_error = event

        meta = {
            "saw_finish": saw_finish,
            "partial": not saw_finish,
            "elapsed_seconds": elapsed,
            "events_count": len(events),
            "raw_lines_count": len(raw_lines),
            "parse_errors_count": len(parse_errors),
            "parse_errors_preview": parse_errors[:5],
            "last_status": last_status,
            "answer_started": answer_started,
            "thinking_started": thinking_started,
            "tool_started": tool_started,
            "document_search_started": document_search_started,
            "has_answer_text": bool(answer_text),
            "has_thinking_text": bool(thinking_text),
            "no_answer_yet": not bool(answer_text),
            "raw_preview": raw_lines[:20],
        }

        if last_error and not answer_text and not thinking_text:
            raise RuntimeError(f"ComeIn agent failed: {last_error}")

        if not events and not answer_text and not thinking_text:
            raise RuntimeError(
                "ComeIn SSE returned no data events. "
                f"elapsed={elapsed}s payload={json.dumps(payload, ensure_ascii=False)} "
                f"raw_preview={raw_lines[:10]} parse_errors={parse_errors[:3]}"
            )

        return {
            "payload": payload,
            "answer_text": answer_text,
            "text": answer_text,
            "thinking_text": thinking_text,
            "events": events,
            "meta": meta,
        }

    async def post_agent(
        self,
        payload: Dict[str, Any],
        *,
        hard_deadline_seconds: float = 180.0,
        line_wait_seconds: float = 15.0,
    ) -> Dict[str, Any]:
        """
        Stable public generic ComeIn Agent API caller.

        app.py is responsible for normalizing agent-specific payloads.
        This provider is responsible for:
        - token fetching/caching
        - SSE request
        - SSE parsing
        - diagnostics
        """
        return await self._post_agent(
            payload,
            hard_deadline_seconds=hard_deadline_seconds,
            line_wait_seconds=line_wait_seconds,
        )


    async def company_overview(
        self,
        *,
        full_code: str,
        company_text: str,
        market_type: str,
        max_wait_seconds: float = 180.0,
    ) -> Dict[str, Any]:
        # Matches the known-good Cover Page payload.
        payload = {
            "model": "DEEPSEEK-NOTHINK",
            "query": f"请给我介绍一下{company_text}的基本情况",
            "agentType": "company_overview",
            "agentParams": {
                "marketType": market_type,
                "companyText": company_text,
                "fullCode": full_code,
            },
            "useRag": 1,
            "referenceType": "chunk",
        }

        return await self._post_agent(
            payload,
            hard_deadline_seconds=max_wait_seconds,
        )

    async def financial_search(
        self,
        *,
        query: str,
        max_wait_seconds: float = 180.0,
    ) -> Dict[str, Any]:
        # Critical fix:
        # The known-good Cover Page payload for financial_search has NO agentParams.
        payload = {
            "model": "DEEPSEEK-NOTHINK",
            "query": query,
            "agentType": "financial_search",
        }

        return await self._post_agent(
            payload,
            hard_deadline_seconds=max_wait_seconds,
        )

    async def performance_review(
        self,
        *,
        full_code: str,
        company_text: str,
        fiscal_year: int,
        report_type: str,
        focus_text: str = "",
        compact: bool = True,
        max_wait_seconds: float = 240.0,
    ) -> Dict[str, Any]:
        normalized_report_type, report_label = self._report_type_label(report_type)

        release_text = "精简版" if compact else "长版"
        report_text = f"{fiscal_year}年-{report_label}"

        if compact:
            query = (
                f"帮我点评一下{company_text}的业绩情况"
                f"{report_text}请按照{release_text}风格输出"
            )
        else:
            query = f"帮我详细点评一下{company_text}的业绩情况{report_text}"

        # Matches the known-good Cover Page payload.
        payload = {
            "query": query,
            "model": "Auto",
            "agentType": "performance_review",
            "agentParams": {
                "fullCode": full_code,
                "fiscalYear": fiscal_year,
                "reportType": normalized_report_type,
                "releaseIds": [2],
                "companyText": company_text,
                "reportText": report_text,
                "focusText": focus_text or "",
                "releaseText": release_text,
            },
        }

        return await self._post_agent(
            payload,
            hard_deadline_seconds=max_wait_seconds,
        )

    async def seller_deep_research_report(
        self,
        *,
        full_code: str,
        company_text: str,
        industry_text: str = "",
        focus_point: str = "",
        length_requirement: str = "长篇",
        max_wait_seconds: float = 900.0,
    ) -> Dict[str, Any]:
        # Kept available, but not required for the current 3-button flow.
        # Matches the successful direct curl format: agentParams as key/value array.
        payload = {
            "model": "DEEPSEEK-NOTHINK",
            "query": (
                f"请围绕{company_text}生成一篇sell-side风格深度报告，"
                f"重点分析{focus_point or '主营业务、竞争格局、盈利能力与主要风险'}。"
            ),
            "agentType": "seller_deep_research_report",
            "agentParams": [
                {"key": "industryText", "value": industry_text},
                {"key": "fullCode", "value": full_code},
                {"key": "companyText", "value": company_text},
                {"key": "roleName", "value": "sell_side"},
                {"key": "lengthRequirement", "value": length_requirement},
                {"key": "focusPoint", "value": focus_point},
            ],
        }

        return await self._post_agent(
            payload,
            hard_deadline_seconds=max_wait_seconds,
        )