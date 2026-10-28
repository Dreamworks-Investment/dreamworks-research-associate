from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]

APP_ENV = os.getenv("APP_ENV", "dev")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
COMEIN_AGENT_TOKEN_URL = os.getenv("COMEIN_AGENT_TOKEN_URL", "https://server.comein.cn/comein/index.php")
COMEIN_AGENT_CHAT_URL = os.getenv("COMEIN_AGENT_CHAT_URL", "https://sse.comein.cn/open/sse/chat")
COMEIN_APPID = os.getenv("COMEIN_APPID", "")
COMEIN_PHONE = os.getenv("COMEIN_PHONE", "")
COMEIN_PUBLIC_KEY = os.getenv("COMEIN_PUBLIC_KEY", "")
COMEIN_PRIVATE_KEY = os.getenv("COMEIN_PRIVATE_KEY", "")

COMEIN_TOKEN_CACHE_SECONDS = int(os.getenv("COMEIN_TOKEN_CACHE_SECONDS", "1800"))

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")

ALPHAPAI_API_BASE = os.getenv("ALPHAPAI_API_BASE", "")
ALPHAPAI_API_KEY = os.getenv("ALPHAPAI_API_KEY", "")

SKILL_ARCHIVE = os.getenv("SKILL_ARCHIVE", "")
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", "/tmp/reports"))
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR = REPORTS_DIR.resolve()

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000").split(",")
    if origin.strip()
]

PROVIDER = os.getenv("PROVIDER", "manus")
MANUS_API_KEY = os.getenv("MANUS_API_KEY", "")
MANUS_BASE_URL = os.getenv("MANUS_BASE_URL", "https://api.manus.ai")
MANUS_AGENT_PROFILE = os.getenv("MANUS_AGENT_PROFILE", "manus-1.6-max")
MANUS_FORCE_SKILLS = [s.strip() for s in os.getenv("MANUS_FORCE_SKILLS", "").split(",") if s.strip()]
MANUS_PROMPT_SKILL_NAME = os.getenv("MANUS_PROMPT_SKILL_NAME", "dreamworks-full-report-xu-prefin")

ARTIFACT_SEARCH_DIRS = [Path(p).expanduser().resolve() for p in os.getenv("ARTIFACT_SEARCH_DIRS", "").split(",") if p.strip()] or [REPORTS_DIR, (Path.home() / "Downloads").resolve(), BASE_DIR.resolve()]
