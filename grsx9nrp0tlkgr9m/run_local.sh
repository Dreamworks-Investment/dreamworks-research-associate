#!/usr/bin/env bash
set -euo pipefail

if [ ! -d ".venv" ]; then
  python -m venv .venv
fi

source .venv/bin/activate
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from template. Please fill in your keys and SKILL_ARCHIVE path."
fi

uvicorn backend.app:app --reload --port 8000
