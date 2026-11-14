"use strict";
// ---------- estado ----------
let CHAIN = null;     // {blocks, state, online, io}
let STATE = null;
let selected = null;
let ws = null;
const $ = (s, el = document) => el.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), 2500);
}

// ---------- conversões ----------
const isGain = c => c.unit === "G";
const db = v => 20 * Math.log10(v);
const undb = d => Math.pow(10, d / 20);
const GAIN_FLOOR = 1e-4; // -80 dB

function toPos(c, v) {
  if (isGain(c)) {
    const lo = db(Math.max(c.min, GAIN_FLOOR)), hi = db(c.max);
    return (db(Math.max(v, GAIN_FLOOR)) - lo) / (hi - lo);
  }
  if (c.log && c.min > 0) return Math.log(v / c.min) / Math.log(c.max / c.min);
  return (v - c.min) / (c.max - c.min || 1);
}
function fromPos(c, p) {
  let v;
  if (isGain(c)) {
    const lo = db(Math.max(c.min, GAIN_FLOOR)), hi = db(c.max);
    v = undb(lo + p * (hi - lo));
    if (c.min === 0 && p <= 0) v = 0;
  } else if (c.log && c.min > 0) v = c.min * Math.pow(c.max / c.min, p);
  else v = c.min + p * (c.max - c.min);
  if (c.integer) v = Math.round(v);
  return Math.min(c.max, Math.max(c.min, v));
}
// valor do plugin -> valor mostrado (dB para ganhos)
const toDisp = (c, v) => isGain(c) ? (v <= 0 ? -Infinity : db(v)) : v;
const fromDisp = (c, d) => isGain(c) ? (d === -Infinity ? 0 : undb(d)) : d;

function fmt(c, v) {
  if (isGain(c)) { if (v <= 0) return "−∞ dB"; const d = db(v); return (d > 0 ? "+" : "") + d.toFixed(1) + " dB"; }
  const u = c.unit || "";
  if (u === "Hz") return v >= 1000 ? (v / 1000).toFixed(2) + " kHz" : v.toFixed(0) + " Hz";
  if (c.integer) return String(Math.round(v)) + (u ? " " + u : "");
  const a = Math.abs(v);
  const s = a >= 100 ? v.toFixed(0) : a >= 10 ? v.toFixed(1) : a >= 1 ? v.toFixed(2) : v.toFixed(3);
  return s + (u ? " " + u : "");
}

// ---------- websocket ----------
function connectWS() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = ev => handle(JSON.parse(ev.data));
  ws.onclose = () => { setStatus(false, "sem ligação ao servidor"); setTimeout(connectWS, 2000); };
  ws.onopen = () => loadChain();
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

function handle(m) {
  switch (m.type) {
    case "status": setStatus(m.online, m.online ? "motor ativo" : "motor de áudio parado"); break;
    case "param":
      STATE.blocks[m.block].params[m.symbol] = m.value;
      if (selected === m.block) updateControl(m.symbol, m.value);
      break;
    case "bypass": STATE.blocks[m.block].bypass = m.value; renderStrip(); if (selected === m.block) renderPanel(); break;
    case "chain_bypass": STATE.chain_bypass = m.value; renderStrip(); break;
    case "chain": applyChain(m.chain); break;
    case "meters": updateMeters(m.values); break;
    case "error": toast("Erro: " + m.message); break;
  }
}
function setStatus(on, text) { $("#dot").classList.toggle("on", !!on); $("#statusText").textContent = text; }

// ---------- carregar ----------
// a API devolve os blocos já com params/bypass; STATE é a vista usada pela UI
function applyChain(v) {
  CHAIN = v;
  STATE = { chain_bypass: v.chain_bypass,
            blocks: Object.fromEntries(v.blocks.map(b => [b.id, { bypass: b.bypass, params: b.params }])) };
  if (!selected || !CHAIN.blocks.find(b => b.id === selected)) selected = CHAIN.blocks[0]?.id;
  setStatus(CHAIN.online, CHAIN.online ? "motor ativo" : "motor de áudio parado");
  renderMeters(); renderStrip(); renderPanel();
}
async function loadChain() {
  const r = await fetch("/api/chain"); applyChain(await r.json()); loadPresets();
}
// ---------- strip ----------
function renderStrip() {
  const s = $("#strip"); s.innerHTML = "";
  $("#abBtn").classList.toggle("on", STATE.chain_bypass);
  for (const b of CHAIN.blocks) {
    const bs = STATE.blocks[b.id];
    const d = el("div", "blk" + (b.id === selected ? " sel" : "") + (bs.bypass || STATE.chain_bypass ? " off" : ""));
    d.append(el("div", "n", b.name), el("div", "r", b.replaces || b.plugin));
    const pw = el("button", "pw", "⏻"); pw.title = bs.bypass ? "Ligar" : "Bypass";
    pw.onclick = e => { e.stopPropagation(); bs.bypass = !bs.bypass; send({ type: "bypass", block: b.id, value: bs.bypass }); renderStrip(); if (selected === b.id) renderPanel(); };
    d.append(pw);
    d.onclick = () => { selected = b.id; renderStrip(); renderPanel(); };
    s.append(d);
  }
}

// ---------- painel ----------
const ctlEls = {};
function renderPanel() {
  const p = $("#panel"); p.innerHTML = ""; for (const k in ctlEls) delete ctlEls[k];
  const b = CHAIN.blocks.find(x => x.id === selected); if (!b) return;
  const bs = STATE.blocks[b.id];
  const head = el("div", "head");
  const h = el("div"); h.append(el("h2", null, b.name), el("div", "muted", `${b.plugin} · substitui: ${b.replaces || "—"}${bs.bypass ? " · EM BYPASS" : ""}`));
  const reset = el("button", "ghost", "Repor valores"); reset.title = "Volta aos valores do chain.yaml";
  reset.onclick = async () => { if (confirm(`Repor os valores de "${b.name}"?`)) await fetch(`/api/reset?block=${b.id}`, { method: "POST" }); };
  head.append(h, reset); p.append(head);

  const byS = Object.fromEntries(b.controls.map(c => [c.symbol, c]));
  const main = el("div", "grid");
  for (const s of b.main) main.append(makeControl(b, byS[s], bs.params[s]));
  if (!b.main.length) main.append(el("div", "muted", "Sem parâmetros principais definidos: vê Avançado."));
  p.append(main);

  const rest = b.controls.filter(c => !b.main.includes(c.symbol));
  if (rest.length) {
    const det = el("details", "adv"); det.append(el("summary", null, `Avançado (${rest.length} parâmetros)`));
    const g = el("div", "grid"); det.append(g);
    det.addEventListener("toggle", () => { if (det.open && !g.childElementCount) for (const c of rest) g.append(makeControl(b, c, bs.params[c.symbol])); }, { once: false });
    p.append(det);
  }
}

function makeControl(b, c, value) {
  const box = el("div", "ctl");
  const lab = el("label"); const name = el("span", null, c.name); const val = el("span", "v", fmt(c, value));
  lab.append(name, val); box.append(lab);
  const commit = v => { STATE.blocks[b.id].params[c.symbol] = v; val.textContent = fmt(c, v); sendThrottled(b.id, c.symbol, v); };

  if (c.enum && c.points.length) {
    const sel = el("select");
    for (const pt of c.points) { const o = el("option", null, pt.label); o.value = pt.value; sel.append(o); }
    sel.value = String(value); sel.onchange = () => commit(parseFloat(sel.value));
    box.append(sel);
    ctlEls[c.symbol] = { c, set: v => { sel.value = String(v); val.textContent = fmt(c, v); } };
    val.textContent = "";
  } else if (c.toggle) {
    const cb = el("input"); cb.type = "checkbox"; cb.checked = value >= 0.5; cb.className = "sw";
    cb.onchange = () => commit(cb.checked ? 1 : 0); val.textContent = value >= 0.5 ? "ligado" : "desligado";
    const comm2 = commit; cb.onchange = () => { comm2(cb.checked ? c.max : c.min); val.textContent = cb.checked ? "ligado" : "desligado"; };
    box.append(cb);
    ctlEls[c.symbol] = { c, set: v => { cb.checked = v >= 0.5; val.textContent = v >= 0.5 ? "ligado" : "desligado"; } };
  } else {
    const row = el("div", "row");
    const r = el("input"); r.type = "range"; r.min = 0; r.max = 1000; r.value = Math.round(toPos(c, value) * 1000);
    const n = el("input"); n.type = "number"; n.step = "any";
    const setN = v => { const d = toDisp(c, v); n.value = isFinite(d) ? +d.toFixed(isGain(c) ? 1 : 3) : ""; };
    setN(value);
    r.oninput = () => { const v = fromPos(c, r.value / 1000); setN(v); commit(v); };
    r.ondblclick = () => { const v = b.defaults[c.symbol]; r.value = Math.round(toPos(c, v) * 1000); setN(v); commit(v); };
    n.onchange = () => { let v = fromDisp(c, parseFloat(n.value)); if (!isFinite(v)) return; v = Math.min(c.max, Math.max(c.min, v)); r.value = Math.round(toPos(c, v) * 1000); setN(v); commit(v); };
    r.title = "Duplo clique repõe o valor por defeito";
    row.append(r, n); box.append(row);
    ctlEls[c.symbol] = { c, set: v => { r.value = Math.round(toPos(c, v) * 1000); setN(v); val.textContent = fmt(c, v); } };
  }
  return box;
}
function updateControl(sym, v) { ctlEls[sym]?.set(v); }

const pending = {}; let flushT = null;
function sendThrottled(block, symbol, value) {
  pending[block + "|" + symbol] = { type: "param", block, symbol, value };
  if (!flushT) flushT = setTimeout(() => { for (const k in pending) { send(pending[k]); delete pending[k]; } flushT = null; }, 40);
}

// ---------- meters ----------
const meterEls = {};
function renderMeters() {
  const m = $("#meters"); m.innerHTML = "";
  for (const b of CHAIN.blocks) for (const mt of b.meters) {
    const key = `${b.id}.${mt.symbol}`;
    const d = el("div", "meter" + (mt.kind === "gr" ? " gr" : ""));
    const top = el("div", "top"); const v = el("span", "val", "—");
    top.append(el("span", null, mt.label), v);
    const bar = el("div", "bar"); const f = el("div", "fill"); bar.append(f); d.append(top, bar); m.append(d);
    meterEls[key] = { mt, v, f, d };
  }
}
function updateMeters(values) {
  for (const k in values) {
    const e = meterEls[k]; if (!e) continue;
    const x = values[k];
    if (e.mt.kind === "gr") {
      const red = Math.min(0, x);                       // redução (≤ 0 dB)
      e.f.style.width = Math.min(100, -red / 24 * 100) + "%";
      e.v.textContent = red > -0.05 ? "0.0 dB" : red.toFixed(1) + " dB";
    } else {
      const pct = Math.max(0, Math.min(100, (x + 60) / 60 * 100));
      e.f.style.width = pct + "%"; e.f.style.setProperty("--w", (100 / Math.max(pct, 1) * 100) + "%");
      e.v.textContent = x <= -119 ? "−∞" : x.toFixed(1) + " dB";
    }
  }
}

// ---------- presets ----------
async function loadPresets() {
  const r = await (await fetch("/api/presets")).json();
  const sel = $("#presetSel"); const cur = sel.value; sel.innerHTML = '<option value="">— preset —</option>';
  for (const n of r.presets) { const o = el("option", null, n); o.value = n; sel.append(o); }
  if (r.presets.includes(cur)) sel.value = cur;
}
$("#presetLoad").onclick = async () => {
  const n = $("#presetSel").value; if (!n) return;
  const r = await fetch(`/api/presets/${encodeURIComponent(n)}/load`, { method: "POST" });
  toast(r.ok ? `Preset "${n}" carregado` : "Falhou a carregar");
};
$("#presetSave").onclick = async () => {
  const n = prompt("Nome do preset (ex.: SSB DX, SSB ragchew):", $("#presetSel").value || ""); if (!n) return;
  const r = await fetch("/api/presets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: n }) });
  if (r.ok) { await loadPresets(); $("#presetSel").value = n; toast(`Guardado "${n}"`); } else toast((await r.json()).error);
};
$("#presetDel").onclick = async () => {
  const n = $("#presetSel").value; if (!n || !confirm(`Apagar o preset "${n}"?`)) return;
  await fetch(`/api/presets/${encodeURIComponent(n)}`, { method: "DELETE" }); loadPresets();
};
$("#abBtn").onclick = () => { STATE.chain_bypass = !STATE.chain_bypass; send({ type: "chain_bypass", value: STATE.chain_bypass }); renderStrip(); };

// ---------- mixer ----------
async function loadMixer() {
  const r = await (await fetch("/api/mixer")).json();
  $("#mixerCard").textContent = r.card ? `placa: ${r.card}` : "(define MIXER_CARD ou AUDIO_CARD)";
  const m = $("#mixer"); m.innerHTML = "";
  if (r.error) m.append(el("div", "muted", r.error));
  if (!r.controls.length && !r.error) m.append(el("div", "muted", "Sem controlos."));
  for (const c of r.controls) {
    const box = el("div", "ctl");
    const lab = el("label"); const v = el("span", "v");
    lab.append(el("span", null, c.kind === "enum" ? c.name : `${c.name} · ${c.direction === "capture" ? "captura" : "saída"}`), v);
    box.append(lab);
    const post = body => fetch("/api/mixer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: c.id, kind: c.kind, direction: c.direction, ...body }) })
      .then(async res => { if (!res.ok) toast((await res.json()).error); });
    if (c.kind === "enum") {
      const sel = el("select"); for (const it of c.items) { const o = el("option", null, it); o.value = it; sel.append(o); }
      sel.value = c.value; sel.onchange = () => post({ value: sel.value }); box.append(sel);
    } else {
      const row = el("div", "row");
      if (c.has_volume) {
        const r2 = el("input"); r2.type = "range"; r2.min = 0; r2.max = 100; r2.value = c.percent ?? 0;
        v.textContent = `${c.percent ?? "?"}%${c.db ? " · " + c.db : ""}`;
        let t = null; r2.oninput = () => { v.textContent = r2.value + "%"; clearTimeout(t); t = setTimeout(() => post({ percent: +r2.value }), 60); };
        row.append(r2);
      }
      if (c.has_switch) {
        const cb = el("input"); cb.type = "checkbox"; cb.checked = !!c.on; cb.title = "ligado/mute";
        cb.onchange = () => post({ on: cb.checked }); row.append(cb);
      }
      box.append(row);
    }
    m.append(box);
  }
  $("#cards").textContent = (await (await fetch("/api/cards")).json()).raw;
}
$("#mixerRefresh").onclick = loadMixer;

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
  $("#tab-chain").hidden = t.dataset.tab !== "chain";
  $("#tab-mixer").hidden = t.dataset.tab !== "mixer";
  if (t.dataset.tab === "mixer") loadMixer();
});

connectWS();
