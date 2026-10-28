from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import httpx

from ..config import MANUS_API_KEY, MANUS_BASE_URL, MANUS_AGENT_PROFILE


class ManusResearchProvider:
    def __init__(self) -> None:
        if not MANUS_API_KEY:
            raise RuntimeError("MANUS_API_KEY is not configured")
        self.base = MANUS_BASE_URL.rstrip("/")
        self.headers = {
            "Content-Type": "application/json",
            "x-manus-api-key": MANUS_API_KEY,
        }
        self.agent_profile = MANUS_AGENT_PROFILE

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        with httpx.Client(timeout=180) as client:
            resp = client.post(f"{self.base}{path}", headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        with httpx.Client(timeout=180) as client:
            resp = client.get(f"{self.base}{path}", headers=self.headers, params=params)
            resp.raise_for_status()
            return resp.json()

    def _try_get(self, paths: List[str], params: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for path in paths:
            try:
                return self._get(path, params)
            except Exception as e:
                last_error = e
        raise last_error or RuntimeError("No GET path succeeded")

    def _try_post(self, paths: List[str], payload: Dict[str, Any]) -> Dict[str, Any]:
        last_error: Optional[Exception] = None
        for path in paths:
            try:
                return self._post(path, payload)
            except Exception as e:
                last_error = e
        raise last_error or RuntimeError("No POST path succeeded")

    def list_skills(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {}
        if project_id:
            params["project_id"] = project_id
        data = self._get("/v2/skill.list", params)
        return data.get("data", [])

    def create_task(
        self,
        prompt_text: str,
        enable_skills: Optional[List[str]] = None,
        force_skills: Optional[List[str]] = None,
        locale: str = "zh-CN",
        interactive_mode: bool = False,
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "message": {"content": [{"type": "text", "text": prompt_text}]},
            "locale": locale,
            "interactive_mode": interactive_mode,
            "agent_profile": self.agent_profile,
            "hide_in_task_list": False,
            "share_visibility": "private",
        }
        if enable_skills:
            payload["message"]["enable_skills"] = enable_skills
        if force_skills:
            payload["message"]["force_skills"] = force_skills
        if title:
            payload["title"] = title
        return self._post("/v2/task.create", payload)

    def get_task(self, task_id: str) -> Dict[str, Any]:
        return self._get("/v2/task.detail", {"task_id": task_id})

    def list_messages(self, task_id: str) -> Dict[str, Any]:
        return self._try_get(
            ["/v2/task.listMessages", "/v2/task.list-messages", "/v2/list-messages"],
            {"task_id": task_id},
        )

    def confirm_action(self, task_id: str, event_id: str, input_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"task_id": task_id, "event_id": event_id}
        if input_data is not None:
            payload["input"] = input_data
        return self._try_post(
            ["/v2/task.confirmAction", "/v2/task.confirm-action"],
            payload,
        )

    def send_message(self, task_id: str, text: str) -> Dict[str, Any]:
        payload = {"task_id": task_id, "message": {"content": [{"type": "text", "text": text}]}}
        return self._try_post(["/v2/task.sendMessage", "/v2/task.send-message"], payload)

    def _flatten_dicts(self, node: Any) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if isinstance(node, dict):
            out.append(node)
            for v in node.values():
                out.extend(self._flatten_dicts(v))
        elif isinstance(node, list):
            for item in node:
                out.extend(self._flatten_dicts(item))
        return out

    def _collect_texts(self, node: Any) -> List[str]:
        texts: List[str] = []
        if isinstance(node, dict):
            for key in ["text", "content", "message", "brief", "description"]:
                if isinstance(node.get(key), str):
                    texts.append(node[key])
            for v in node.values():
                texts.extend(self._collect_texts(v))
        elif isinstance(node, list):
            for item in node:
                texts.extend(self._collect_texts(item))
        return texts

    def _dedupe(self, items: List[str]) -> List[str]:
        seen, out = set(), []
        for item in items:
            norm = re.sub(r"\s+", " ", item).strip()
            if not norm or norm in seen:
                continue
            seen.add(norm)
            out.append(item.strip())
        return out

    def extract_waiting_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        flattened = self._flatten_dicts(payload)
        waiting_candidates: List[Dict[str, Any]] = []
        ask_candidates: List[Dict[str, Any]] = []
        for d in flattened:
            if d.get("agent_status") == "waiting" or any(k in d for k in ["waiting_for_event_id", "waiting_for_event_type", "confirm_input_schema"]):
                waiting_candidates.append(d)
            if str(d.get("type", "")).lower() == "messageaskuser" or str(d.get("event_type", "")).lower() == "messageaskuser":
                ask_candidates.append(d)
        candidate = waiting_candidates[-1] if waiting_candidates else None
        ask = ask_candidates[-1] if ask_candidates else None
        return {
            "is_waiting": bool(waiting_candidates),
            "is_message_ask_user": bool(ask),
            "waiting_for_event_id": candidate.get("waiting_for_event_id") if candidate else None,
            "waiting_for_event_type": (candidate.get("waiting_for_event_type") or candidate.get("event_type")) if candidate else None,
            "confirm_input_schema": candidate.get("confirm_input_schema") if candidate else None,
            "raw_waiting_event": candidate,
            "raw_message_ask_user": ask,
        }

    def extract_timeline(self, payload: Dict[str, Any]) -> List[str]:
        texts = self._dedupe([t for t in self._collect_texts(payload) if isinstance(t, str) and len(t.strip()) > 16])
        # remove generic scaffold prompt if there are richer texts
        filtered = [t for t in texts if not t.strip().startswith("Use the provided skill(s)")]
        return filtered or texts

    def extract_latest_text(self, payload: Dict[str, Any]) -> str:
        timeline = self.extract_timeline(payload)
        return timeline[0] if timeline else ""

    def extract_parallel_tasks(self, payload: Dict[str, Any]) -> List[str]:
        texts = self._collect_texts(payload)
        seen, tasks = set(), []
        for text in texts:
            for match in re.findall(r'【任务\d+：[^】]+】', text):
                if match not in seen:
                    seen.add(match)
                    tasks.append(match)
        return tasks

    def extract_attachments(self, messages_payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        attachments = []
        seen = set()
        for d in self._flatten_dicts(messages_payload):
            atts = d.get("attachments")
            if isinstance(atts, list):
                for item in atts:
                    if not isinstance(item, dict):
                        continue
                    filename = item.get("filename") or item.get("name")
                    url = item.get("url") or item.get("download_url")
                    ctype = item.get("content_type") or ""
                    if not filename:
                        continue
                    if not re.search(r'\.(md|pdf|docx)$', str(filename), re.I):
                        continue
                    key = (filename, url)
                    if key in seen:
                        continue
                    seen.add(key)
                    attachments.append({
                        "name": str(filename),
                        "url": url,
                        "content_type": ctype,
                        "format": str(filename).split(".")[-1].lower(),
                        "source": "manus_attachment",
                    })
        return attachments

    def get_task_state(self, task_id: str) -> Dict[str, Any]:
        detail = self.get_task(task_id)
        messages = self.list_messages(task_id)
        waiting = self.extract_waiting_state(messages)
        return {
            "task_id": task_id,
            "status": detail.get("task", {}).get("status") or detail.get("status") or ("waiting" if waiting["is_waiting"] else "running"),
            "detail": detail,
            "messages": messages,
            "waiting": waiting,
            "latest_text": self.extract_latest_text(messages),
            "timeline": self.extract_timeline(messages),
            "parallel_tasks": self.extract_parallel_tasks(messages),
            "attachments": self.extract_attachments(messages),
        }
