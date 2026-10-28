from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from backend.report_export import write_docx, write_markdown, write_pdf
from backend.skill_runtime import build_system_prompt, load_skill_bundle
from backend.providers.openai_provider import OpenAIResearchProvider
from backend.config import REPORTS_DIR

mcp = FastMCP("deep-value-report")

skill_parts = load_skill_bundle()
system_prompt = build_system_prompt(skill_parts)


@mcp.tool()
def preview_report_outline(stock_name: str, stock_code: str) -> str:
    '''Return a compact outline for the report before running the full research job.'''
    return (
        f"Outline for {stock_name} ({stock_code})\n"
        "1. Conclusion\n"
        "2. Business model\n"
        "3. Company history\n"
        "4. Business data overview\n"
        "5. Growth\n"
        "6. Return rates\n"
        "7. Sustainability\n"
        "8. Management & culture\n"
        "9. External opinions\n"
        "10. Valuation, odds, confidence\n"
    )


@mcp.tool()
def run_deep_value_report(stock_name: str, stock_code: str, user_prompt: str = "") -> str:
    '''Generate the markdown report using the local skill and LLM provider.'''
    provider = OpenAIResearchProvider()
    prompt = (
        user_prompt
        or f"Please generate a full modern deep value report for {stock_name} {stock_code}."
    )
    return provider.generate_report(system_prompt=system_prompt, user_prompt=prompt)


@mcp.tool()
def export_report(report_markdown: str, fmt: str = "md") -> str:
    '''Export markdown to md, pdf, or docx and return the saved file path.'''
    import uuid

    report_id = str(uuid.uuid4())
    if fmt == "md":
        path = REPORTS_DIR / f"{report_id}.md"
        write_markdown(path, report_markdown)
    elif fmt == "pdf":
        path = REPORTS_DIR / f"{report_id}.pdf"
        write_pdf(path, report_markdown)
    elif fmt == "docx":
        path = REPORTS_DIR / f"{report_id}.docx"
        write_docx(path, report_markdown)
    else:
        raise ValueError("Unsupported export format")
    return str(path)


if __name__ == "__main__":
    # For local STDIO development:
    mcp.run()
