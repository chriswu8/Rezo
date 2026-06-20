"""Hardware specs + best-effort GPU sampling.

GPU support on Windows is messy, so this is layered and fails gracefully:
  1. nvidia-smi  -> exact utilization + VRAM (discrete NVIDIA cards)
  2. Get-Counter -> sum of GPU engine utilization (works on Intel/AMD iGPUs)
  3. give up    -> util/mem reported as None (dashboard shows "N/A")

Everything here is read out-of-band on a slow cadence so the 10s system loop
never blocks on a PowerShell spawn.
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys

import psutil

_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW

_specs_cache: dict | None = None
_gpu_method: str | None = None  # 'nvidia' | 'powershell' | 'none'


def _run(cmd: list[str], timeout: float = 12.0) -> str | None:
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        if out.returncode == 0:
            return out.stdout
    except Exception:
        pass
    return None


def _ps(script: str, timeout: float = 12.0) -> str | None:
    return _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )


# --------------------------------------------------------------------------
# Static specs (computed once)
# --------------------------------------------------------------------------
def get_specs() -> dict:
    global _specs_cache
    if _specs_cache is not None:
        return _specs_cache

    vm = psutil.virtual_memory()
    cpu_name = platform.processor() or "Unknown CPU"
    gpu_name = "Unknown GPU"
    gpu_vram = None

    if sys.platform == "win32":
        out = _ps(
            "(Get-CimInstance Win32_Processor | Select-Object -First 1)"
            ".Name"
        )
        if out and out.strip():
            cpu_name = out.strip()
        gout = _ps(
            "Get-CimInstance Win32_VideoController | "
            "Select-Object -First 1 -ExpandProperty Name"
        )
        if gout and gout.strip():
            gpu_name = gout.strip()

    _specs_cache = {
        "cpu_name": cpu_name,
        "cpu_cores_physical": psutil.cpu_count(logical=False) or psutil.cpu_count(),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "ram_total": vm.total,
        "gpu_name": gpu_name,
        "gpu_vram": gpu_vram,
        "platform": f"{platform.system()} {platform.release()}",
        "hostname": platform.node(),
        "python": platform.python_version(),
    }
    return _specs_cache


# --------------------------------------------------------------------------
# Live GPU sampling
# --------------------------------------------------------------------------
def _detect_gpu_method() -> str:
    global _gpu_method
    if _gpu_method is not None:
        return _gpu_method
    if shutil.which("nvidia-smi"):
        _gpu_method = "nvidia"
    elif sys.platform == "win32":
        _gpu_method = "powershell"
    else:
        _gpu_method = "none"
    return _gpu_method


def _sample_nvidia() -> dict:
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits",
        ],
        timeout=8.0,
    )
    if not out:
        return {"gpu_util": None, "gpu_mem_used": None, "gpu_mem_total": None}
    # Sum across GPUs; report combined.
    util_sum = 0.0
    mem_used = 0
    mem_total = 0
    n = 0
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            util_sum += float(parts[0])
            mem_used += int(float(parts[1])) * 1024 * 1024
            mem_total += int(float(parts[2])) * 1024 * 1024
            n += 1
        except ValueError:
            continue
    if n == 0:
        return {"gpu_util": None, "gpu_mem_used": None, "gpu_mem_total": None}
    return {
        "gpu_util": round(util_sum / n, 1),
        "gpu_mem_used": mem_used,
        "gpu_mem_total": mem_total,
    }


def _sample_powershell_gpu() -> dict:
    # Sum engine utilization across all GPU engines/processes (Task-Manager-ish
    # activity signal), and dedicated GPU memory in use. VRAM total is unknown
    # for shared-memory iGPUs, so it is left None.
    script = (
        "$u = (Get-Counter '\\GPU Engine(*)\\Utilization Percentage' "
        "-ErrorAction SilentlyContinue).CounterSamples | "
        "Measure-Object -Property CookedValue -Sum | "
        "Select-Object -ExpandProperty Sum; "
        "$m = (Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage' "
        "-ErrorAction SilentlyContinue).CounterSamples | "
        "Measure-Object -Property CookedValue -Sum | "
        "Select-Object -ExpandProperty Sum; "
        "Write-Output (\"{0};{1}\" -f $u, $m)"
    )
    out = _ps(script, timeout=12.0)
    if not out or ";" not in out:
        return {"gpu_util": None, "gpu_mem_used": None, "gpu_mem_total": None}
    try:
        u_str, m_str = out.strip().splitlines()[-1].split(";")
        util = float(u_str) if u_str.strip() else 0.0
        mem = int(float(m_str)) if m_str.strip() else 0
    except (ValueError, IndexError):
        return {"gpu_util": None, "gpu_mem_used": None, "gpu_mem_total": None}
    return {
        "gpu_util": round(min(util, 100.0), 1),
        "gpu_mem_used": mem,
        "gpu_mem_total": None,
    }


def sample_gpu() -> dict:
    method = _detect_gpu_method()
    if method == "nvidia":
        return _sample_nvidia()
    if method == "powershell":
        return _sample_powershell_gpu()
    return {"gpu_util": None, "gpu_mem_used": None, "gpu_mem_total": None}
