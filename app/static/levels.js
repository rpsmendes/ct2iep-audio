"use strict";
// Input / output trims: fixed gain stages before and after the chain.
// They stay active with A/B (they set the levels, not the sound) and are not
// part of presets. Meters show the peak level before and after each trim.

const LV = { cur: null, els: {}, rendered: false, timer: null, pending: {} };

function sendLevels(change) {
  Object.assign(LV.pending, change);
  if (LV.timer) return;
  LV.timer = setTimeout(() => { send({ type: "levels", ...LV.pending }); LV.pending = {}; LV.timer = null; }, 50);
}

const fmtDb = d => (d > 0.05 ? "+" : "") + d.toFixed(1) + " dB";

function levelMeter(parent, label, title) {
  const row = el("div", "lvl-m"); row.title = title;
  const bar = el("div", "bar"), fill = el("div", "fill"); bar.append(fill);
  const val = el("span", "val", "—"), clip = el("span", "clip", "CLIP");
  row.append(el("span", "lbl", label), bar, val, clip);
  parent.append(row);
  return { fill, val, clip, clipUntil: 0 };
}

function renderLevels(lv) {
  const box = $("#levels"); box.innerHTML = ""; LV.els = {};
  LV.cur = { ...lv }; LV.rendered = true;
  if (!lv.available) {
    box.append(el("p", "hint", "Input/output trims unavailable: the Calf plugins are not installed in the image."));
    return;
  }
  const stages = [
    ["in", "Input trim", "card → chain",
      ["Card", "Level arriving from the card (before the trim). If it shows CLIP, lower the card's input gain knob: a digital trim cannot undo clipping in the card."],
      ["To chain", "Level going into the processing chain (after the trim)."]],
    ["out", "Output trim", "chain → radio",
      ["Chain", "Level leaving the processing chain (before the trim)."],
      ["To radio", "Level sent to the outputs (after the trim): this is what reaches the radio's USB/rear input."]],
  ];
  for (const [stage, title, sub, pre, post] of stages) {
    const key = stage + "_db";
    const g = el("div", "lvl");
    const head = el("div", "lvl-head");
    const val = el("span", "lvl-val", fmtDb(lv[key]));
    head.append(el("strong", null, title), el("span", "muted small", sub), val);
    const c = { name: title, unit: "G", min: undb(lv.min_db), max: undb(lv.max_db) };
    const sl = makeSlider(c, undb(lv[key]), v => {
      const d = Math.round(db(v) * 10) / 10;
      val.textContent = fmtDb(d); LV.cur[key] = d; sendLevels({ [key]: d });
    }, 1);
    sl.el.title = "Drag to change · double click = 0 dB · arrows/wheel when focused: 0.5 dB steps";
    const meters = el("div", "lvl-meters");
    LV.els[stage] = { pre: levelMeter(meters, pre[0], pre[1]), post: levelMeter(meters, post[0], post[1]), sl, val };
    g.append(head, sl.el, meters);
    if (stage === "out") {
      const lab = el("label", "lvl-opt");
      const cb = el("input"); cb.type = "checkbox"; cb.checked = !!lv.left_only;
      cb.onchange = () => { LV.cur.left_only = cb.checked; sendLevels({ left_only: cb.checked }); };
      lab.title = "The chain is mono and is sent to both channels. If the radio adds both channels of its USB input, sending only the left one lowers the level by about 6 dB.";
      lab.append(cb, document.createTextNode(" Left channel only"));
      g.append(lab);
      LV.els.out.cb = cb;
    }
    box.append(g);
  }
}

function updateLevelControls(lv) {
  if (!LV.rendered || !!lv.available !== !!LV.cur?.available) { renderLevels(lv); return; }
  LV.cur = { ...LV.cur, ...lv };
  for (const stage of ["in", "out"]) {
    const e = LV.els[stage]; if (!e) continue;
    const d = lv[stage + "_db"];
    if (d == null) continue;
    e.sl.set(undb(d)); e.val.textContent = fmtDb(d);
  }
  if (LV.els.out?.cb && lv.left_only != null) LV.els.out.cb.checked = !!lv.left_only;
}

function paintLevel(m, d) {
  const pct = Math.max(0, Math.min(100, (d + 60) / 60 * 100));
  m.fill.style.width = pct + "%"; m.fill.style.setProperty("--w", (100 / Math.max(pct, 1) * 100) + "%");
  m.val.textContent = d <= -119 ? "−∞" : d.toFixed(1);
}

UI.hooks.chain.push(v => { if (v.levels) updateLevelControls(v.levels); });
UI.hooks.levels.push(lv => updateLevelControls(lv));
UI.hooks.meters.push(values => {
  const now = Date.now();
  for (const stage of ["in", "out"]) {
    const e = LV.els[stage]; if (!e) continue;
    for (const k of ["pre", "post"]) {
      const d = values[`_${stage}.${k}`];
      if (d != null) paintLevel(e[k], d);
      if (values[`_${stage}.clip_${k}`]) e[k].clipUntil = now + 2000;     // hold the CLIP light for 2 s
      e[k].clip.classList.toggle("on", e[k].clipUntil > now);
    }
  }
});
