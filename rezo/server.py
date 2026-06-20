"""Flask app: serves the dashboard and a small JSON API.

Readers only -- the API never writes. A live snapshot comes from the running
collector when present (fresh, no extra sampling), otherwise from the DB so the
dashboard still works in --serve-only mode against seeded/historical data.
"""
from __future__ import annotations

import time

import psutil
from flask import Flask, jsonify, request, send_from_directory

from . import analyze, config, metrics, queries, recommend, security
from .collector import _disk_usage as disk_snapshot

WEB_DIR = str((__import__("pathlib").Path(__file__).parent / "web").resolve())


def create_app(collector=None) -> Flask:
    app = Flask(__name__, static_folder=WEB_DIR, static_url_path="")

    @app.route("/")
    def index():
        return send_from_directory(WEB_DIR, "index.html")

    @app.route("/api/specs")
    def api_specs():
        return jsonify(metrics.get_specs())

    @app.route("/api/summary")
    def api_summary():
        specs = metrics.get_specs()
        if collector is not None and collector.latest:
            sys_row = dict(collector.latest)
            apps = collector.latest_apps
            paused = collector.paused
        else:
            sys_row = queries.latest_sys() or {}
            _, _, _, per_drive = disk_snapshot()
            sys_row["per_drive"] = per_drive
            apps = queries.app_rankings("day")[:25]
            paused = None

        if "per_drive" not in sys_row:
            _, _, _, per_drive = disk_snapshot()
            sys_row["per_drive"] = per_drive

        return jsonify({
            "specs": specs,
            "sys": sys_row,
            "apps": apps,
            "paused": paused,
            "boot_time": psutil.boot_time(),
            "now": time.time(),
            "data_days": queries.data_span_days(),
        })

    @app.route("/api/timeseries")
    def api_timeseries():
        rng = request.args.get("range", "day")
        if rng not in queries.RANGES:
            rng = "day"
        return jsonify(queries.timeseries(rng))

    @app.route("/api/apps")
    def api_apps():
        rng = request.args.get("range", "day")
        if rng not in queries.RANGES:
            rng = "day"
        return jsonify({"range": rng, "apps": queries.app_rankings(rng)})

    @app.route("/api/recommendations")
    def api_reco():
        return jsonify(recommend.build())

    @app.route("/api/alerts")
    def api_alerts():
        if request.args.get("demo"):
            return jsonify(_demo_alerts())
        cont = analyze.contention()
        if collector is not None:
            sec = collector.security
        else:
            # serve-only: do a live scan on demand
            sec = security.scan_processes()
        suspicious = sec.get("suspicious", [])
        high = (any(c["severity"] == "high" for c in cont)
                or any(s["severity"] == "high" for s in suspicious))
        return jsonify({
            "contention": cont,
            "suspicious": suspicious,
            "scanned_at": sec.get("scanned_at"),
            "scanned_count": sec.get("scanned_count"),
            "has_high": high,
            "clear": not cont and not suspicious,
            "note": ("Heuristic signals only — not antivirus. Legitimate software "
                     "can match these patterns, and a periodic scan can miss "
                     "processes that run briefly. Always confirm before acting."),
        })

    return app


def _demo_alerts() -> dict:
    """Synthetic findings so the alert UI can be previewed (/api/alerts?demo=1).
    These are illustrative examples, NOT real detections."""
    return {
        "contention": [
            {"name": "ffmpeg.exe", "label": "Ffmpeg", "severity": "high", "kind": "CPU",
             "detail": "Ffmpeg is averaging 78% of total CPU while the system is "
                       "saturated (91% avg) — likely starving other processes of CPU time.",
             "suggestion": "If this is a tool you're intentionally running (a build, a "
                           "model, a VM, an export), heavy use is normal. If it's "
                           "unexpected, close it or investigate why it's busy.",
             "win_cpu": 78.0, "ram_share": 4.0},
            {"name": None, "label": "System", "severity": "medium", "kind": "RAM",
             "system": True,
             "detail": "Memory is nearly full (~90% avg) without one dominant app — "
                       "the system may be paging to disk, which slows everything.",
             "suggestion": "Close some apps/tabs, or see the upgrade guidance below."},
        ],
        "suspicious": [
            {"name": "svchost.exe", "label": "Svchost", "severity": "high",
             "kind": "Masquerade",
             "exe_path": "C:\\Users\\you\\AppData\\Local\\Temp\\svchost.exe",
             "reasons": ["Named like the Windows system process 'svchost.exe', but "
                         "running from C:\\Users\\you\\AppData\\Local\\Temp instead of "
                         "the Windows system folder.",
                         "It is not validly digitally signed."],
             "action": "Verify it: right-click the .exe → Properties → Digital "
                       "Signatures, search the name online, and run a Microsoft "
                       "Defender scan. If you don't recognise it, treat it as suspect.",
             "cpu": 40},
        ],
        "scanned_at": None, "scanned_count": 293, "has_high": True, "clear": False,
        "note": ("DEMO preview — these are example findings, not real detections. "
                 "Heuristic signals only, not antivirus."),
    }


def run_server(collector=None) -> None:
    app = create_app(collector)
    # threaded=True so concurrent dashboard polls don't block; never use the
    # reloader/debugger inside a background thread.
    app.run(
        host=config.HOST,
        port=config.PORT,
        threaded=True,
        use_reloader=False,
        debug=False,
    )
