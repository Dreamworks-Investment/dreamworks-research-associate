from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Dict

from .config import SKILL_ARCHIVE


def load_skill_bundle(skill_archive: str | None = None) -> Dict[str, str]:
    archive_path = Path(skill_archive or SKILL_ARCHIVE)
    if not archive_path.exists():
        raise FileNotFoundError(
            f"Skill archive not found: {archive_path}. Set SKILL_ARCHIVE in .env"
        )

    if archive_path.suffix != ".skill" and not zipfile.is_zipfile(archive_path):
        raise ValueError("Expected a .skill zip archive")

    parts: Dict[str, str] = {}
    with zipfile.ZipFile(archive_path) as zf:
        for name in zf.namelist():
            if name.endswith("/") or not name.endswith(".md"):
                continue
            parts[name] = zf.read(name).decode("utf-8", errors="ignore")
    return parts


def build_system_prompt(skill_parts: Dict[str, str]) -> str:
    skill_md = skill_parts.get("SKILL.md", "")
    references = "\n\n".join(
        skill_parts[name]
        for name in sorted(skill_parts)
        if name.startswith("references/")
    )

    return (
        "You are the runtime for the local skill package 'modern-deep-value-report-v2'.\n\n"
        "Follow the skill content below as the highest-priority business template.\n\n"
        "================ SKILL.md ================\n"
        + skill_md
        + "\n\n================ REFERENCES ================\n"
        + references
        + "\n\nYou must:\n"
        "1. Ask for or infer the stock name and stock code.\n"
        "2. Produce a modern deep value research report in markdown.\n"
        "3. Preserve the report's section structure.\n"
        "4. Fill every required blank or field with a reasoned answer.\n"
        "5. When data is missing, explicitly mark assumptions and what external data should be retrieved.\n"
        "6. End with a compact 'next data to verify' section.\n"
    )
