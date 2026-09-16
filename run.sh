#!/usr/bin/env bash
# Film Organizer launcher
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    python3 -m venv .venv
    ./.venv/bin/pip install --upgrade pip
    ./.venv/bin/pip install -r requirements.txt
fi
# 0.0.0.0 = reachable from other devices on the LAN; app-level security is
# enforced by the device gate (see app/pairing.py): remote devices must pair
# via the QR code shown in Settings.  Loopback stays trusted automatically.
exec ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8765
