#!/usr/bin/env bash
# Film Organizer launcher
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi
exec ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
