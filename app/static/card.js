"use strict";
// Card tab: detected audio interfaces (aplay -l / arecord -l) and the ALSA
// mixer of the chosen card. /proc/asound does not exist inside Docker, so the
// list comes from the ALSA tools (which use /dev/snd).

const CARD = { list: [], current: null };
try { CARD.current = localStorage.getItem("ct2iep.card"); } catch (_) { /* no storage */ }

async function loadCards() {
  const r = await api("GET", "/api/cards");
  CARD.list = r.cards;
  const sel = $("#cardSel"); sel.innerHTML = "";
  for (const c of r.cards) {
    const o = el("option", null, `${c.id} — ${c.name}`); o.value = c.id; sel.append(o);
  }
  const ids = r.cards.map(c => c.id);
  if (!ids.includes(CARD.current)) CARD.current = ids.includes(r.default) ? r.default : (ids[0] || null);
  sel.value = CARD.current || "";
  sel.disabled = !r.cards.length;
  $("#cardsRaw").textContent = r.raw;

  const list = $("#cardList"); list.innerHTML = "";
  if (!r.cards.length) {
    list.append(el("p", "muted", "No sound card found. Check that the container has access to /dev/snd and that the card is connected."));
  }
  for (const c of r.cards) {
    const d = el("div", "card-item" + (c.id === r.default ? " main" : ""));
    const h = el("div", "card-title");
    h.append(el("strong", null, c.id), el("span", "muted", ` ${c.name} · card ${c.index}`));
    if (c.id === r.default) h.append(el("span", "badge installed", "chain card"));
    d.append(h);
    const fmtDev = (arr, label) => arr.length ? `${label}: ${arr.map(x => `hw:${c.id},${x.device} (${x.name})`).join(", ")}` : `${label}: —`;
    d.append(el("div", "small muted", fmtDev(c.capture, "Input")), el("div", "small muted", fmtDev(c.playback, "Output")));
    list.append(d);
  }
}

async function loadMixer() {
  const m = $("#mixer"); m.innerHTML = "";
  const note = $("#mixerNote"); note.hidden = true;
  if (!CARD.current) return;
  const r = await api("GET", `/api/mixer?card=${encodeURIComponent(CARD.current)}`);
  if (r.error) { m.append(el("div", "muted", r.error)); return; }
  const gains = r.controls.filter(c => c.kind === "volume" && c.has_volume);
  if (!gains.length) {
    note.hidden = false;
    note.textContent = r.controls.length
      ? "This card has no software-controllable gains: input gain and volume are set with the card's own knobs. The controls below are the only ones the driver exposes."
      : "This card exposes no ALSA mixer controls (gains only on the card's own knobs).";
  }
  for (const c of r.controls) {
    const box = el("div", "ctl");
    const lab = el("label"); const v = el("span", "v");
    lab.append(el("span", null, c.kind === "enum" ? c.name : `${c.name} · ${c.direction === "capture" ? "input" : "output"}`), v);
    box.append(lab);
    const post = body => api("POST", "/api/mixer", { card: CARD.current, id: c.id, kind: c.kind, direction: c.direction, ...body })
      .catch(e => toast(e.message));
    if (c.kind === "enum") {
      const sel = el("select"); sel.setAttribute("aria-label", c.name);
      for (const it of c.items) { const o = el("option", null, it); o.value = it; sel.append(o); }
      sel.value = c.value; sel.onchange = () => post({ value: sel.value }); box.append(sel);
    } else {
      const row = el("div", "row");
      if (c.has_volume) {
        const r2 = el("input"); r2.type = "range"; r2.min = 0; r2.max = 100; r2.value = c.percent ?? 0;
        r2.setAttribute("aria-label", c.name);
        v.textContent = `${c.percent ?? "?"}%${c.db ? " · " + c.db : ""}`;
        let t = null; r2.oninput = () => { v.textContent = r2.value + "%"; clearTimeout(t); t = setTimeout(() => post({ percent: +r2.value }), 60); };
        row.append(r2);
      }
      if (c.has_switch) {
        const lbl = el("label", "sw-lbl"); const cb = el("input"); cb.type = "checkbox"; cb.checked = !!c.on;
        cb.onchange = () => post({ on: cb.checked }); lbl.append(cb, document.createTextNode(" on"));
        row.append(lbl);
      }
      box.append(row);
    }
    m.append(box);
  }
}

// ---------- output selection ----------
const keyOf = (card, dev) => `${card},${dev}`;
async function loadOutputs(data) {
  const r = data || await api("GET", "/api/outputs");
  $("#mainCard").textContent = r.main ? `hw:${r.main},0` : "AUDIO_CARD";
  const sel = new Map(r.selected.map(o => [keyOf(o.card, o.device), o]));
  const rows = [];
  for (const c of CARD.list) for (const d of c.playback) rows.push({ card: c.id, device: d.device, label: `${c.id} · ${d.name}`, sub: `${c.name} · hw:${c.id},${d.device}` });
  // virtual outputs (Zeus over TCI): not sound cards, always listed
  for (const v of r.virtual || []) rows.push({ ...v, virtual: true });
  // selected devices that are not connected right now
  for (const o of r.selected) if (!rows.some(x => keyOf(x.card, x.device) === keyOf(o.card, o.device)))
    rows.push({ card: o.card, device: o.device, label: `${o.card} · device ${o.device}`, sub: "not detected (unplugged?)", gone: true });
  const box = $("#outputs"); box.innerHTML = "";
  for (const x of rows) {
    const k = keyOf(x.card, x.device), o = sel.get(k);
    const main = x.card === r.main && x.device === 0;
    const row = el("label", "out-row" + (o ? " on" : "") + (x.gone ? " gone" : ""));
    const cb = el("input"); cb.type = "checkbox"; cb.checked = !!o; cb.dataset.card = x.card; cb.dataset.device = x.device;
    cb.onchange = saveOutputs;
    const info = el("div", "out-info");
    info.append(el("div", "out-name", x.label), el("div", "muted small", x.sub));
    const badges = el("div", "badges");
    if (main) badges.append(el("span", "badge installed", "main card · direct"));
    else if (o) {
      const st = (x.virtual
        ? { running: ["installed", "connected"], failed: ["available", "not connected"], starting: ["base", "connecting…"] }
        : { running: ["installed", "bridge running"], failed: ["available", "bridge failed"], starting: ["base", "bridge starting…"] })[o.state] || ["base", o.state];
      const b = el("span", "badge " + st[0], st[1]); if (o.detail) b.title = o.detail; badges.append(b);
    } else badges.append(el("span", "badge base", x.virtual ? "via TCI" : "via bridge"));
    row.append(cb, info, badges);
    if (o && o.state === "failed" && o.detail) info.append(el("div", "small warn-text", o.detail));
    box.append(row);
  }
  if (!rows.length) box.append(el("p", "muted", "No playback device found."));
  // bridges take a moment to start: refresh the status a few times
  clearTimeout(CARD.outT);
  if (r.selected.some(o => !o.main && o.state !== "running") && !$("#tab-card").hidden && (CARD.outTries = (CARD.outTries || 0) + 1) < 8)
    CARD.outT = setTimeout(() => loadOutputs().catch(() => {}), 2500);
}
async function saveOutputs() {
  const selected = [...document.querySelectorAll("#outputs input:checked")].map(c => ({ card: c.dataset.card, device: +c.dataset.device }));
  if (!selected.length) { toast("Select at least one output"); loadOutputs(); return; }
  try { CARD.outTries = 0; await loadOutputs(await api("POST", "/api/outputs", { selected })); toast("Output changed"); }
  catch (e) { toast(e.message); loadOutputs(); }
}

async function showCardTab() {
  try { await loadCards(); CARD.outTries = 0; await loadOutputs(); await loadMixer(); }
  catch (e) { toast(e.message); }
}
$("#cardSel").onchange = () => {
  CARD.current = $("#cardSel").value;
  try { localStorage.setItem("ct2iep.card", CARD.current); } catch (_) { /* no storage */ }
  loadMixer().catch(e => toast(e.message));
};
$("#mixerRefresh").onclick = showCardTab;
UI.tabs.card = { onShow: showCardTab };
