#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
exec python -m uvicorn backend.app:app --host 0.0.0.0 --port 8000