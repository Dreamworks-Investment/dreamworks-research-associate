from __future__ import annotations

from typing import Any, Dict, List

from openai import OpenAI

from ..config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL


class OpenAIResearchProvider:
    def __init__(self) -> None:
        if not OPENAI_API_KEY:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        kwargs: Dict[str, Any] = {"api_key": OPENAI_API_KEY}
        if OPENAI_BASE_URL:
            kwargs["base_url"] = OPENAI_BASE_URL

        self.client = OpenAI(**kwargs)
        self.model = OPENAI_MODEL

    def generate_report(
        self,
        system_prompt: str,
        user_prompt: str,
        context_blocks: List[str] | None = None,
    ) -> str:
        context = "\n\n".join(context_blocks or [])
        full_input = f"{user_prompt}\n\n=== DATA CONTEXT ===\n{context}".strip()

        response = self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": full_input},
            ],
        )
        return response.output_text
