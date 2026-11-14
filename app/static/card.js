"use strict";
// Separador Placa: interfaces de áudio detetadas (aplay -l / arecord -l) e
// mixer ALSA da placa escolhida. Dentro do Docker o /proc/asound não existe,
// por isso a lista vem das ferramentas ALSA (que usam /dev/snd).

const CARD = { list: [], current: null };
try { CARD.current = localStorage.getItem("ct2iep.card"); } catch (_) { /* sem storage */ }

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
    list.append(el("p", "muted", "Nenhuma placa de áudio encontrada. Confirma que o contentor tem acesso a /dev/snd e que a placa está ligada."));
  }
  for (const c of r.cards) {
    const d = el("div", "card-item" + (c.id === r.default ? " main" : ""));
    const h = el("div", "card-title");
    h.append(el("strong", null, c.id), el("span", "muted", ` ${c.name} · placa ${c.index}`));
    if (c.id === r.default) h.append(el("span", "badge installed", "placa da cadeia"));
    d.append(h);
    const fmtDev = (arr, label) => arr.length ? `${label}: ${arr.map(x => `hw:${c.id},${x.device} (${x.name})`).join(", ")}` : `${label}: —`;
    d.append(el("div", "small muted", fmtDev(c.capture, "Entrada")), el("div", "small muted", fmtDev(c.playback, "Saída")));
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
      ? "Esta placa não tem ganhos controláveis por software: o ganho de entrada e o volume ajustam-se nos botões da própria placa. Os controlos abaixo são os únicos que o driver expõe."
      : "Esta placa não expõe nenhum controlo de mixer ALSA (ganhos só nos botões da própria placa).";
  }
  for (const c of r.controls) {
    const box = el("div", "ctl");
    const lab = el("label"); const v = el("span", "v");
    lab.append(el("span", null, c.kind === "enum" ? c.name : `${c.name} · ${c.direction === "capture" ? "entrada" : "saída"}`), v);
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
        cb.onchange = () => post({ on: cb.checked }); lbl.append(cb, document.createTextNode(" ligado"));
        row.append(lbl);
      }
      box.append(row);
    }
    m.append(box);
  }
}

async function showCardTab() {
  try { await loadCards(); await loadMixer(); }
  catch (e) { toast(e.message); }
}
$("#cardSel").onchange = () => {
  CARD.current = $("#cardSel").value;
  try { localStorage.setItem("ct2iep.card", CARD.current); } catch (_) { /* sem storage */ }
  loadMixer().catch(e => toast(e.message));
};
$("#mixerRefresh").onclick = showCardTab;
UI.tabs.card = { onShow: showCardTab };
