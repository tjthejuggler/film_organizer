#!/usr/bin/env bash
# Film Organizer launcher (for the MyApps tray launcher and general use).
# Starts the web app if it isn't running yet, then opens the UI in a browser.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi

URL="http://127.0.0.1:8765"

# Already running? Just open the UI.
if curl -fsS "$URL/" >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
    exit 0
fi

nohup ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8765 \
    >/dev/null 2>&1 &

# Wait (up to ~10s) for the server to answer before opening the UI.
for _ in $(seq 1 50); do
    curl -fsS "$URL/" >/dev/null 2>&1 && break
    sleep 0.2
done
xdg-open "$URL" >/dev/null 2>&1 || true
