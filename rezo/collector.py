"""The background collector: samples the system and per-app usage into SQLite.

Threads (all daemons, all individually crash-isolated):
  * system loop   -- every SYS_INTERVAL: one sys_samples row
  * process loop  -- every PROC_INTERVAL: top-K apps -> proc_samples + registry
  * gpu loop      -- every GPU_INTERVAL: refresh the shared GPU cache
  * maintenance   -- hourly: roll up completed days, prune, checkpoint WAL

CPU correctness notes (see advisor):
  * Per-process cpu_percent is a delta since the last call on the SAME Process
    object. psutil.process_iter() keeps a module cache, so we must iterate
    through it (never build our own {pid: Process}) for deltas to accumulate.
  * We prime once at startup and let the first interval elapse before the first
    real sweep, so no bogus 0%/spike rows are written.
  * Per-app CPU = sum(pid cpu_percent) / logical_cores, capped at 100, to match
    Task Manager's "% of total CPU".
"""
from __future__ import annotations

import sys
import threading
import time
import traceback

import psutil

from . import config, metrics, security
from .storage import Writer

_NCORES = psutil.cpu_count(logical=True) or 1
_PROC_ERRORS = (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess)
# Pseudo-processes that aren't real apps. "System Idle Process" in particular is
# the *inverse* of usage (idle %), so it would otherwise always rank #1 by CPU.
_EXCLUDE_NAMES = {"system idle process", "idle"}


def _log(msg: str) -> None:
    print(f"[collector] {msg}", file=sys.stderr, flush=True)


class Collector:
    def __init__(self, writer: Writer | None = None) -> None:
        self.writer = writer or Writer()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._threads: list[threading.Thread] = []

        # psutil's system cpu_percent needs a warm-up; drop the first stored row.
        self._sys_first = True

        # shared state read by the API (plain dict access is GIL-safe enough)
        self.latest: dict = {}
        self.latest_apps: list[dict] = []
        self._gpu: dict = {"gpu_util": None, "gpu_mem_used": None, "gpu_mem_total": None}
        self._thread_count: int | None = None
        self.security: dict = {"suspicious": [], "scanned_at": None, "scanned_count": 0}

        # delta baselines for cumulative counters
        self._prev_disk = None
        self._prev_net = None
        self._prev_t = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._prime()
        for target in (self._sys_loop, self._proc_loop, self._gpu_loop,
                       self._sec_loop, self._maint_loop):
            t = threading.Thread(target=self._guarded, args=(target,), daemon=True)
            t.start()
            self._threads.append(t)
        _log("started")

    def stop(self) -> None:
        self._stop.set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    @property
    def paused(self) -> bool:
        return self._paused.is_set()

    def _prime(self) -> None:
        psutil.cpu_percent(None)
        for p in psutil.process_iter():
            try:
                p.cpu_percent(None)
            except _PROC_ERRORS:
                continue
        # snapshot baselines for delta counters
        self._prev_disk = psutil.disk_io_counters()
        self._prev_net = psutil.net_io_counters()
        self._prev_t = time.time()

    def _guarded(self, fn) -> None:
        """Run a loop target; never let an exception kill the thread silently."""
        while not self._stop.is_set():
            try:
                fn()
                return  # fn runs its own loop; returning means stop requested
            except Exception:
                _log("loop crashed, restarting in 5s:\n" + traceback.format_exc())
                self._stop.wait(5)

    def _wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`, returns False if we should stop."""
        return not self._stop.wait(seconds)

    # -- loops ------------------------------------------------------------
    def _sys_loop(self) -> None:
        while self._wait(config.SYS_INTERVAL):
            if self._paused.is_set():
                continue
            self._sample_system()

    def _proc_loop(self) -> None:
        while self._wait(config.PROC_INTERVAL):
            if self._paused.is_set():
                continue
            self._sample_procs()

    def _gpu_loop(self) -> None:
        # sample immediately, then on cadence
        while True:
            if not self._paused.is_set():
                try:
                    self._gpu = metrics.sample_gpu()
                except Exception:
                    pass
            if not self._wait(config.GPU_INTERVAL):
                return

    def _sec_loop(self) -> None:
        # scan shortly after start, then on cadence (crash-isolated via _guarded)
        self._stop.wait(8)
        while True:
            if not self._paused.is_set():
                cpu_by_name = {a["name"]: a["cpu"] for a in self.latest_apps}
                self.security = security.scan_processes(cpu_by_name=cpu_by_name)
            if not self._wait(config.SEC_INTERVAL):
                return

    def _maint_loop(self) -> None:
        # run once shortly after start, then hourly
        self._stop.wait(30)
        while not self._stop.is_set():
            try:
                done = self.writer.rollup_proc_daily()
                self.writer.prune()
                if done:
                    _log(f"rolled up {done} day(s)")
            except Exception:
                _log("maintenance error:\n" + traceback.format_exc())
            if not self._wait(3600):
                return

    # -- sampling ---------------------------------------------------------
    def _sample_system(self) -> None:
        now = time.time()
        ts = int(now)
        cpu = psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        sw = psutil.swap_memory()

        # delta counters (clamp negatives to 0 across reboots/resets)
        dt = max(now - (self._prev_t or now), 1e-6)
        disk = psutil.disk_io_counters()
        net = psutil.net_io_counters()
        dr = dw = ns = nr = 0.0
        if disk and self._prev_disk:
            dr = max(0.0, (disk.read_bytes - self._prev_disk.read_bytes) / dt)
            dw = max(0.0, (disk.write_bytes - self._prev_disk.write_bytes) / dt)
        if net and self._prev_net:
            ns = max(0.0, (net.bytes_sent - self._prev_net.bytes_sent) / dt)
            nr = max(0.0, (net.bytes_recv - self._prev_net.bytes_recv) / dt)
        self._prev_disk, self._prev_net, self._prev_t = disk, net, now

        d_total, d_used, d_free, per_drive = _disk_usage()
        freq = psutil.cpu_freq()
        batt = psutil.sensors_battery()

        row = {
            "ts": ts,
            "cpu": round(cpu, 1),
            "ram_used": vm.used,
            "ram_total": vm.total,
            "ram_pct": round(vm.percent, 1),
            "swap_used": sw.used,
            "swap_total": sw.total,
            "disk_read_bps": round(dr, 1),
            "disk_write_bps": round(dw, 1),
            "net_sent_bps": round(ns, 1),
            "net_recv_bps": round(nr, 1),
            "disk_total": d_total,
            "disk_used": d_used,
            "disk_free": d_free,
            "gpu_util": self._gpu.get("gpu_util"),
            "gpu_mem_used": self._gpu.get("gpu_mem_used"),
            "gpu_mem_total": self._gpu.get("gpu_mem_total"),
            "proc_count": len(psutil.pids()),
            "thread_count": self._thread_count,
            "cpu_freq": round(freq.current, 0) if freq else None,
            "battery_pct": round(batt.percent, 0) if batt else None,
            "on_battery": (0 if (batt and batt.power_plugged) else 1) if batt else None,
        }
        row["per_drive"] = per_drive
        if self._sys_first:
            # warm-up sample: baselines now set, CPU not yet meaningful — skip it
            self._sys_first = False
            return
        self.writer.insert_sys(row)
        self.latest = row

    def _sample_procs(self) -> None:
        ts = int(time.time())
        agg: dict[str, dict] = {}
        paths: dict[str, str | None] = {}
        total_threads = 0
        for p in psutil.process_iter(["name", "memory_info", "num_threads"]):
            try:
                info = p.info
                name = (info.get("name") or "unknown").strip() or "unknown"
                if name.lower() in _EXCLUDE_NAMES:
                    continue
                cpu = p.cpu_percent(None)
                mem = info.get("memory_info")
                rss = mem.rss if mem else 0
                total_threads += info.get("num_threads") or 0
            except _PROC_ERRORS:
                continue
            except Exception:
                continue
            slot = agg.get(name)
            if slot is None:
                slot = agg[name] = {"cpu": 0.0, "ram": 0, "instances": 0}
                paths[name] = None
            slot["cpu"] += cpu
            slot["ram"] += rss
            slot["instances"] += 1

        self._thread_count = total_threads

        # normalize CPU to "% of total system CPU"
        apps = []
        for name, s in agg.items():
            apps.append(
                {
                    "name": name,
                    "cpu": round(min(s["cpu"] / _NCORES, 100.0), 1),
                    "ram": int(s["ram"]),
                    "instances": s["instances"],
                }
            )

        # store the union of top-K by CPU and top-K by RAM
        by_cpu = sorted(apps, key=lambda a: a["cpu"], reverse=True)[: config.PROC_TOP_K]
        by_ram = sorted(apps, key=lambda a: a["ram"], reverse=True)[: config.PROC_TOP_K]
        keep = {a["name"]: a for a in by_cpu}
        keep.update({a["name"]: a for a in by_ram})
        to_store = list(keep.values())

        self.writer.insert_procs(ts, to_store)
        self.writer.touch_registry(ts, {a["name"]: paths.get(a["name"]) for a in to_store})

        # cache a richer set for the live "current top apps" view
        self.latest_apps = sorted(apps, key=lambda a: a["cpu"], reverse=True)[:40]


def _disk_usage():
    """Aggregate + per-drive usage for fixed disks."""
    total = used = free = 0
    per_drive = []
    seen = set()
    for part in psutil.disk_partitions(all=False):
        if part.device in seen:
            continue
        seen.add(part.device)
        opts = (part.opts or "").lower()
        if "cdrom" in opts or part.fstype == "":
            continue
        try:
            u = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        total += u.total
        used += u.used
        free += u.free
        per_drive.append(
            {
                "mount": part.mountpoint,
                "total": u.total,
                "used": u.used,
                "free": u.free,
                "pct": round(u.percent, 1),
            }
        )
    return total, used, free, per_drive
