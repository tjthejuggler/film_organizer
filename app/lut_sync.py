"""Bridge to quick_capture_assist: keep the voice fast-path in sync.

Whenever catalog reality changes — a scan discovers titles, a title is
moved between internal/external storage, files are deleted, or a queued
operation finishes after a drive is plugged in — we regenerate the
dispatcher's lookup table (movies) and show catalog (series) so "play
<title>" works for everything that is currently reachable.

Debounced: hooks fire MANY times during a scan/move burst; we coalesce
them into one regeneration ~2 s after the last request. Failures never
propagate (the organizer must keep working even if the assistant repo is
missing).
"""
import subprocess
import threading

_SYNC_SCRIPT = ("/home/twain/Projects/quick_capture_assist/"
                "scripts/sync_movie_lut.py")

_timer: threading.Timer | None = None
_lock = threading.Lock()


def request_sync(reason: str = "") -> None:
    """Coalesce a sync request; runs sync_movie_lut.py in the background."""
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(2.0, _run, args=(reason,))
        _timer.daemon = True
        _timer.start()


def _run(reason: str) -> None:
    global _timer
    with _lock:
        _timer = None
    try:
        subprocess.Popen(
            ["python3", _SYNC_SCRIPT, "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except Exception:
        pass  # never let assistant-sync break the organizer
