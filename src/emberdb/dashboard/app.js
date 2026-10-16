// EmberDB dashboard client. Vanilla JS, talks to http_api.py's REST + /ws.

const state = {
  selectedDb: 0,
  dbCount: 16,
  opsHistory: [],
  cdcEvents: [],
  lastCdcOffset: 0,
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- helpers

function fmtBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtUptime(seconds) {
  seconds = Math.floor(seconds);
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function fmtAgo(seconds) {
  if (seconds == null) return "—";
  if (seconds < 1) return "just now";
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

// ---------------------------------------------------------------- KPIs

function renderStats(stats) {
  const branchEl = $("fact-branch");
  if (stats.branch) {
    const dirty = stats.branch.dirty > 0 ? ` (${stats.branch.dirty} changed)` : "";
    branchEl.textContent = stats.branch.current + dirty;
    branchEl.style.color = stats.branch.current === "main" ? "" : "var(--ember-hot)";
  }
  $("fact-role").textContent = stats.replication.role;
  $("fact-uptime").textContent = fmtUptime(stats.uptime_seconds);
  $("fact-version").textContent = stats.version;

  const m = stats.metrics;
  $("kpi-ops").textContent = m.ops_per_sec.toLocaleString();
  $("kpi-clients").textContent = m.connected_clients;
  $("kpi-clients-sub").textContent = `${m.total_connections_received} total seen`;

  const totalHits = m.keyspace_hits, totalMiss = m.keyspace_misses;
  const ratio = (totalHits + totalMiss) > 0 ? (m.hit_rate * 100).toFixed(1) + "%" : "—";
  $("kpi-hitratio").textContent = ratio;
  $("kpi-hitratio-sub").textContent = `${totalHits.toLocaleString()} hits / ${totalMiss.toLocaleString()} misses`;

  const totalKeys = stats.keyspace.reduce((a, d) => a + d.keys, 0);
  const totalBytes = stats.keyspace.reduce((a, d) => a + d.bytes, 0);
  $("kpi-keys").textContent = totalKeys.toLocaleString();
  $("kpi-keys-sub").textContent = `${stats.keyspace.length} db${stats.keyspace.length === 1 ? "" : "s"} in use`;
  $("kpi-memory").textContent = fmtBytes(totalBytes);
  $("kpi-memory-sub").textContent = stats.eviction.maxmemory > 0
    ? `limit ${fmtBytes(stats.eviction.maxmemory)} (${stats.eviction.maxmemory_policy})`
    : "no limit set";

  state.opsHistory = m.ops_history || [];
  drawSparkline();
}

function drawSparkline() {
  const canvas = $("kpi-ops-spark");
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const data = state.opsHistory.slice(-40);
  if (data.length < 2) return;
  const max = Math.max(...data, 1);
  const step = w / (data.length - 1);

  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, "rgba(255, 106, 61, 0.55)");
  grad.addColorStop(1, "rgba(255, 106, 61, 0.02)");

  ctx.beginPath();
  data.forEach((v, i) => {
    const x = i * step, y = h - (v / max) * (h - 4) - 2;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = "#ff6a3d";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  ctx.lineTo(w, h);
  ctx.lineTo(0, h);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
}

// ---------------------------------------------------------------- heat strip

function heatColor(secondsAgo, accessCount) {
  // blends recency (decays over 10 min) and frequency (log-scaled)
  const recency = Math.max(0, 1 - secondsAgo / 600);
  const frequency = Math.min(1, Math.log2(accessCount + 1) / 8);
  const heat = Math.max(recency, frequency * 0.85);

  const cold = [58, 74, 82];
  const mid = [138, 90, 58];
  const hot = [255, 106, 61];
  let c;
  if (heat < 0.5) {
    const t = heat / 0.5;
    c = cold.map((v, i) => Math.round(v + (mid[i] - v) * t));
  } else {
    const t = (heat - 0.5) / 0.5;
    c = mid.map((v, i) => Math.round(v + (hot[i] - v) * t));
  }
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

async function refreshHeatstrip() {
  const el = $("heatstrip");
  try {
    const data = await api(`/api/v1/heatmap?db=${state.selectedDb}&limit=240`);
    if (!data.cells.length) {
      el.innerHTML = `<div class="heat-empty">No keys in db${state.selectedDb} yet — write something and it'll show up here.</div>`;
      return;
    }
    el.innerHTML = "";
    for (const cell of data.cells) {
      const div = document.createElement("div");
      div.className = "heatcell";
      div.style.background = heatColor(cell.last_access_seconds_ago, cell.access_count);
      div.title = `${cell.key}\nlast access: ${fmtAgo(cell.last_access_seconds_ago)}\naccess count: ${cell.access_count}`;
      el.appendChild(div);
    }
  } catch (e) {
    el.innerHTML = `<div class="heat-empty">Couldn't load heat data: ${e.message}</div>`;
  }
}

// ---------------------------------------------------------------- CDC log

function renderCdcEvents(events) {
  if (!events || !events.length) return;
  const log = $("cdc-log");
  const empty = log.querySelector(".cdc-empty");
  if (empty) empty.remove();

  for (const ev of events) {
    const row = document.createElement("div");
    row.className = "cdc-row";
    row.dataset.type = ev.type;
    row.innerHTML = `
      <span class="cdc-row__offset">#${ev.offset}</span>
      <span class="cdc-row__cmd">${ev.cmd}</span>
      <span class="cdc-row__key">${ev.key ?? ""}</span>
    `;
    log.appendChild(row);
    state.lastCdcOffset = Math.max(state.lastCdcOffset, ev.offset);
  }
  $("cdc-offset").textContent = `offset ${state.lastCdcOffset}`;

  while (log.children.length > 100) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

// ---------------------------------------------------------------- keyspace

async function refreshKeyspace() {
  const pattern = $("keyspace-pattern").value || "*";
  const body = $("keytable-body");
  try {
    const data = await api(`/api/v1/keyspace?db=${state.selectedDb}&pattern=${encodeURIComponent(pattern)}&count=100`);
    if (!data.keys.length) {
      body.innerHTML = `<tr><td colspan="5" class="keytable__empty">No matching keys.</td></tr>`;
      return;
    }
    body.innerHTML = data.keys.map(k => `
      <tr>
        <td>${k.key}</td>
        <td><span class="type-pill">${k.type}</span></td>
        <td>${k.ttl != null ? Math.round(k.ttl) + "s" : "—"}</td>
        <td>${fmtAgo(k.last_access_seconds_ago)}</td>
        <td>${k.access_count}</td>
      </tr>
    `).join("");
  } catch (e) {
    body.innerHTML = `<tr><td colspan="5" class="keytable__empty">${e.message}</td></tr>`;
  }
}

function setupDbSelector(dbCount) {
  const sel = $("keyspace-db");
  if (sel.children.length) return;
  for (let i = 0; i < dbCount; i++) {
    const opt = document.createElement("option");
    opt.value = i;
    opt.textContent = `db ${i}`;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => {
    state.selectedDb = parseInt(sel.value, 10);
    $("console-db").textContent = `db ${state.selectedDb}`;
    refreshHeatstrip();
    refreshKeyspace();
  });
  $("keyspace-pattern").addEventListener("keydown", (e) => {
    if (e.key === "Enter") refreshKeyspace();
  });
}

// ---------------------------------------------------------------- console

function appendConsoleLine(text, cls) {
  const out = $("console-output");
  const hint = out.querySelector(".console__hint");
  if (hint) hint.remove();
  const div = document.createElement("div");
  div.className = `console__line ${cls}`;
  div.textContent = text;
  out.appendChild(div);
  out.scrollTop = out.scrollHeight;
}

function formatReply(reply) {
  if (reply === null || reply === undefined) return "(nil)";
  if (typeof reply === "object") return JSON.stringify(reply, null, 2);
  return String(reply);
}

$("console-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("console-input");
  const raw = input.value.trim();
  if (!raw) return;
  appendConsoleLine(raw, "console__line--cmd");
  input.value = "";

  const argv = raw.match(/"[^"]*"|'[^']*'|\S+/g).map(t => t.replace(/^["']|["']$/g, ""));
  try {
    const res = await api("/api/v1/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ db: state.selectedDb, argv }),
    });
    appendConsoleLine(formatReply(res.reply), "console__line--ok");
    refreshKeyspace();
    refreshHeatstrip();
  } catch (err) {
    appendConsoleLine(err.message, "console__line--err");
  }
});

// ---------------------------------------------------------------- WebSocket

function connectWebSocket() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws`);

  ws.onopen = () => {
    $("live-dot").className = "live-dot is-live";
    $("live-label").textContent = "live";
  };
  ws.onclose = () => {
    $("live-dot").className = "live-dot is-down";
    $("live-label").textContent = "reconnecting…";
    setTimeout(connectWebSocket, 1500);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (event) => {
    const msg = JSON.parse(event.data);
    if (msg.type === "tick") {
      renderStats(msg.stats);
      setupDbSelector(msg.stats.keyspace.length ? Math.max(...msg.stats.keyspace.map(d => d.db)) + 1 : 16);
      if (msg.cdc_events) renderCdcEvents(msg.cdc_events);
    }
  };
}

// ---------------------------------------------------------------- boot

setupDbSelector(16);
connectWebSocket();
refreshHeatstrip();
refreshKeyspace();
setInterval(refreshHeatstrip, 5000);
setInterval(refreshKeyspace, 5000);
