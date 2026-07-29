const $ = (id) => document.getElementById(id);
const canvas = $("track-canvas");
const ctx = canvas.getContext("2d");
let points = [];
let latest = {};
let rotation = 0;
let operator = false;
let toastTimer;

function fmt(value, digits = 1) {
  return value == null || Number.isNaN(Number(value)) ? "—" : Number(value).toFixed(digits);
}
function bytes(value) {
  if (value == null) return "—";
  const units = ["B", "KB", "MB", "GB"]; let i = 0, n = value;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i > 1 ? 1 : 0)} ${units[i]}`;
}
function toast(message, error = false) {
  clearTimeout(toastTimer); const node = $("toast");
  node.textContent = message; node.className = error ? "show error" : "show";
  toastTimer = setTimeout(() => node.className = "", 3600);
}
function setOperator(value) {
  operator = value;
  $("controls-panel").classList.toggle("locked", !value);
  $("controls-panel").querySelectorAll("button,select,input").forEach(el => el.disabled = !value);
  $("lock-label").textContent = value ? "Unlocked" : "Locked";
  $("operator-button").textContent = value ? "Operator logout" : "Operator login";
}

function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const ratio = Math.min(devicePixelRatio || 1, 2);
  canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0); draw();
}
function draw() {
  const width = canvas.clientWidth, height = canvas.clientHeight;
  ctx.clearRect(0, 0, width, height);
  $("map-empty").style.display = points.length ? "none" : "flex";
  if (!points.length) return;
  const rotated = points.map(p => {
    const c = Math.cos(rotation), s = Math.sin(rotation);
    return {...p, rx: p.x * c - p.y * s, ry: p.x * s + p.y * c};
  });
  let minX = Math.min(...rotated.map(p => p.rx)), maxX = Math.max(...rotated.map(p => p.rx));
  let minY = Math.min(...rotated.map(p => p.ry)), maxY = Math.max(...rotated.map(p => p.ry));
  const spanX = Math.max(maxX - minX, 20), spanY = Math.max(maxY - minY, 20);
  const pad = 48, scale = Math.min((width - pad * 2) / spanX, (height - pad * 2) / spanY);
  const midX = (minX + maxX) / 2, midY = (minY + maxY) / 2;
  const screen = p => ({x: width / 2 + (p.rx - midX) * scale, y: height / 2 - (p.ry - midY) * scale});

  ctx.lineWidth = 1; ctx.strokeStyle = "rgba(142,154,170,.14)";
  const meterGrid = [10, 20, 50, 100, 200].find(v => v * scale >= 42) || 500;
  const origin = screen({rx: 0, ry: 0});
  for (let x = origin.x % (meterGrid * scale); x < width; x += meterGrid * scale) {
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, height); ctx.stroke();
  }
  for (let y = origin.y % (meterGrid * scale); y < height; y += meterGrid * scale) {
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(width, y); ctx.stroke();
  }
  ctx.font = "10px system-ui"; ctx.fillStyle = "#687583";
  ctx.fillText(`${meterGrid} m grid`, 15, 20);

  let previous = null, activeStyle = null;
  for (const point of rotated) {
    const p = screen(point);
    if (previous && previous.segment === point.segment) {
      const style = point.source === "gnss" ? "#f6bd60" : "#e63946";
      if (activeStyle !== style) {
        if (activeStyle) ctx.stroke();
        ctx.beginPath(); ctx.moveTo(previous.sx, previous.sy);
        ctx.strokeStyle = style; ctx.lineWidth = 2.2; activeStyle = style;
      }
      ctx.lineTo(p.x, p.y);
    } else if (activeStyle) {
      ctx.stroke(); activeStyle = null;
    }
    previous = {...point, sx: p.x, sy: p.y};
  }
  if (activeStyle) ctx.stroke();
  const current = rotated[rotated.length - 1], p = screen(current);
  if (current.uncertainty) {
    ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(4, current.uncertainty * scale), 0, Math.PI * 2);
    ctx.fillStyle = current.source === "gnss" ? "rgba(246,189,96,.13)" : "rgba(255,255,255,.1)";
    ctx.fill();
  }
  ctx.beginPath(); ctx.arc(p.x, p.y, 5.5, 0, Math.PI * 2); ctx.fillStyle = "#fff"; ctx.fill();
  ctx.beginPath(); ctx.arc(p.x, p.y, 10, 0, Math.PI * 2); ctx.strokeStyle = "rgba(255,255,255,.28)"; ctx.lineWidth = 1; ctx.stroke();
}

async function loadTrack() {
  const response = await fetch("/api/track"); const data = await response.json();
  points = data.points || []; draw();
}
function updateTelemetry(data) {
  const oldSession = latest.session_id; latest = data;
  const m = data.measurement || {}, position = data.position;
  if (oldSession && data.session_id !== oldSession) loadTrack();
  if (position) { points.push(position); if (points.length > 36000) points.shift(); draw(); }
  $("connection-pill").className = data.connected ? "pill online" : "pill offline";
  $("connection-pill").innerHTML = `<i></i>${data.connected ? "Sensor online" : "Sensor offline"}`;
  $("recording-pill").className = data.recording ? "pill recording" : "pill muted";
  $("recording-pill").textContent = data.recording ? "● Recording" : data.state || "Stopped";
  $("speed").textContent = fmt(m.gnss_speed);
  $("fix").textContent = m.gnss_fix ?? "—"; $("satellites").textContent = `${m.gnss_sats ?? "—"} sats`;
  $("lap-count").textContent = data.lap_count ?? "—";
  $("lap-time").textContent = data.current_lap_s == null ? "—" : `${fmt(data.current_lap_s, 2)} s`;
  const u = position?.uncertainty ?? m.ins_pos_u; $("uncertainty").textContent = fmt(u, 2);
  $("session").textContent = data.filename || "No active file"; $("rows").textContent = (data.row_count || 0).toLocaleString();
  $("sensor").textContent = data.model || "—";
  const source = position?.source;
  $("source-label").innerHTML = `<i class="legend-source"></i>${source ? source.toUpperCase() + (source === "gnss" ? " · degraded" : "") : "No position"}`;
  $("source-label").querySelector("i").style.background = source === "gnss" ? "#f6bd60" : "#38d996";
}
async function refreshStatus() {
  try {
    const response = await fetch("/api/status"), data = await response.json();
    setOperator(data.operator); if (data.telemetry?.version) updateTelemetry(data.telemetry);
    $("viewer-count").textContent = `${Math.max(1, data.viewers)} viewer${data.viewers === 1 ? "" : "s"}`;
    $("disk").textContent = bytes(data.health.disk_free);
    $("temperature").textContent = data.health.cpu_temp_c == null ? "—" : `${fmt(data.health.cpu_temp_c)} °C`;
  } catch { $("connection-pill").className = "pill offline"; }
}
async function refreshSessions() {
  const list = $("sessions-list");
  try {
    const response = await fetch("/api/sessions"), data = await response.json();
    if (!data.sessions.length) { list.innerHTML = '<p class="empty-copy">No completed sessions found.</p>'; return; }
    list.innerHTML = data.sessions.map(s => `<div class="session-row"><strong>${s.filename}</strong><span>${bytes(s.size)} · ${new Date(s.modified * 1000).toLocaleString()}</span><a href="/api/sessions/${encodeURIComponent(s.filename)}">Download CSV</a></div>`).join("");
  } catch { list.innerHTML = '<p class="empty-copy">Unable to load sessions.</p>'; }
}
async function sendCommand(name, payload = {}) {
  const response = await fetch(`/api/commands/${name}`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
  const text = await response.text(); let data; try { data = JSON.parse(text); } catch { data = {error: text}; }
  if (!response.ok || !data.success) throw new Error(data.error || "Command failed");
  if (data.state) updateTelemetry(data.state); toast(`${name.replaceAll("-", " ")} completed`);
}

document.querySelectorAll("[data-command]").forEach(button => button.addEventListener("click", async () => {
  if (button.dataset.confirm && !confirm(button.dataset.confirm)) return;
  button.disabled = true; try { await sendCommand(button.dataset.command); if (["stop","new-session"].includes(button.dataset.command)) refreshSessions(); }
  catch (error) { toast(error.message, true); } finally { button.disabled = !operator; }
}));
$("add-marker").addEventListener("click", async () => {
  try { await sendCommand("marker", {type: $("marker-type").value, note: $("marker-note").value}); $("marker-note").value = ""; }
  catch (error) { toast(error.message, true); }
});
$("operator-button").addEventListener("click", async () => {
  if (operator) { await fetch("/api/operator/logout", {method: "POST"}); setOperator(false); toast("Operator controls locked"); }
  else { $("pin-input").value = ""; $("login-error").textContent = ""; $("login-dialog").showModal(); setTimeout(() => $("pin-input").focus(), 100); }
});
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const response = await fetch("/api/operator/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({pin: $("pin-input").value})});
  if (!response.ok) { $("login-error").textContent = response.status === 429 ? "Too many attempts. Wait one minute." : "Incorrect PIN."; return; }
  $("login-dialog").close(); setOperator(true); toast("Operator controls unlocked");
});
$("rotate-left").onclick = () => { rotation -= Math.PI / 12; draw(); };
$("rotate-right").onclick = () => { rotation += Math.PI / 12; draw(); };
$("reset-view").onclick = () => { rotation = 0; draw(); };
$("refresh-sessions").onclick = refreshSessions;

window.addEventListener("resize", resizeCanvas);
resizeCanvas(); refreshStatus(); refreshSessions();
setInterval(refreshStatus, 10000); setInterval(refreshSessions, 30000);
const events = new EventSource("/api/stream");
events.onmessage = event => updateTelemetry(JSON.parse(event.data));
events.onerror = () => { $("connection-pill").className = "pill offline"; $("connection-pill").innerHTML = "<i></i>Dashboard reconnecting"; };
