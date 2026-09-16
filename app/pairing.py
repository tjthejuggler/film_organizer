"""QR-code device pairing + per-device session tokens.

The server listens on the LAN (0.0.0.0) but remote devices are only let
through when they carry a `fo_device` cookie whose token matches a row in
the `devices` table.  Trust is granted in exactly one way: a 6-digit
pairing code that only exists on a screen attached to the machine running
the server (the Settings sheet renders it as a QR code).  Loopback
requests skip the check entirely — the local UI is always trusted.

Token model: each paired device gets a 256-bit random token.  Only a
SHA-256 hash of the token is stored, so a stolen database backup does not
leak usable cookies.  Codes expire after 5 minutes and regenerate on
demand; regenerating also kicks nothing already paired (revoking a device
is a separate, explicit action).
"""
import hashlib
import ipaddress
import os
import secrets
import threading
import time

from . import config, db

COOKIE_NAME = "fo_device"
CODE_TTL = 5 * 60          # pairing code lifetime (seconds)
TOKEN_TTL = 365 * 24 * 3600  # device token lifetime (seconds)

# RLock (not Lock): peek_code() legitimately nests into new_code() while
# holding the lock — a plain Lock self-deadlocks there (same trap the db
# layer documented).
_lock = threading.RLock()
_code = None       # current 6-digit pairing code (or None)
_code_expires = 0  # unix time after which the code is dead

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL DEFAULT 'Device',
    token_hash TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    last_seen  TEXT,
    last_ip    TEXT
);
"""


def ensure_schema():
    with db.conn() as con:
        con.executescript(SCHEMA)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def new_code() -> str:
    """(Re)generate the 6-digit pairing code; valid for CODE_TTL."""
    global _code, _code_expires
    with _lock:
        _code = f"{secrets.randbelow(1000000):06d}"
        _code_expires = time.time() + CODE_TTL
        return _code


def peek_code() -> dict:
    """Current code + seconds left; generates one lazily on first view."""
    global _code, _code_expires
    with _lock:
        if not _code or time.time() >= _code_expires:
            return {"code": new_code(), "expires_in": CODE_TTL}
        return {"code": _code, "expires_in": int(_code_expires - time.time())}


def redeem(code: str) -> dict:
    """Exchange a valid pairing code for a device token (one-shot grant).

    Returns {"token": ..., "name": ...} or raises ValueError."""
    global _code, _code_expires
    with _lock:
        alive = _code and code == _code and time.time() < _code_expires
        if not alive:
            raise ValueError("invalid or expired code")
        _code = None  # one-shot: a leaked QR cannot pair a second device
        _code_expires = 0
    token = secrets.token_urlsafe(32)
    with db.tx() as c:
        c.execute(
            "INSERT INTO devices(name, token_hash, last_seen, last_ip) "
            "VALUES(?,?,?,?)",
            ("Device", _hash(token), _now(), ""),
        )
    return {"token": token, "name": "Device"}


def check_token(token: str) -> bool:
    """True when the token belongs to a paired device (updates last_seen)."""
    if not token:
        return False
    row = db.q1("SELECT id FROM devices WHERE token_hash=?", (_hash(token),))
    if not row:
        return False
    with db.tx() as c:
        c.execute("UPDATE devices SET last_seen=? WHERE id=?", (_now(), row["id"]))
    return True


def list_devices() -> list:
    """Paired devices, token hashes never leave the server."""
    return [dict(r) for r in db.q(
        "SELECT id, name, created_at, last_seen, last_ip FROM devices "
        "ORDER BY id")]


def rename_device(device_id: int, name: str):
    with db.tx() as c:
        c.execute("UPDATE devices SET name=? WHERE id=?", (name, device_id))


def revoke_device(device_id: int) -> bool:
    with db.tx() as c:
        cur = c.execute("DELETE FROM devices WHERE id=?", (device_id,))
        return cur.rowcount > 0


def lan_urls() -> list:
    """URLs the QR code should encode — one per non-internal IPv4/IPv6
    interface address, so phones can just point a camera at the screen."""
    urls = []
    try:
        import socket
        host = config.HOST if config.HOST != "0.0.0.0" else None
        if host is None:
            # best-effort primary-route address (no packets actually sent
            # for UDP connect on most stacks)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("8.8.8.8", 80))
                host = s.getsockname()[0]
            finally:
                s.close()
        if host:
            urls.append(f"http://{host}:{config.PORT}/")
    except OSError:
        pass
    return urls
