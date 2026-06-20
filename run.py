"""Rezo entry point.

Usage:
  python run.py                  Tray app + collector + dashboard (default)
  python run.py --no-tray        Same, but no tray icon (Ctrl+C to stop)
  python run.py --serve-only     Dashboard only, against existing data (no collecting)
  python run.py --seed [--days N]  Fill the DB with demo history, then exit
  python run.py --collect-seconds N   Collect for N seconds then exit (testing)
  python run.py --open           Just open the dashboard in a browser
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

from rezo import config, storage
from rezo.server import run_server


def _single_instance() -> bool:
    """Return True if we acquired the lock; False if another instance holds it."""
    if sys.platform != "win32":
        return True
    import ctypes
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\RezoSystemDashboard")
    # 183 == ERROR_ALREADY_EXISTS
    return ctypes.windll.kernel32.GetLastError() != 183


def main() -> int:
    ap = argparse.ArgumentParser(prog="rezo", description="Rezo system dashboard")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--serve-only", action="store_true")
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--collect-seconds", type=int, default=0)
    ap.add_argument("--open", action="store_true")
    args = ap.parse_args()

    if args.open:
        webbrowser.open(config.URL)
        return 0

    if args.seed:
        from rezo import seed
        print(f"Seeding ~{args.days} days of demo data into {config.DB_PATH} …")
        counts = seed.generate(args.days)
        print(f"Done: {counts['sys']} system, {counts['proc']} process, "
              f"{counts['daily']} daily rows. Run `python run.py` to view.")
        return 0

    storage.init_db()

    if args.serve_only:
        print(f"Serving dashboard (no collection) at {config.URL}")
        threading.Timer(1.0, lambda: webbrowser.open(config.URL)).start()
        run_server(collector=None)
        return 0

    if args.collect_seconds:
        from rezo.collector import Collector
        c = Collector()
        c.start()
        print(f"Collecting for {args.collect_seconds}s …")
        time.sleep(args.collect_seconds)
        c.stop()
        time.sleep(0.5)
        from rezo import queries
        print(f"Collected. History span: {queries.data_span_days()*24:.2f} hours.")
        return 0

    # --- running modes that collect: guard against a second instance ---
    if not _single_instance():
        print("Rezo is already running — opening the dashboard.")
        webbrowser.open(config.URL)
        return 0

    from rezo.collector import Collector
    collector = Collector()
    collector.start()

    server_thread = threading.Thread(
        target=run_server, kwargs={"collector": collector}, daemon=True
    )
    server_thread.start()

    if args.no_tray:
        print(f"Rezo collecting; dashboard at {config.URL}  (Ctrl+C to stop)")
        threading.Timer(1.0, lambda: webbrowser.open(config.URL)).start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            collector.stop()
        return 0

    from rezo.tray import run_tray
    run_tray(collector, open_browser=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
