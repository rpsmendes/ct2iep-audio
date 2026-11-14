"use strict";
// Núcleo da UI: estado, WebSocket, cadeia (blocos, painel, controlos), medidores,
// presets e separadores. Funcionalidades extra registam-se em UI.* (ver fim).

const UI = {
  chain: null,            // vista devolvida por /api/chain
  selected: null,         // id do bloco selecionado
  fine: false,            // ajuste fino ligado (útil em ecrãs táteis)
  hooks: { panel: [], param: [], chain: [], stripTail: [], panelActions: [] },
  tabs: {},               // nome -> { onShow }
};
let ws = null;
const $ = (s, el = document) => el.querySelector(s);
const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const blockById = id => UI.chain?.blocks.find(b => b.id === id);

function toast(msg, ms = 2500) {
  const t = $("#toast"); t.textContent = msg; t.hidden = false;
  clearTimeout(t._h); t._h = setTimeout(() => (t.hidden = true), ms);
}
async function api(method, url, body) {
  const r = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  let j = {}; try { j = await r.json(); } catch (_) { /* sem corpo */ }
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// ---------- escalas dos controlos ----------
// O slider trabalha numa posição 0..1; a escala converte posição <-> valor.
const isGain = c => c.unit === "G";
const db = v => 20 * Math.log10(v);
const undb = d => Math.pow(10, d / 20);
const GAIN_FLOOR_DB = -72;
const TIME_UNITS = new Set(["ms", "s", "min"]);

function scaleOf(c) {
  if (isGain(c)) return "db";
  const logish = c.log || c.unit === "Hz" || TIME_UNITS.has(c.unit);
  if (logish && c.min > 0 && c.max > c.min) return "log";
  if (logish && c.min === 0 && c.max > 0) return "plog";     // tempos que começam em 0
  return "lin";
}
function toPos(c, v) {
  switch (scaleOf(c)) {
    case "db": {
      const lo = Math.max(c.min > 0 ? db(c.min) : GAIN_FLOOR_DB, GAIN_FLOOR_DB), hi = db(c.max);
      return v <= 0 ? 0 : (Math.max(db(v), lo) - lo) / (hi - lo);
    }
    case "log": return Math.log(Math.max(v, c.min) / c.min) / Math.log(c.max / c.min);
    case "plog": { const k = c.max / 1000; return Math.log1p(Math.max(v, 0) / k) / Math.log1p(c.max / k); }
    default: return (v - c.min) / (c.max - c.min || 1);
  }
}
function fromPos(c, p) {
  p = Math.min(1, Math.max(0, p));
  let v;
  switch (scaleOf(c)) {
    case "db": {
      const lo = Math.max(c.min > 0 ? db(c.min) : GAIN_FLOOR_DB, GAIN_FLOOR_DB), hi = db(c.max);
      v = (p <= 0 && c.min === 0) ? 0 : undb(lo + p * (hi - lo)); break;
    }
    case "log": v = c.min * Math.pow(c.max / c.min, p); break;
    case "plog": { const k = c.max / 1000; v = k * Math.expm1(p * Math.log1p(c.max / k)); break; }
    default: v = c.min + p * (c.max - c.min);
  }
  if (c.integer) v = Math.round(v);
  return Math.min(c.max, Math.max(c.min, v));
}
// valor do plugin <-> valor mostrado (dB para ganhos)
const toDisp = (c, v) => isGain(c) ? (v <= 0 ? -Infinity : db(v)) : v;
const fromDisp = (c, d) => isGain(c) ? (d === -Infinity ? 0 : undb(d)) : d;

// passo de teclado/roda: ganhos em dB, o resto na posição do slider
function nudge(c, v, dir, fine, big) {
  const mult = big ? 10 : 1;
  if (c.enum || c.integer || c.toggle) return Math.min(c.max, Math.max(c.min, Math.round(v) + dir * mult));
  if (isGain(c)) {
    const d = (v <= 0 ? GAIN_FLOOR_DB : db(v)) + dir * (fine ? 0.1 : 0.5) * mult;
    return Math.min(c.max, Math.max(c.min, undb(d)));
  }
  return fromPos(c, toPos(c, v) + dir * (fine ? 0.002 : 0.01) * mult);
}

function fmtNum(v, unit) {
  const a = Math.abs(v);
  const dec = a >= 1000 ? 0 : a >= 100 ? (unit === "Hz" ? 0 : 1) : a >= 10 ? 1 : a >= 1 ? 2 : 3;
  return v.toFixed(dec);
}
function fmt(c, v) {
  if (isGain(c)) { if (v <= 0) return "−∞ dB"; const d = db(v); return (d > 0.05 ? "+" : "") + d.toFixed(1) + " dB"; }
  const u = c.unit || "";
  if (u === "Hz") return v >= 1000 ? (v / 1000).toFixed(v >= 10000 ? 1 : 2) + " kHz" : fmtNum(v, u) + " Hz";
  if (c.integer) return String(Math.round(v)) + (u ? " " + u : "");
  return fmtNum(v, u) + (u ? " " + u : "");
}

// ---------- slider com arrasto relativo ----------
// Clicar não muda o valor (seguro em TX); arrastar ajusta ~2,5x mais devagar
// que o rato; Shift/Alt ou "Ajuste fino" = 10x mais fino; duplo clique = defeito.
function makeSlider(c, value, onChange, defaultValue) {
  const root = el("div", "sl"); root.tabIndex = 0; root.setAttribute("role", "slider");
  root.setAttribute("aria-label", c.name);
  const track = el("div", "tr"), fill = el("div", "fi"), thumb = el("div", "th");
  track.append(fill, thumb); root.append(track);
  let cur = value, pos = toPos(c, value), drag = null;
  const paint = () => { const pc = (pos * 100).toFixed(2) + "%"; fill.style.width = pc; thumb.style.left = pc; root.setAttribute("aria-valuetext", fmt(c, cur)); };
  const setValue = (v, emit) => { cur = v; pos = toPos(c, v); paint(); if (emit) onChange(v); };
  paint();

  root.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    root.focus(); root.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, pos, fine: null, moved: false };
    root.classList.add("active"); UI.busy = (UI.busy || 0) + 1;
  });
  root.addEventListener("pointermove", e => {
    if (!drag) return;
    const fine = UI.fine || e.shiftKey || e.altKey;
    if (drag.fine !== null && fine !== drag.fine) { drag.x = e.clientX; drag.pos = pos; }  // muda o modo a meio
    drag.fine = fine;
    const dx = e.clientX - drag.x;
    if (!drag.moved && Math.abs(dx) < 3) return;
    drag.moved = true;
    const span = Math.max(track.clientWidth * 2.5, 320) * (fine ? 10 : 1);
    pos = Math.min(1, Math.max(0, drag.pos + dx / span));
    cur = fromPos(c, pos); paint(); onChange(cur);
  });
  const end = e => {
    if (!drag) return;
    drag = null; root.classList.remove("active");
    try { root.releasePointerCapture(e.pointerId); } catch (_) { /* já libertado */ }
    UI.busy = Math.max(0, (UI.busy || 1) - 1); if (!UI.busy) flushDeferred();
  };
  root.addEventListener("pointerup", end);
  root.addEventListener("pointercancel", end);
  root.addEventListener("dblclick", () => { if (defaultValue != null) setValue(defaultValue, true); });
  root.addEventListener("keydown", e => {
    const map = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, PageUp: 1, PageDown: -1 };
    if (e.key === "Home") { setValue(c.min, true); e.preventDefault(); return; }
    if (e.key === "End") { setValue(c.max, true); e.preventDefault(); return; }
    if (!(e.key in map)) return;
    e.preventDefault();
    setValue(nudge(c, cur, map[e.key], UI.fine || e.shiftKey, e.key.startsWith("Page")), true);
  });
  // a roda só atua com o slider focado (não muda valores por acidente ao fazer scroll)
  root.addEventListener("wheel", e => {
    if (document.activeElement !== root) return;
    e.preventDefault();
    setValue(nudge(c, cur, e.deltaY < 0 ? 1 : -1, UI.fine || e.shiftKey), true);
  }, { passive: false });
  return { el: root, set: v => { if (!drag) setValue(v, false); } };
}

// ---------- websocket ----------
function connectWS() {
  ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = ev => handle(JSON.parse(ev.data));
  ws.onclose = () => { setStatus(false, "sem ligação ao servidor"); setTimeout(connectWS, 2000); };
  ws.onopen = () => loadChain();
}
function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

let deferredChain = null;
function flushDeferred() { if (deferredChain) { const c = deferredChain; deferredChain = null; applyChain(c); } }

function handle(m) {
  switch (m.type) {
    case "status": setStatus(m.online, m.online ? "motor ativo" : "motor de áudio parado"); break;
    case "chain":
      if (UI.busy) deferredChain = m.chain;           // não estragar um arrasto em curso
      else applyChain(m.chain);
      break;
    case "param": {
      const b = blockById(m.block); if (!b) break;
      b.params[m.symbol] = m.value;
      if (UI.selected === m.block) updateControl(m.symbol, m.value);
      UI.hooks.param.forEach(f => f(b, m.symbol, m.value));
      break;
    }
    case "bypass": { const b = blockById(m.block); if (b) b.bypass = m.value; renderStrip(); if (UI.selected === m.block) renderPanel(); UI.hooks.param.forEach(f => f(b)); break; }
    case "chain_bypass": UI.chain.chain_bypass = m.value; renderStrip(); UI.hooks.param.forEach(f => f(null)); break;
    case "meters": updateMeters(m.values); break;
    case "error": toast("Erro: " + m.message); break;
  }
}
function setStatus(on, text) { $("#dot").classList.toggle("on", !!on); $("#statusText").textContent = text; }

// ---------- cadeia ----------
function applyChain(v) {
  UI.chain = v;
  if (!UI.selected || !blockById(UI.selected)) UI.selected = v.blocks[0]?.id || null;
  setStatus(v.online, v.online ? "motor ativo" : "motor de áudio parado");
  renderMeters(); renderStrip(); renderPanel();
  UI.hooks.chain.forEach(f => f(v));
}
async function loadChain() {
  applyChain(await api("GET", "/api/chain"));
  loadPresets();
}
function select(id) { UI.selected = id; renderStrip(); renderPanel(); }

function renderStrip() {
  const s = $("#strip"); s.innerHTML = "";
  $("#abBtn").classList.toggle("on", !!UI.chain.chain_bypass);
  for (const b of UI.chain.blocks) {
    const off = b.bypass || UI.chain.chain_bypass;
    const d = el("div", "blk" + (b.id === UI.selected ? " sel" : "") + (off ? " off" : "") + (b.error || b.missing ? " bad" : ""));
    d.dataset.id = b.id;
    d.append(el("div", "n", b.name), el("div", "r", b.missing ? "plugin não instalado" : b.error ? "erro ao carregar" : (b.replaces || b.plugin)));
    const pw = el("button", "pw", "⏻"); pw.title = b.bypass ? "Ligar" : "Bypass"; pw.setAttribute("aria-label", pw.title);
    pw.onclick = e => { e.stopPropagation(); setBypass(b, !b.bypass); };
    d.append(pw);
    d.onclick = () => select(b.id);
    s.append(d);
  }
  UI.hooks.stripTail.forEach(f => f(s));
}
function setBypass(b, val) {
  b.bypass = val; send({ type: "bypass", block: b.id, value: val });
  renderStrip(); if (UI.selected === b.id) renderPanel(); UI.hooks.param.forEach(f => f(b));
}

// ---------- painel do bloco ----------
const ctlEls = {};
function renderPanel() {
  const p = $("#panel"); p.innerHTML = ""; for (const k in ctlEls) delete ctlEls[k];
  const b = blockById(UI.selected);
  if (!b) { p.append(el("div", "muted", "Cadeia vazia.")); return; }
  const head = el("div", "head");
  const h = el("div");
  const sub = [b.plugin, b.replaces ? "substitui: " + b.replaces : "", b.bypass ? "EM BYPASS" : ""].filter(Boolean).join(" · ");
  h.append(el("h2", null, b.name), el("div", "muted", sub));
  const actions = el("div", "actions");
  const fineLbl = el("label", "fine"); const fineCb = el("input"); fineCb.type = "checkbox"; fineCb.checked = UI.fine;
  fineCb.onchange = () => { UI.fine = fineCb.checked; };
  fineLbl.append(fineCb, document.createTextNode(" Ajuste fino")); fineLbl.title = "Arrasto e teclado 10x mais finos (o mesmo que manter Shift)";
  actions.append(fineLbl);
  UI.hooks.panelActions.forEach(f => f(b, actions));
  const reset = el("button", "ghost", "Repor valores"); reset.title = "Volta aos valores do chain.yaml (ou aos do plugin)";
  reset.onclick = async () => { if (confirm(`Repor os valores de "${b.name}"?`)) await api("POST", `/api/reset?block=${encodeURIComponent(b.id)}`); };
  actions.append(reset);
  head.append(h, actions); p.append(head);

  if (b.missing || b.error) {
    p.append(el("div", "note warn", b.missing ? "O plugin deste bloco não está instalado. Instala o pacote no separador Plugins ou remove o bloco." : `Este bloco não foi carregado: ${b.error}.`));
    if (b.missing) return;
  }
  UI.hooks.panel.forEach(f => f(b, p));

  const byS = Object.fromEntries(b.controls.map(c => [c.symbol, c]));
  const main = el("div", "grid");
  for (const s of b.main) if (byS[s]) main.append(makeControl(b, byS[s]));
  if (!b.main.length) main.append(el("div", "muted", "Sem parâmetros principais definidos: vê Avançado."));
  p.append(main);

  const rest = b.controls.filter(c => !b.main.includes(c.symbol));
  if (rest.length) {
    const det = el("details", "adv"); det.append(el("summary", null, `Avançado (${rest.length} parâmetros)`));
    const g = el("div", "grid"); det.append(g);
    det.addEventListener("toggle", () => { if (det.open && !g.childElementCount) for (const c of rest) g.append(makeControl(b, c)); });
    p.append(det);
  }
  p.append(el("p", "hint", "Arrastar ajusta (clicar não muda o valor) · Shift ou “Ajuste fino” = 10× mais fino · setas do teclado com o slider selecionado · duplo clique = valor por defeito"));
}

function makeControl(b, c) {
  const value = b.params[c.symbol];
  const box = el("div", "ctl");
  const lab = el("label"); const val = el("span", "v", fmt(c, value));
  lab.append(el("span", null, c.name), val); box.append(lab);
  const commit = v => { b.params[c.symbol] = v; val.textContent = fmt(c, v); sendThrottled(b.id, c.symbol, v); UI.hooks.param.forEach(f => f(b, c.symbol, v)); };

  if (c.enum && c.points.length) {
    const sel = el("select"); sel.setAttribute("aria-label", c.name);
    for (const pt of c.points) { const o = el("option", null, pt.label); o.value = pt.value; sel.append(o); }
    sel.value = String(value); sel.onchange = () => commit(parseFloat(sel.value));
    box.append(sel); val.textContent = "";
    ctlEls[c.symbol] = { set: v => { sel.value = String(v); } };
  } else if (c.toggle) {
    const cb = el("input"); cb.type = "checkbox"; cb.checked = value >= 0.5; cb.className = "sw"; cb.setAttribute("aria-label", c.name);
    const txt = v => (v >= 0.5 ? "ligado" : "desligado");
    val.textContent = txt(value);
    cb.onchange = () => { commit(cb.checked ? c.max : c.min); val.textContent = txt(cb.checked ? 1 : 0); };
    box.append(cb);
    ctlEls[c.symbol] = { set: v => { cb.checked = v >= 0.5; val.textContent = txt(v); } };
  } else {
    const row = el("div", "row");
    const n = el("input"); n.type = "number"; n.step = "any"; n.setAttribute("aria-label", c.name + " (valor)");
    const setN = v => { const d = toDisp(c, v); n.value = isFinite(d) ? +d.toFixed(isGain(c) ? 1 : 3) : ""; };
    const sl = makeSlider(c, value, v => { setN(v); commit(v); }, b.defaults[c.symbol]);
    setN(value);
    n.onchange = () => {
      let v = fromDisp(c, parseFloat(n.value)); if (!isFinite(v)) return;
      v = Math.min(c.max, Math.max(c.min, v)); sl.set(v); setN(v); commit(v);
    };
    n.onkeydown = e => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const v = nudge(c, b.params[c.symbol], e.key === "ArrowUp" ? 1 : -1, UI.fine || e.shiftKey);
      sl.set(v); setN(v); commit(v);
    };
    row.append(sl.el, n); box.append(row);
    ctlEls[c.symbol] = { set: v => { sl.set(v); setN(v); val.textContent = fmt(c, v); } };
  }
  return box;
}
function updateControl(sym, v) { ctlEls[sym]?.set(v); }

const pending = {}; let flushT = null;
function sendThrottled(block, symbol, value) {
  pending[block + "|" + symbol] = { type: "param", block, symbol, value };
  if (!flushT) flushT = setTimeout(() => { for (const k in pending) { send(pending[k]); delete pending[k]; } flushT = null; }, 40);
}

// ---------- medidores ----------
const meterEls = {};
function renderMeters() {
  const m = $("#meters"); m.innerHTML = ""; for (const k in meterEls) delete meterEls[k];
  for (const b of UI.chain.blocks) for (const mt of b.meters) {
    const key = `${b.id}.${mt.symbol}`;
    const d = el("div", "meter" + (mt.kind === "gr" ? " gr" : ""));
    const top = el("div", "top"); const v = el("span", "val", "—");
    top.append(el("span", null, mt.label), v);
    const bar = el("div", "bar"); const f = el("div", "fill"); bar.append(f); d.append(top, bar); m.append(d);
    meterEls[key] = { mt, v, f };
  }
}
function updateMeters(values) {
  for (const k in values) {
    const e = meterEls[k]; if (!e) continue;
    const x = values[k];
    if (e.mt.kind === "gr") {
      const red = Math.min(0, x);
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
  const r = await api("GET", "/api/presets");
  const sel = $("#presetSel"); const cur = sel.value; sel.innerHTML = '<option value="">— preset —</option>';
  for (const n of r.presets) { const o = el("option", null, n); o.value = n; sel.append(o); }
  if (r.presets.includes(cur)) sel.value = cur;
}
$("#presetLoad").onclick = async () => {
  const n = $("#presetSel").value; if (!n) return;
  try { await api("POST", `/api/presets/${encodeURIComponent(n)}/load`); toast(`Preset "${n}" carregado`); }
  catch (e) { toast(e.message); }
};
$("#presetSave").onclick = async () => {
  const n = prompt("Nome do preset (ex.: SSB DX, SSB ragchew):", $("#presetSel").value || ""); if (!n) return;
  try { await api("POST", "/api/presets", { name: n }); await loadPresets(); $("#presetSel").value = n; toast(`Guardado "${n}" (ordem, blocos e valores)`); }
  catch (e) { toast(e.message); }
};
$("#presetDel").onclick = async () => {
  const n = $("#presetSel").value; if (!n || !confirm(`Apagar o preset "${n}"?`)) return;
  await api("DELETE", `/api/presets/${encodeURIComponent(n)}`); loadPresets();
};
$("#abBtn").onclick = () => {
  UI.chain.chain_bypass = !UI.chain.chain_bypass; send({ type: "chain_bypass", value: UI.chain.chain_bypass });
  renderStrip(); UI.hooks.param.forEach(f => f(null));
};
$("#factoryReset")?.addEventListener("click", async () => {
  if (!confirm("Repor a cadeia de fábrica (config/chain.yaml)? A ordem, os blocos e os valores atuais perdem-se (guarda um preset antes, se quiseres).")) return;
  try { await api("POST", "/api/reset"); toast("Cadeia de fábrica reposta"); } catch (e) { toast(e.message); }
});

// ---------- mixer (separador Placa) ----------
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
if ($("#mixerRefresh")) { $("#mixerRefresh").onclick = loadMixer; UI.tabs.mixer = { onShow: loadMixer }; }

// ---------- separadores ----------
document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
  document.querySelectorAll("main > section").forEach(s => { s.hidden = s.id !== "tab-" + t.dataset.tab; });
  UI.tabs[t.dataset.tab]?.onShow?.();
});

// os módulos extra (graph.js, library.js, …) carregam depois deste e registam-se em UI.hooks
window.addEventListener("load", connectWS);
