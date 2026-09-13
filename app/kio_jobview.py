#!/usr/bin/python3
"""Standalone native-move helper (run with the SYSTEM python3, which has
python3-dbus + gir1.2-glib installed — NOT the project venv).

Performs one file/folder move while showing Plasma's standard file-transfer
progress dialog (the same one Dolphin's moves live in) via
org.kde.JobViewServer. Supports pause (suspend) and cancel from the dialog.

Usage:  python3 kio_jobview.py <src> <dest> <label> [desktopEntry]

stdout protocol (machine-readable, one per line):
    VIEW <object path>   dialog registered
    DONE                 move completed
    CANCELLED            user pressed cancel (partial dest cleaned up)
    ERROR <detail>       move failed   (partial dest cleaned up)
Exit codes: 0 done, 3 cancelled, 4 error, 5 environment error.
"""
import errno
import os
import shutil
import sys
import time

CHUNK = 4 * 1024 * 1024          # copy chunk size
UPDATE_EVERY = 0.15              # seconds between D-Bus progress updates


class Cancelled(Exception):
    pass


def main():
    if len(sys.argv) < 4:
        print("ERROR missing arguments")
        return 4
    src, dest, label = sys.argv[1], sys.argv[2], sys.argv[3]
    desktop_entry = sys.argv[4] if len(sys.argv) > 4 else "org.kde.dolphin"

    try:
        import dbus
        import dbus.mainloop.glib
        from gi.repository import GLib
    except ImportError as e:
        print(f"ERROR dbus bindings unavailable: {e}")
        return 5

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    try:
        bus = dbus.SessionBus()
    except Exception as e:
        print(f"ERROR no session bus: {e}")
        return 5

    svc = bus.get_object("org.kde.JobViewServer", "/JobViewServer")
    view_path = str(svc.requestView(desktop_entry, 3, {},   # suspend|kill
                                    dbus_interface="org.kde.JobViewServerV2"))
    view = bus.get_object("org.kde.JobViewServer", view_path)
    vj = dbus.Interface(view, "org.kde.JobViewV2")
    print(f"VIEW {view_path}", flush=True)

    state = {"suspended": False, "cancelled": False}
    bus.add_signal_receiver(lambda: state.__setitem__("suspended", True),
                            "suspendRequested", "org.kde.JobViewV2",
                            "org.kde.JobViewServer", view_path)
    bus.add_signal_receiver(lambda: state.__setitem__("suspended", False),
                            "resumeRequested", "org.kde.JobViewV2",
                            "org.kde.JobViewServer", view_path)
    bus.add_signal_receiver(lambda: state.__setitem__("cancelled", True),
                            "cancelRequested", "org.kde.JobViewV2",
                            "org.kde.JobViewServer", view_path)

    import dbus as _d
    U32, U64 = _d.UInt32, _d.UInt64
    loop = GLib.MainLoop()

    def pump():
        ctx = loop.get_context()
        while ctx.pending():
            ctx.iteration(False)

    def check_flags():
        pump()
        if state["cancelled"]:
            raise Cancelled()
        while state["suspended"]:
            time.sleep(0.05)
            pump()
            if state["cancelled"]:
                raise Cancelled()

    def plan(s, d):
        """(files [(src,dst)], dirs [dest dirs to create], total bytes)"""
        if os.path.isfile(s):
            return [(s, d)], [], os.path.getsize(s)
        files, dirs, total = [], [], 0
        for root, _dirs, names in os.walk(s):
            rel = os.path.relpath(root, s)
            target_root = d if rel == "." else os.path.join(d, rel)
            dirs.append(target_root)
            for n in names:
                fp = os.path.join(root, n)
                files.append((fp, os.path.join(target_root, n)))
                total += os.path.getsize(fp)
        return files, dirs, total

    def cleanup(d):
        """Best-effort removal of a half-written destination."""
        try:
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            elif os.path.exists(d):
                os.unlink(d)
        except OSError:
            pass

    def report_progress(args):
        # args: dict of pre-built callables to apply
        for fn in args:
            fn()

    try:
        if not os.path.lexists(src):
            raise FileNotFoundError(src)
        dest_parent = os.path.dirname(dest.rstrip(os.sep))
        if dest_parent and not os.path.isdir(dest_parent):
            os.makedirs(dest_parent, exist_ok=True)
        if os.path.lexists(dest):
            raise FileExistsError(dest)

        files, dirs, total = plan(src, dest)
        n_files = max(len(files), 1)

        vj.setDescriptionField(U32(1), "Source", src)
        vj.setDescriptionField(U32(2), "Destination", dest)
        vj.setInfoMessage(f"Moving: {label or os.path.basename(src)}")
        vj.setTotalAmount(U64(max(total, 1)), "bytes")
        vj.setTotalAmount(U64(n_files), "files")
        vj.setPercent(U32(0))
        pump()

        done_bytes = 0
        last_paint = 0.0
        speed_mark_t = time.monotonic()
        speed_mark_b = 0

        def paint(force=False):
            nonlocal done_bytes, last_paint, speed_mark_b, speed_mark_t
            now = time.monotonic()
            if force or now - last_paint >= UPDATE_EVERY:
                vj.setProcessedAmount(U64(done_bytes), "bytes")
                if now > speed_mark_t:
                    rate = int((done_bytes - speed_mark_b) / (now - speed_mark_t))
                    vj.setSpeed(U64(max(rate, 0)))
                    speed_mark_t, speed_mark_b = now, done_bytes
                if total:
                    vj.setPercent(U32(min(100, int(100 * done_bytes / total))))
                last_paint = now

        # same-device file/dir move: one atomic rename, dialog races to 100%
        try:
            os.rename(src, dest)
            vj.setProcessedAmount(U64(max(total, 1)), "bytes")
            vj.setProcessedAmount(U64(n_files), "files")
            vj.setPercent(U32(100))
            vj.terminate("")
            print("DONE", flush=True)
            return 0
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise           # real error, not a cross-device jump

        # cross-device: create dirs, copy file-by-file, then delete source
        for d in dirs:
            os.makedirs(d, exist_ok=True)
        for i, (s, d) in enumerate(files, 1):
            check_flags()
            vj.setInfoMessage(f"Moving: {os.path.basename(s)}")
            vj.setProcessedAmount(U64(i - 1), "files")
            try:
                os.rename(s, d)             # lucky case: same subtree device
                done_bytes += os.path.getsize(d)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                with open(s, "rb") as fin, open(d, "wb") as fout:
                    while True:
                        check_flags()
                        chunk = fin.read(CHUNK)
                        if not chunk:
                            break
                        fout.write(chunk)
                        done_bytes += len(chunk)
                        paint()
                shutil.copystat(s, d)
            paint(force=True)
        if os.path.isdir(src):
            shutil.rmtree(src)
        else:
            os.unlink(src)

        vj.setProcessedAmount(U64(max(total, 1)), "bytes")
        vj.setProcessedAmount(U64(n_files), "files")
        vj.setPercent(U32(100))
        vj.terminate("")
        print("DONE", flush=True)
        return 0

    except Cancelled:
        cleanup(dest)
        try:
            vj.terminate("Cancelled")
        except Exception:
            pass
        print("CANCELLED", flush=True)
        return 3
    except Exception as e:
        cleanup(dest)
        try:
            vj.terminate(str(e))
        except Exception:
            pass
        print(f"ERROR {e}", flush=True)
        return 4


if __name__ == "__main__":
    sys.exit(main())
