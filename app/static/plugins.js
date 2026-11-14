"use strict";
// Plugins on demand: library to add blocks to the chain (+ button in the
// strip) and the "Plugins" tab to install/remove plugin packages.

const LIB = { list: null, at: null };

async function loadLibrary(force) {
  if (!LIB.list || force) LIB.list = (await api("GET", "/api/library")).plugins;
  const n = $("#libCount"); if (n) n.textContent = `· ${LIB.list.length} plugins available`;
  return LIB.list;
}

// ---------- add a block ----------
function openLibrary() {
  const ids = UI.chain.blocks.map(b => b.id);
  const i = ids.indexOf(UI.selected);
  LIB.at = i < 0 ? ids.length : i + 1;
  const after = UI.chain.blocks[LIB.at - 1];
  $("#libWhere").textContent = after ? `It will be inserted after “${after.name}”. You can drag it elsewhere afterwards.` : "It will be inserted at the start of the chain.";
  $("#libModal").hidden = false;
  $("#libSearch").value = "";
  $("#libList").innerHTML = '<p class="muted">Loading the library…</p>';
  loadLibrary().then(list => {
    const sel = $("#libClass"), cur = sel.value;
    const classes = [...new Set(list.map(p => p.class))].sort();
    sel.innerHTML = '<option value="">All types</option>' + classes.map(c => `<option>${c}</option>`).join("");
    sel.value = classes.includes(cur) ? cur : "";
    renderLibrary();
  }).catch(e => { $("#libList").innerHTML = ""; $("#libList").append(el("p", "muted", e.message)); });
  setTimeout(() => $("#libSearch").focus(), 30);
}
function closeLibrary() { $("#libModal").hidden = true; }

function renderLibrary() {
  const q = $("#libSearch").value.trim().toLowerCase();
  const cls = $("#libClass").value;
  const list = (LIB.list || []).filter(p => (!cls || p.class === cls) &&
    (!q || `${p.name} ${p.author} ${p.class} ${p.bundle}`.toLowerCase().includes(q)));
  const box = $("#libList"); box.innerHTML = "";
  if (!list.length) { box.append(el("p", "muted", "No plugins found.")); return; }
  for (const p of list.slice(0, 300)) {
    const row = el("div", "lib-row");
    const info = el("div", "lib-info");
    const title = el("div", "lib-name", p.name);
    const badges = el("span", "badges");
    badges.append(el("span", "badge", p.ins > 1 ? "stereo" : "mono"));
    if (p.sidechain) badges.append(el("span", "badge", "sidechain"));
    if (p.extra) badges.append(el("span", "badge extra", "extra package"));
    title.append(badges);
    info.append(title, el("div", "muted small", [p.class, p.author, p.bundle].filter(Boolean).join(" · ")));
    const add = el("button", null, "Add");
    add.onclick = async () => {
      add.disabled = true;
      try {
        const r = await api("POST", "/api/chain/blocks", { uri: p.uri, at: LIB.at });
        UI.selected = r.id; closeLibrary();
        toast(r.error ? `“${r.name}” added, but it did not load: ${r.error}` : `“${r.name}” added to the chain`, r.error ? 5000 : 2500);
        renderStrip(); renderPanel();
      } catch (e) { toast(e.message); add.disabled = false; }
    };
    row.append(info, add); box.append(row);
  }
  if (list.length > 300) box.append(el("p", "muted small", `… and ${list.length - 300} more: refine the search.`));
}

$("#libClose").onclick = closeLibrary;
$("#libModal").addEventListener("click", e => { if (e.target.id === "libModal") closeLibrary(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#libModal").hidden) closeLibrary(); });
$("#libSearch").addEventListener("input", renderLibrary);
$("#libClass").addEventListener("change", renderLibrary);

UI.hooks.stripTail.push(strip => {
  const add = el("button", "blk-add", "+");
  add.title = "Add a plugin to the chain (after the selected block)";
  add.setAttribute("aria-label", add.title);
  add.onclick = openLibrary;
  strip.append(add);
});

// ---------- Plugins tab (packages) ----------
let pkgBusy = false;
function showPkgLog(lines, open) {
  const box = $("#pkgLogBox"); box.hidden = false; box.open = !!open;
  $("#pkgLog").textContent = (lines || []).join("\n");
}
async function pkgAction(action, pkg) {
  if (pkgBusy) return;
  const verb = action === "install" ? "Install" : "Remove";
  if (!confirm(`${verb} package ${pkg}?\n\nThe audio engine restarts (about 2 s without audio). Do not do it while transmitting.`)) return;
  pkgBusy = true; renderPackages(LIB.pkgs, action === "install" ? pkg : null, action === "remove" ? pkg : null);
  try {
    const r = await api("POST", `/api/packages/${action}`, { pkg });
    showPkgLog(r.log, false);
    const m = r.manifest;
    if (action === "install") {
      toast(m.missing_libs.length ? `${pkg} installed, but libraries are missing: ${m.missing_libs.join(", ")}` : `${pkg} installed (${m.bundles.length} plugins). Use + in the Chain tab to add them.`, 6000);
    } else toast(`${pkg} removed`);
  } catch (e) {
    toast(e.message, 6000);
    if (e.data?.log?.length) showPkgLog(e.data.log, true);
  } finally {
    pkgBusy = false;
    await loadPackages();
    loadLibrary(true).catch(() => {});
  }
}
async function loadPackages() {
  try { const r = await api("GET", "/api/packages"); LIB.pkgs = r.packages; renderPackages(r.packages); }
  catch (e) { $("#packages").innerHTML = ""; $("#packages").append(el("p", "muted", e.message)); }
  loadLibrary().catch(() => {});
}
function renderPackages(pkgs, installing, removing) {
  const box = $("#packages"); box.innerHTML = "";
  for (const p of pkgs || []) {
    const card = el("div", "pkg");
    const head = el("div", "pkg-head");
    const t = el("div"); t.append(el("strong", null, p.title), el("code", "pkg-name", p.pkg));
    const state = { base: "in the image", installed: `installed${p.version ? " · " + p.version : ""}`, available: "not installed" }[p.state];
    head.append(t, el("span", "badge " + p.state, state));
    card.append(head, el("div", "muted small", p.desc));
    if (p.missing_libs?.length) card.append(el("div", "small warn-text", `Missing libraries: ${p.missing_libs.join(", ")}`));
    if (p.in_use) card.append(el("div", "small muted", "In use in the chain."));
    const btnRow = el("div", "pkg-actions");
    if (p.state === "available") {
      const b = el("button", null, installing === p.pkg ? "Installing…" : "Install");
      b.disabled = pkgBusy; b.onclick = () => pkgAction("install", p.pkg); btnRow.append(b);
    } else if (p.state === "installed") {
      const b = el("button", "ghost danger", removing === p.pkg ? "Removing…" : "Remove");
      b.disabled = pkgBusy || p.in_use; if (p.in_use) b.title = "First remove the blocks that use this package from the chain";
      b.onclick = () => pkgAction("remove", p.pkg); btnRow.append(b);
    }
    if (installing === p.pkg) btnRow.append(el("span", "muted small", "downloading from the Debian repository; this can take a minute"));
    card.append(btnRow); box.append(card);
  }
  $("#customInstall").disabled = pkgBusy;
}
$("#pkgRefresh").onclick = loadPackages;
$("#customInstall").onclick = () => {
  const v = $("#customPkg").value.trim().toLowerCase();
  if (v) pkgAction("install", v);
};
$("#customPkg").addEventListener("keydown", e => { if (e.key === "Enter") $("#customInstall").click(); });
UI.tabs.plugins = { onShow: loadPackages };
