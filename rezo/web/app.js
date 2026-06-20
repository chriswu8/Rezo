"use strict";

// ---------- helpers ----------
const $ = (id) => document.getElementById(id);
const COL = { cpu:"#5b9dff", ram:"#7c5cff", gpu:"#2dd4bf", net:"#f59e0b",
              net2:"#fbbf24", disk:"#f472b6", disk2:"#fb7185" };

function fmtBytes(n) {
  if (n == null) return "—";
  const u = ["B","KB","MB","GB","TB","PB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
const fmtBps = (n) => n == null ? "—" : fmtBytes(n) + "/s";
const pad = (x) => String(x).padStart(2, "0");

function fmtLabel(t, range) {
  const d = new Date(t * 1000);
  if (range === "day")   return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (range === "week")  return `${d.getMonth()+1}/${d.getDate()} ${pad(d.getHours())}h`;
  if (range === "month") return `${d.getMonth()+1}/${d.getDate()}`;
  return d.toLocaleString(undefined, { month: "short" }) +
         (d.getMonth() === 0 ? ` '${String(d.getFullYear()).slice(2)}` : "");
}
function relTime(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now()/1000 - ts);
  if (s < 86400) return "today";
  const days = Math.floor(s/86400);
  if (days < 30)  return `${days}d ago`;
  if (days < 365) return `${Math.floor(days/30)}mo ago`;
  return `${Math.floor(days/365)}y ago`;
}
const statusFor = (v, watch, tight) => v >= tight ? "tight" : v >= watch ? "watch" : "ok";
const barColor = (s) => s === "tight" ? "#f87171" : s === "watch" ? "#fbbf24" : "#34d399";

// ---------- chart factory ----------
Chart.defaults.color = "#8a97b1";
Chart.defaults.font.family = "Segoe UI, system-ui, sans-serif";
Chart.defaults.font.size = 11;
const charts = {};

function makeChart(id, datasets, { pct=false, fmtY=null } = {}) {
  const ctx = $(id).getContext("2d");
  charts[id] = new Chart(ctx, {
    type: "line",
    data: { labels: [], datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: false, interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0, hitRadius: 8 }, line: { borderWidth: 1.8, tension: .25 } },
      plugins: {
        legend: { display: datasets.length > 1, labels: { boxWidth: 10, boxHeight: 10, padding: 12 } },
        tooltip: {
          callbacks: {
            label: (c) => {
              const v = c.parsed.y;
              if (v == null) return `${c.dataset.label}: —`;
              return `${c.dataset.label}: ${pct ? v.toFixed(1)+"%" : (fmtY ? fmtY(v) : v)}`;
            },
          },
        },
      },
      scales: {
        x: { grid: { color: "#1c2638" }, ticks: { maxTicksLimit: 8, maxRotation: 0 } },
        y: {
          beginAtZero: true, grid: { color: "#1c2638" },
          suggestedMax: pct ? 100 : undefined,
          ticks: { callback: (v) => pct ? v + "%" : (fmtY ? fmtY(v) : v) },
        },
      },
    },
  });
  return charts[id];
}

function ds(label, color, fill=true) {
  return {
    label, borderColor: color, backgroundColor: color + "22",
    fill, data: [], spanGaps: true,
  };
}

function initCharts() {
  makeChart("chart-cpu", [ds("CPU %", COL.cpu)], { pct: true });
  makeChart("chart-ram", [ds("Memory %", COL.ram)], { pct: true });
  makeChart("chart-gpu", [ds("GPU %", COL.gpu)], { pct: true });
  makeChart("chart-net", [ds("Down", COL.net), ds("Up", COL.net2, false)],
            { fmtY: (v) => fmtBytes(v) + "/s" });
  makeChart("chart-disk", [ds("Read", COL.disk), ds("Write", COL.disk2, false)],
            { fmtY: (v) => fmtBytes(v) + "/s" });
}

function updateChart(id, labels, series) {
  const c = charts[id];
  c.data.labels = labels;
  series.forEach((s, i) => { c.data.datasets[i].data = s; });
  c.update("none");
}

// ---------- gauges ----------
function gauge(label, valText, sub, pct, status) {
  const width = pct == null ? 0 : Math.min(100, Math.max(0, pct));
  const col = status ? barColor(status) : COL.cpu;
  const bar = pct == null ? "" :
    `<div class="bar"><span style="width:${width}%;background:${col}"></span></div>`;
  return `<div class="gauge">
      <div class="glabel">${label}</div>
      <div class="gval">${valText}</div>
      <div class="gsub">${sub || ""}</div>${bar}</div>`;
}

function renderGauges(s, specs) {
  const sys = s.sys || {};
  const gpuTxt = sys.gpu_util == null ? "N/A" : sys.gpu_util.toFixed(0) + "%";
  let gpuSub = specs.gpu_name || "";
  if (sys.gpu_mem_used)  // only when >0 (integrated GPUs report 0 dedicated VRAM)
    gpuSub = fmtBytes(sys.gpu_mem_used) + (sys.gpu_mem_total ? " / " + fmtBytes(sys.gpu_mem_total) : " used");

  const drives = sys.per_drive || [];
  const dTotal = drives.reduce((a,d)=>a+d.total,0);
  const dFree  = drives.reduce((a,d)=>a+d.free,0);
  const dUsedPct = dTotal ? (1 - dFree/dTotal) * 100 : 0;
  const driveSub = drives.map(d => `${d.mount} ${fmtBytes(d.free)} free`).join(" · ");

  const cards = [
    gauge("CPU", (sys.cpu ?? 0).toFixed(0) + "%",
          `${specs.cpu_cores_logical || "?"} threads` +
          (sys.cpu_freq ? ` · ${(sys.cpu_freq/1000).toFixed(2)} GHz` : ""),
          sys.cpu, statusFor(sys.cpu ?? 0, 70, 90)),
    gauge("Memory", (sys.ram_pct ?? 0).toFixed(0) + "%",
          `${fmtBytes(sys.ram_used)} / ${fmtBytes(sys.ram_total)}`,
          sys.ram_pct, statusFor(sys.ram_pct ?? 0, 80, 90)),
    gauge("GPU", gpuTxt, gpuSub, sys.gpu_util,
          sys.gpu_util == null ? null : statusFor(sys.gpu_util, 60, 85)),
    gauge("Disk", fmtBytes(dFree) + " free",
          driveSub || "—", dUsedPct, statusFor(dUsedPct, 80, 90)),
    gauge("Network", "↓ " + fmtBps(sys.net_recv_bps),
          "↑ " + fmtBps(sys.net_sent_bps), null, null),
    gauge("Processes", (sys.proc_count ?? "—") + "",
          (sys.thread_count != null ? sys.thread_count + " threads" : "running"), null, null),
  ];
  $("gauges").innerHTML = cards.join("");
}

// ---------- alerts (contention + suspicious processes) ----------
let hogNames = new Set(), threatNames = new Set();
const sevRank = { high: 3, medium: 2, info: 1 };

function renderAlerts(d) {
  hogNames = new Set(d.contention.filter(c => c.name).map(c => c.name));
  threatNames = new Set(d.suspicious.map(s => s.name));

  const el = $("alerts");
  if (d.clear) {
    el.className = "alerts-sec clear";
    el.innerHTML = `<div class="alert-ok">✅ No resource contention or suspicious
      processes detected${d.scanned_count ? ` · scanned ${d.scanned_count} processes` : ""}.</div>`;
    renderApps();
    return;
  }
  el.className = "alerts-sec" + (d.has_high ? " critical" : "");

  const cards = [];

  // suspicious first — these are the scary ones
  for (const s of d.suspicious) {
    const reasons = (s.reasons || []).map(r => `<li>${esc(r)}</li>`).join("");
    cards.push(`<div class="alert threat ${s.severity}">
      <div class="alert-top"><span class="alert-ic">🛡️</span>
        <span class="alert-title">Possible threat — ${esc(s.label)}
          <span class="mono">(${esc(s.name)})</span></span>
        <span class="sevpill ${s.severity}">${s.severity}</span></div>
      <div class="alert-kind">${esc(s.kind)}</div>
      <ul class="alert-reasons">${reasons}</ul>
      <div class="alert-path mono">${esc(s.exe_path || "")}</div>
      <div class="alert-action">→ ${esc(s.action || "")}</div>
    </div>`);
  }

  // then contention
  for (const c of d.contention) {
    const icon = c.system ? "📊" : "⚠️";
    const title = c.system
      ? `System ${esc(c.kind)} pressure`
      : `${esc(c.label)} is hogging ${esc(c.kind)}`;
    cards.push(`<div class="alert hog ${c.severity}">
      <div class="alert-top"><span class="alert-ic">${icon}</span>
        <span class="alert-title">${title}</span>
        <span class="sevpill ${c.severity}">${c.severity}</span></div>
      <div class="alert-detail">${esc(c.detail)}</div>
      <div class="alert-action">→ ${esc(c.suggestion || "")}</div>
    </div>`);
  }

  const header = `<div class="alerts-head">
      <span class="alerts-h">${d.has_high ? "⚠️ Attention needed" : "Heads up"}</span>
      <span class="muted">${d.contention.length} contention · ${d.suspicious.length} suspicious</span>
    </div>`;
  const foot = d.suspicious.length
    ? `<div class="alerts-note">${esc(d.note || "")}</div>` : "";
  el.innerHTML = header + `<div class="alert-cards">${cards.join("")}</div>` + foot;
  renderApps();
}

// ---------- apps table ----------
let lastApps = [], currentSort = "avg_cpu";

function renderApps() {
  const max = Math.max(1, ...lastApps.map(a => a[currentSort] || 0));
  const rows = [...lastApps].sort((a,b) => (b[currentSort]||0) - (a[currentSort]||0)).slice(0, 25);
  const isCpu = currentSort === "avg_cpu";
  $("apps-body").innerHTML = rows.map((a, i) => {
    const metric = a[currentSort] || 0;
    const w = Math.max(3, (metric / max) * 70);
    const mbar = `<span class="minibar" style="width:${w}px;background:${isCpu?COL.cpu:COL.ram}"></span>`;
    const newb = a.is_new ? `<span class="badge-new">🟢 new</span>` : "";
    const hogb = hogNames.has(a.name) ? `<span class="badge-hog">⚠️ hog</span>` : "";
    const thr = threatNames.has(a.name) ? `<span class="badge-threat">🛡️ check</span>` : "";
    return `<tr class="${threatNames.has(a.name)?'row-threat':hogNames.has(a.name)?'row-hog':''}">
      <td>${i+1}</td>
      <td><div class="appname">${isCpu?"":mbar}<b>${esc(a.label)}</b>${newb}${hogb}${thr}
          ${isCpu?mbar:""}</div></td>
      <td class="num">${a.avg_cpu.toFixed(1)}%</td>
      <td class="num">${a.max_cpu.toFixed(1)}%</td>
      <td class="num">${fmtBytes(a.avg_ram)}</td>
      <td class="num">${fmtBytes(a.max_ram)}</td>
      <td class="num">${a.instances ?? "—"}</td>
      <td title="${a.first_seen ? new Date(a.first_seen*1000).toLocaleString() : ""}">${relTime(a.first_seen)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">No app data yet — the collector logs apps every 30s.</td></tr>`;
}
const esc = (s) => (s||"").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

// ---------- recommendations ----------
function renderReco(r) {
  $("reco-summary").textContent = r.summary;

  const h = r.health, hc = [];
  const card = (name, pill, big, sub) =>
    `<div class="hcard"><div class="htop"><span class="hname">${name}</span>
       <span class="hpill ${pill}">${pill}</span></div>
       <div class="hsub">${big}</div><div class="hsub">${sub}</div></div>`;
  hc.push(card("CPU", h.cpu.status, `p90 ${h.cpu.p90}% · ${h.cpu.headroom}% headroom`, `median ${h.cpu.p50}%`));
  hc.push(card("Memory", h.ram.status, `p90 ${h.ram.p90}% · ${h.ram.headroom}% headroom`, `peak ${h.ram.max}%`));
  hc.push(card("Disk", h.disk.status, `${h.disk.free_gb} GB free`, `${h.disk.free_pct}% free`));
  if (h.gpu) hc.push(card("GPU", h.gpu.status, `p90 ${h.gpu.p90}%`, `${h.gpu.headroom}% headroom`));
  $("health").innerHTML = hc.join("");

  if (r.now.length === 0) {
    $("reco-now").innerHTML =
      `<div class="reco-empty">✅ Healthy headroom across CPU, memory, disk${h.gpu?", and GPU":""}. No upgrade purchases needed right now.</div>`;
  } else {
    $("reco-now").innerHTML = r.now.map(n => `
      <div class="reco ${n.severity}">
        <div class="rtag">${n.resource} · ${n.severity}</div>
        <h4>${esc(n.title)}</h4>
        <p>${esc(n.detail)}</p>
        <p class="sugg">→ ${esc(n.suggestion)}</p>
      </div>`).join("");
  }

  $("reco-planning").innerHTML = r.planning.map(p => `
    <div class="reco">
      <h4>${esc(p.title)}</h4>
      <p>${esc(p.detail)}</p>
      <p class="trigger">When: ${esc(p.trigger)}</p>
    </div>`).join("");
}

// ---------- data loading ----------
let specs = {}, currentRange = "day";

async function jget(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.status);
  return res.json();
}

async function loadSummary() {
  try {
    const s = await jget("/api/summary");
    specs = s.specs;
    $("specline").textContent =
      `${specs.cpu_name} · ${specs.cpu_cores_logical} threads · ${fmtBytes(specs.ram_total)} RAM · ${specs.gpu_name}`;
    const dot = $("status-dot"), txt = $("status-text");
    if (s.paused === true) { dot.className = "dot paused"; txt.textContent = "paused"; }
    else { dot.className = "dot live"; txt.textContent = "collecting"; }
    const up = s.boot_time ? (s.now - s.boot_time) : 0;
    $("uptime").textContent = "uptime " + fmtDur(up);
    $("dataspan").textContent = "history " + (s.data_days >= 1 ?
      s.data_days.toFixed(1) + "d" : Math.round(s.data_days*24) + "h");
    renderGauges(s, specs);
    $("cpu-now").textContent = (s.sys.cpu ?? 0).toFixed(0) + "%";
    $("ram-now").textContent = (s.sys.ram_pct ?? 0).toFixed(0) + "%";
    $("gpu-now").textContent = s.sys.gpu_util == null ? "N/A" : s.sys.gpu_util.toFixed(0) + "%";
  } catch (e) {
    $("status-dot").className = "dot dead";
    $("status-text").textContent = "collector offline";
  }
}
function fmtDur(s) {
  s = Math.floor(s); const d = Math.floor(s/86400); s%=86400;
  const h = Math.floor(s/3600); const m = Math.floor((s%3600)/60);
  return (d?`${d}d `:"") + (h?`${h}h `:"") + `${m}m`;
}

async function loadRange() {
  const r = currentRange;
  $("apps-range-label").textContent = `· past ${{day:"24 hours",week:"7 days",month:"30 days",year:"12 months"}[r]}`;
  try {
    const ts = await jget("/api/timeseries?range=" + r);
    const pts = ts.points;
    const labels = pts.map(p => fmtLabel(p.t, r));
    updateChart("chart-cpu", labels, [pts.map(p => p.cpu)]);
    updateChart("chart-ram", labels, [pts.map(p => p.ram_pct)]);
    updateChart("chart-gpu", labels, [pts.map(p => p.gpu_util)]);
    updateChart("chart-net", labels, [pts.map(p=>p.net_recv_bps), pts.map(p=>p.net_sent_bps)]);
    updateChart("chart-disk", labels, [pts.map(p=>p.disk_read_bps), pts.map(p=>p.disk_write_bps)]);
  } catch (e) { /* keep last */ }
  try {
    const ap = await jget("/api/apps?range=" + r);
    lastApps = ap.apps; renderApps();
  } catch (e) { /* keep last */ }
}

async function loadReco() {
  try { renderReco(await jget("/api/recommendations")); }
  catch (e) { $("reco-summary").textContent = "Recommendations unavailable."; }
}

async function loadAlerts() {
  // /?demo=1 previews the full dashboard with example alerts rendered
  const demo = location.search.includes("demo=1") ? "?demo=1" : "";
  try { renderAlerts(await jget("/api/alerts" + demo)); }
  catch (e) { /* keep last state */ }
}

// ---------- wiring ----------
function wireTabs() {
  $("rangetabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#rangetabs button").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); currentRange = b.dataset.range; loadRange();
  });
  $("sorttabs").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    document.querySelectorAll("#sorttabs button").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); currentSort = b.dataset.sort; renderApps();
  });
}

function boot() {
  initCharts(); wireTabs();
  loadSummary(); loadRange(); loadReco(); loadAlerts();
  setInterval(loadSummary, 4000);
  setInterval(loadRange, 30000);
  setInterval(loadReco, 60000);
  setInterval(loadAlerts, 12000);
}
boot();
