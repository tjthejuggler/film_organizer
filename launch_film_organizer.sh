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

URL="http://127.0.0.1:8964"

# True only if OUR app answers (verifies the page title, so a different
# webapp squatting on the port is never mistaken for Film Organizer).
is_our_app() {
    curl -fsS -m 3 "$URL/" 2>/dev/null | grep -q "<title>Film Organizer</title>"
}

# Already running? Just open the UI.
if is_our_app; then
    xdg-open "$URL" >/dev/null 2>&1 || true
    exit 0
fi

# 0.0.0.0 = LAN-reachable; remote devices must pair via the QR code shown
# in Settings (device gate in app/pairing.py). Loopback stays trusted.
nohup ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8964 \
    >/dev/null 2>&1 &

# Wait (up to ~10s) for OUR server to answer before opening the UI.
for _ in $(seq 1 50); do
    is_our_app && break
    sleep 0.2
done
xdg-open "$URL" >/dev/null 2>&1 || true
