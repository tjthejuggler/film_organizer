"""Native desktop file-move dialog (Plasma JobView).

Dolphin-style progress for programmatic moves: a helper script
(`app/kio_jobview.py`, run under the SYSTEM python3 which has python3-dbus)
requests a JobView from org.kde.JobViewServer and performs the move itself,
streaming real byte/file progress into the standard Plasma transfer dialog
with working pause/cancel.

Why not `kioclient move`? Verified empirically: kioclient links only
KIOCore and never registers with the job tracker (dbus-monitor showed zero
requestView calls), so moves complete silently. The JobView dialog must be
owned by a persistent D-Bus connection for its whole lifetime — hence the
out-of-process helper with a stdout protocol.

Everything is best-effort: `available()` is False on headless systems, and
`move()` returns (ok, detail) instead of raising; callers fall back to
shutil.move. A user cancel surfaces as a RuntimeError in mover.
"""
import os
import shutil
import subprocess
import sys
import threading

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "kio_jobview.py")


def _bus_ok() -> bool:
    """A D-Bus session bus we could plausibly talk to is configured."""
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    return os.path.exists(f"/run/user/{os.getuid()}/bus")


def session_env() -> dict:
    """Environment guaranteed to carry a session-bus address, so the helper
    can reach the desktop's job tracker even when the server was started
    outside the graphical session (cron, systemd, nohup)."""
    env = dict(os.environ)
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        path = f"/run/user/{os.getuid()}/bus"
        if os.path.exists(path):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={path}"
    return env


def _system_python() -> str:
    """A python3 with dbus bindings: prefer the distro interpreter (venvs
    usually lack python3-dbus)."""
    cand = "/usr/bin/python3"
    if os.path.exists(cand):
        return cand
    return sys.executable


def available() -> bool:
    """True when the helper exists, a system python3 is present and a
    session bus is reachable."""
    return (os.path.exists(HELPER)
            and os.path.exists(_system_python())
            and _bus_ok())


def move(src: str, dest: str, label: str = ""):
    """Move src -> dest showing the native progress dialog.

    `dest` is the FULL destination path (file or folder), not a directory.
    Blocks until the move finishes. Returns (ok, detail); ok False means
    'use the shutil fallback'. Never raises.
    """
    if not available():
        return False, "helper unavailable"
    cmd = [_system_python(), HELPER, src, dest, label or os.path.basename(src)]
    try:
        proc = subprocess.Popen(cmd, env=session_env(),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL,
                                text=True)
    except OSError as e:
        return False, str(e)

    # consume stdout in this thread; wait() with a reader is deadlock-prone
    lines: list = []
    reader = threading.Thread(target=lambda: lines.extend(
        line.strip() for line in proc.stdout if line.strip()), daemon=True)
    reader.start()
    proc.wait()
    reader.join(timeout=2)

    for line in lines:
        if line == "DONE":
            return True, ""
        if line.startswith("CANCELLED"):
            return False, "cancelled"
        if line.startswith("ERROR"):
            return False, line[5:].strip() or "move failed"
        if line.startswith("ERROR "):
            return False, line[6:].strip()
    return False, f"helper exited {proc.returncode} without verdict"
