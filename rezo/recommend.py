"""Upgrade recommendation engine.

Two distinct outputs, matching the brief:
  * "now"      -- purchases recommended ONLY when a resource is *sustained*
                  strained (p90 over the evaluation window crosses a threshold).
                  Empty list == healthy, say so plainly. No upselling.
  * "planning" -- forward-looking, profile-aware tips about what the next thing
                  you build will demand, framed as non-urgent guidance.

For an AI-app builder the headline is usually GPU VRAM: cloud-model workflows
(Claude Code, the API) lean on CPU/RAM, but *local* models need a discrete GPU.
"""
from __future__ import annotations

from . import config, metrics, queries

GB = 1024 ** 3


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = (p / 100.0) * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def _is_discrete_gpu(name: str) -> bool:
    n = (name or "").lower()
    discrete = ("nvidia", "geforce", "rtx", "gtx", "quadro", "tesla",
                "radeon rx", "radeon pro", "arc a", "arc b")
    integrated = ("intel(r) graphics", "intel graphics", "uhd", "iris",
                  "radeon graphics", "microsoft basic", "vega")
    if any(k in n for k in discrete):
        return True
    if any(k in n for k in integrated):
        return False
    return False


def _status(value: float, watch: float, tight: float) -> str:
    if value >= tight:
        return "tight"
    if value >= watch:
        return "watch"
    return "ok"


def build() -> dict:
    specs = metrics.get_specs()
    days = config.RECO_WINDOW_DAYS
    span = queries.data_span_days()
    ram_gb = specs["ram_total"] / GB
    cores = specs["cpu_cores_logical"] or 1
    discrete = _is_discrete_gpu(specs["gpu_name"])

    cpu_vals = queries.column_values("cpu", days)
    ram_vals = queries.column_values("ram_pct", days)
    gpu_vals = queries.column_values("gpu_util", days)
    latest = queries.latest_sys() or {}

    cpu_p90 = _pct(cpu_vals, 90)
    cpu_p50 = _pct(cpu_vals, 50)
    ram_p90 = _pct(ram_vals, 90)
    ram_p50 = _pct(ram_vals, 50)
    ram_max = max(ram_vals) if ram_vals else 0.0
    gpu_p90 = _pct(gpu_vals, 90) if gpu_vals else None

    # disk (aggregate of fixed drives)
    disk_total = latest.get("disk_total") or 0
    disk_free = latest.get("disk_free") or 0
    free_pct = (disk_free / disk_total * 100) if disk_total else 100.0
    free_gb = disk_free / GB

    # swap pressure
    swap_used = latest.get("swap_used") or 0
    swap_total = latest.get("swap_total") or 0
    swap_pct = (swap_used / swap_total * 100) if swap_total else 0.0

    health = {
        "cpu": {
            "p50": round(cpu_p50, 1), "p90": round(cpu_p90, 1),
            "headroom": round(100 - cpu_p90, 1),
            "status": _status(cpu_p90, 70, config.CPU_P90_WARN),
        },
        "ram": {
            "p50": round(ram_p50, 1), "p90": round(ram_p90, 1),
            "max": round(ram_max, 1),
            "headroom": round(100 - ram_p90, 1),
            "status": _status(ram_p90, config.RAM_P90_WARN, config.RAM_P90_HIGH),
        },
        "disk": {
            "free_gb": round(free_gb, 1), "free_pct": round(free_pct, 1),
            "status": _status(100 - free_pct, 80, 100 - config.DISK_FREE_PCT_WARN),
        },
        "gpu": None if gpu_p90 is None else {
            "p90": round(gpu_p90, 1), "headroom": round(100 - gpu_p90, 1),
            "status": _status(gpu_p90, 60, config.GPU_P90_WARN),
        },
    }

    now: list[dict] = []
    enough = span >= 0.25  # ~6h of data before we make purchase calls

    if enough and ram_p90 >= config.RAM_P90_WARN:
        sev = "high" if ram_p90 >= config.RAM_P90_HIGH else "moderate"
        target = 64 if ram_gb < 32 else (128 if ram_gb < 64 else int(ram_gb * 2))
        now.append({
            "resource": "RAM",
            "severity": sev,
            "title": f"RAM is the bottleneck ({ram_p90:.0f}% used at p90)",
            "detail": (
                f"Memory sits at {ram_p90:.0f}% nine-tenths of the time"
                + (f", and swap is active ({swap_pct:.0f}% of page file in use)."
                   if swap_pct >= config.SWAP_PRESSURE_PCT else ".")
                + f" You have {ram_gb:.0f} GB."
            ),
            "suggestion": f"Add memory to ~{target} GB. This is the cheapest, "
                          "highest-impact upgrade when memory is the limiter.",
        })

    if enough and cpu_p90 >= config.CPU_P90_WARN:
        now.append({
            "resource": "CPU",
            "severity": "moderate",
            "title": f"CPU is saturated ({cpu_p90:.0f}% at p90)",
            "detail": f"Your {cores}-thread CPU runs near capacity under load. "
                      "This caps parallel builds, test suites, and many agents.",
            "suggestion": "First try capping parallelism / closing background load. "
                          "If sustained, a higher core-count CPU is the upgrade.",
        })

    if enough and (free_pct < config.DISK_FREE_PCT_WARN or free_gb < config.DISK_FREE_GB_WARN):
        now.append({
            "resource": "Storage",
            "severity": "high" if free_gb < 15 else "moderate",
            "title": f"Disk is filling up ({free_gb:.0f} GB free, {free_pct:.0f}%)",
            "detail": "Low free space slows writes and blocks large checkouts, "
                      "container images, datasets, and model weights.",
            "suggestion": "Free space or add an NVMe SSD. Keep 100+ GB free if you "
                          "work with models, datasets, or containers.",
        })

    if gpu_p90 is not None and gpu_p90 >= config.GPU_P90_WARN and discrete:
        now.append({
            "resource": "GPU",
            "severity": "moderate",
            "title": f"GPU is heavily used ({gpu_p90:.0f}% at p90)",
            "detail": "Your discrete GPU is a bottleneck for current workloads.",
            "suggestion": "A higher-VRAM / newer GPU would lift local-model and "
                          "graphics throughput.",
        })

    # ---- forward-looking, profile-aware planning ----
    planning: list[dict] = []

    if not discrete:
        planning.append({
            "title": "Local AI models need a discrete GPU (your biggest gap)",
            "detail": (
                f"Your GPU is integrated ({specs['gpu_name']}), which shares system "
                "RAM and can't run local LLMs, Stable Diffusion, or fine-tuning at "
                "useful speed. If you plan to run models on-device — 7-13B LLMs, "
                "image/video gen, or training — a discrete GPU with >=16 GB VRAM is "
                "the single highest-leverage purchase (RTX 4070 Ti Super 16GB or "
                "4080 for inference; a used 3090/4090 24GB for bigger models)."
            ),
            "trigger": "Only if you move from cloud models to running models locally.",
        })
    else:
        planning.append({
            "title": "Mind VRAM as local models grow",
            "detail": "You have a discrete GPU. Model size is gated by VRAM: ~8 GB "
                      "runs small models, 16 GB comfortably runs 13B-class, 24 GB+ "
                      "opens 30B-class and light fine-tuning.",
            "trigger": "If you start running noticeably larger local models.",
        })

    if ram_gb < 32:
        planning.append({
            "title": "Containers and multiple services will want more RAM",
            "detail": f"You have {ram_gb:.0f} GB. Docker stacks, local databases, "
                      "and several dev servers at once add up fast; 32-64 GB is the "
                      "comfortable range for that style of work.",
            "trigger": "If you start running container stacks or several services.",
        })
    else:
        planning.append({
            "title": f"RAM ({ram_gb:.0f} GB) is ample for orchestration-heavy work",
            "detail": "Plenty of headroom for Docker stacks, local databases, many "
                      "dev servers, and running multiple agents in parallel. RAM is "
                      "unlikely to be your limiter for cloud-model app building.",
            "trigger": "Re-evaluate only if you keep many heavy VMs/containers live.",
        })

    planning.append({
        "title": (f"Your {cores}-thread CPU is built for parallelism"
                  if cores >= 16 else "CPU may bound heavy parallel builds"),
        "detail": (
            f"{cores} logical cores handle parallel builds, large test suites, and "
            "several concurrent agents/sandboxes well — a strong base for "
            "Claude-Code-driven, multi-project building."
            if cores >= 16 else
            f"{cores} logical cores is modest for heavy parallel compiles and many "
            "concurrent agents; consider more cores if builds dominate your time."
        ),
        "trigger": "Watch CPU p90 here; upgrade only if it stays pinned.",
    })

    planning.append({
        "title": "Keep fast SSD headroom for models, datasets & containers",
        "detail": "AI/dev work is storage-hungry: model weights (GBs each), "
                  "datasets, container layers, and node_modules. Keep 100+ GB free "
                  "on a fast NVMe; add capacity before it gets tight, not after.",
        "trigger": f"Currently {free_gb:.0f} GB free.",
    })

    # ---- one-line summary ----
    if not enough:
        summary = (f"Collecting baseline data ({span:.1f} day(s) so far) — purchase "
                   "recommendations sharpen as history builds. Planning tips below "
                   "are based on your hardware profile.")
    elif now:
        worst = "high" if any(r["severity"] == "high" for r in now) else "moderate"
        kinds = ", ".join(sorted({r["resource"] for r in now}))
        summary = (f"{len(now)} upgrade(s) worth considering ({kinds}). "
                   + ("One or more resources are tight." if worst == "high"
                      else "Pressure is moderate, not urgent."))
    else:
        summary = ("Your system has healthy headroom — no upgrades needed right now. "
                   "See planning tips for what your next builds may demand.")

    return {
        "generated_for": {
            "cpu_name": specs["cpu_name"],
            "cores": cores,
            "ram_gb": round(ram_gb, 1),
            "gpu_name": specs["gpu_name"],
            "discrete_gpu": discrete,
        },
        "data_days": span,
        "enough_data": enough,
        "health": health,
        "now": now,
        "planning": planning,
        "summary": summary,
    }
