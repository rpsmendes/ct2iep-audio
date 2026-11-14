"use strict";
// Frequency-response graph for EQ/filter blocks.
// The curve is computed on the server with the plugin itself (/api/response):
// solid line = this block; dashed = sum of all active EQs/filters.

const G = {
  W: 720, H: 230, padL: 38, padR: 12, padT: 12, padB: 24,
  fmin: 20, fmax: 20000, ymin: -30, ymax: 18,
  ssb: [300, 2700],
  timer: null, inflight: false, again: false, data: null,
};
const NS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
const gx = f => G.padL + Math.log10(f / G.fmin) / Math.log10(G.fmax / G.fmin) * (G.W - G.padL - G.padR);
const gy = d => G.padT + (G.ymax - Math.max(G.ymin - 6, Math.min(G.ymax + 6, d))) / (G.ymax - G.ymin) * (G.H - G.padT - G.padB);
const fLabel = f => f >= 1000 ? (f / 1000) + "k" : String(f);

function graphShell(b) {
  const box = el("div", "graph");
  const svg = sv("svg", { viewBox: `0 0 ${G.W} ${G.H}`, role: "img", "aria-label": `Frequency response of ${b.name}` });
  svg.classList.add("graph-svg");
  box.append(svg);
  const foot = el("div", "graph-foot");
  const legend = el("div", "legend");
  legend.innerHTML = '<span class="lg lg-block">This block</span><span class="lg lg-total">All EQs/filters</span>';
  const read = el("div", "readout muted", "Hover over the graph to read values");
  foot.append(legend, read);
  box.append(foot);
  return box;
}

function drawGrid(svg) {
  // draw at the real width (1 unit = 1 px): text always has the same size
  const w = svg.parentElement ? svg.parentElement.clientWidth - 16 : 0;
  G.W = Math.max(300, w || G.W);
  G.H = G.W < 520 ? 190 : 240;
  svg.setAttribute("viewBox", `0 0 ${G.W} ${G.H}`);
  svg.innerHTML = "";
  const defs = sv("defs", {});
  const clip = sv("clipPath", { id: "gclip" });
  clip.append(sv("rect", { x: G.padL, y: G.padT, width: G.W - G.padL - G.padR, height: G.H - G.padT - G.padB }));
  defs.append(clip); svg.append(defs);
  const [a, b] = G.ssb;
  svg.append(sv("rect", { class: "g-band", x: gx(a), y: G.padT, width: gx(b) - gx(a), height: G.H - G.padT - G.padB }));
  const t = sv("text", { class: "g-band-label", x: (gx(a) + gx(b)) / 2, y: G.padT + 11, "text-anchor": "middle" });
  t.textContent = "SSB band"; svg.append(t);
  const ticks = G.W < 520 ? [100, 300, 1000, 3000, 10000] : [50, 100, 200, 500, 1000, 2000, 5000, 10000];
  for (const f of ticks) {
    svg.append(sv("line", { class: "g-grid", x1: gx(f), x2: gx(f), y1: G.padT, y2: G.H - G.padB }));
    const l = sv("text", { class: "g-lbl", x: gx(f), y: G.H - 7, "text-anchor": "middle" }); l.textContent = fLabel(f); svg.append(l);
  }
  for (let d = G.ymax; d >= G.ymin; d -= 6) {
    svg.append(sv("line", { class: d === 0 ? "g-zero" : "g-grid", x1: G.padL, x2: G.W - G.padR, y1: gy(d), y2: gy(d) }));
    const l = sv("text", { class: "g-lbl", x: G.padL - 5, y: gy(d) + 4, "text-anchor": "end" }); l.textContent = (d > 0 ? "+" : "") + d; svg.append(l);
  }
}

function pathOf(freqs, vals) {
  return freqs.map((f, i) => `${i ? "L" : "M"}${gx(f).toFixed(1)},${gy(vals[i]).toFixed(1)}`).join("");
}

function drawCurves(box, data) {
  const svg = box.querySelector("svg");
  drawGrid(svg);
  const g = sv("g", { "clip-path": "url(#gclip)" });
  if (data.total) g.append(sv("path", { class: "g-total", d: pathOf(data.freqs, data.total) }));
  g.append(sv("path", { class: "g-curve" + (data.bypassed ? " bypassed" : ""), d: pathOf(data.freqs, data.block) }));
  const cursor = sv("line", { class: "g-cursor", x1: 0, x2: 0, y1: G.padT, y2: G.H - G.padB, visibility: "hidden" });
  g.append(cursor); svg.append(g);
  const lgT = box.querySelector(".lg-total");
  lgT.hidden = !data.total;
  if (data.total) lgT.textContent = `All EQs/filters (${data.parts.length})`;
  lgT.title = data.parts.join(", ");
  box.querySelector(".lg-block").textContent = data.bypassed ? "This block (bypassed)" : "This block";

  const read = box.querySelector(".readout");
  const pick = ev => {
    const r = svg.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width * G.W;
    if (x < G.padL || x > G.W - G.padR) { cursor.setAttribute("visibility", "hidden"); return; }
    const f = G.fmin * Math.pow(G.fmax / G.fmin, (x - G.padL) / (G.W - G.padL - G.padR));
    let k = 0, best = Infinity;
    data.freqs.forEach((v, i) => { const d = Math.abs(Math.log(v / f)); if (d < best) { best = d; k = i; } });
    cursor.setAttribute("x1", gx(data.freqs[k])); cursor.setAttribute("x2", gx(data.freqs[k])); cursor.setAttribute("visibility", "visible");
    const fs = data.freqs[k] >= 1000 ? (data.freqs[k] / 1000).toFixed(2) + " kHz" : Math.round(data.freqs[k]) + " Hz";
    const s = v => (v > 0.05 ? "+" : "") + v.toFixed(1) + " dB";
    read.textContent = `${fs}: ${s(data.block[k])}` + (data.total ? ` · total ${s(data.total[k])}` : "");
  };
  svg.onpointermove = pick; svg.onpointerdown = pick;
  svg.onpointerleave = () => cursor.setAttribute("visibility", "hidden");
}

async function refreshGraph() {
  const b = blockById(UI.selected);
  const box = document.querySelector("#panel .graph");
  if (!b || !b.graph || !box) return;
  if (G.inflight) { G.again = true; return; }
  G.inflight = true;
  try {
    const data = await api("GET", `/api/response?block=${encodeURIComponent(b.id)}`);
    if (UI.selected === b.id && document.querySelector("#panel .graph") === box) { G.data = { ...data, blockId: b.id }; drawCurves(box, data); }
  } catch (e) {
    box.querySelector(".readout").textContent = "Graph unavailable: " + e.message;
  } finally {
    G.inflight = false;
    if (G.again) { G.again = false; scheduleGraph(0); }
  }
}
function scheduleGraph(ms = 120) { clearTimeout(G.timer); G.timer = setTimeout(refreshGraph, ms); }

UI.hooks.panel.push((b, panel) => {
  if (b.graph) {
    const box = graphShell(b);
    panel.append(box);
    drawGrid(box.querySelector("svg"));
    if (G.data && G.data.blockId === b.id) drawCurves(box, G.data);
    scheduleGraph(0);
  } else if (b.graph_unsupported) {
    panel.append(el("p", "hint", "No graph for this plugin: it cannot be measured offline (it has ports that lv2apply does not support)."));
  }
});
// any change in an EQ/filter changes the total; so does bypass (b null = bypass all)
UI.hooks.param.push(b => {
  const sel = blockById(UI.selected);
  if (sel && sel.graph && (!b || b.graph)) scheduleGraph();
});
let resizeT = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeT);
  resizeT = setTimeout(() => {
    const box = document.querySelector("#panel .graph");
    if (box && G.data && G.data.blockId === UI.selected) drawCurves(box, G.data);
  }, 150);
});
UI.hooks.chain.push(() => { const sel = blockById(UI.selected); if (sel && sel.graph) scheduleGraph(0); });
