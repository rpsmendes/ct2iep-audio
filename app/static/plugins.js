"use strict";
// Plugins on demand: library to add blocks to the chain (+ button in the
// strip) and the "Plugins" tab to install/remove plugin packages.

const LIB = { list: null, at: null };

async function loadLibrary(force) {
  if (!LIB.list || force) LIB.list = (await api("GET", "/api/library")).plugins;
  const n = $("#libCount"); if (n) n.textContent = `· ${LIB.list.length} plugins available`;
  return LIB.list;
}

// ---------- plugin browser (search, filters, pages) ----------
// Used in the "add a block" dialog and in the Plugins tab.
const PAGE_SIZES = [20, 50, 100];
function makeBrowser(root, opts) {
  const st = { q: "", cls: "", src: "", page: 0, size: 20 };
  try { st.size = +localStorage.getItem("ct2iep.libPageSize") || 20; } catch (_) { /* no storage */ }
  root.innerHTML = "";
  const filters = el("div", "lib-filters");
  const q = el("input"); q.type = "search"; q.placeholder = "Search (name, author, type, package)…"; q.autocomplete = "off";
  q.setAttribute("aria-label", "Search plugins");
  const cls = el("select"); cls.setAttribute("aria-label", "Type");
  cls.innerHTML = '<option value="">All types</option>';
  const src = el("select"); src.setAttribute("aria-label", "Source");
  src.innerHTML = '<option value="">All sources</option><option value="base">In the image</option><option value="extra">Installed from the UI</option>';
  filters.append(q, cls, src);
  const info = el("div", "lib-info-row muted small");
  const list = el("div", "lib-list");
  const pager = el("div", "pager");
  root.append(filters, info, list, pager);

  const filtered = () => (LIB.list || []).filter(p => (!st.cls || p.class === st.cls) &&
    (!st.src || (st.src === "extra") === !!p.extra) &&
    (!st.q || `${p.name} ${p.author} ${p.class} ${p.bundle} ${p.uri}`.toLowerCase().includes(st.q)));

  function render() {
    const all = filtered();
    const pages = Math.max(1, Math.ceil(all.length / st.size));
    st.page = Math.min(st.page, pages - 1);
    const from = st.page * st.size, slice = all.slice(from, from + st.size);
    info.textContent = all.length ? `${from + 1}–${from + slice.length} of ${all.length} plugins` + (all.length < (LIB.list || []).length ? ` (filtered from ${LIB.list.length})` : "") : "";
    list.innerHTML = "";
    if (!all.length) list.append(el("p", "muted", "No plugins found."));
    for (const p of slice) {
      const row = el("div", "lib-row");
      const inf = el("div", "lib-info");
      const title = el("div", "lib-name", p.name);
      const badges = el("span", "badges");
      badges.append(el("span", "badge", p.ins > 1 ? "stereo" : "mono"));
      if (p.sidechain) badges.append(el("span", "badge", "sidechain"));
      if (p.extra) badges.append(el("span", "badge extra", "extra package"));
      title.append(badges);
      const sub = el("div", "muted small", [p.class, p.author, p.bundle].filter(Boolean).join(" · "));
      sub.title = p.uri;
      inf.append(title, sub);
      const add = el("button", null, "Add");
      add.title = opts.addTitle ? opts.addTitle() : "Add to the chain";
      add.onclick = async () => { add.disabled = true; try { await opts.onAdd(p); } finally { add.disabled = false; } };
      row.append(inf, add); list.append(row);
    }
    pager.innerHTML = "";
    if (all.length > PAGE_SIZES[0]) {
      const btn = (label, page, title) => {
        const b = el("button", "ghost", label); b.title = title; b.setAttribute("aria-label", title);
        b.disabled = page < 0 || page >= pages || page === st.page;
        b.onclick = () => { st.page = page; render(); list.scrollTop = 0; };
        return b;
      };
      const pageSel = el("select"); pageSel.setAttribute("aria-label", "Page");
      for (let i = 0; i < pages; i++) { const o = el("option", null, `Page ${i + 1} of ${pages}`); o.value = i; pageSel.append(o); }
      pageSel.value = st.page; pageSel.onchange = () => { st.page = +pageSel.value; render(); list.scrollTop = 0; };
      const sizeSel = el("select"); sizeSel.setAttribute("aria-label", "Plugins per page");
      for (const n of PAGE_SIZES) { const o = el("option", null, `${n} per page`); o.value = n; sizeSel.append(o); }
      sizeSel.value = st.size;
      sizeSel.onchange = () => {
        st.page = Math.floor(st.page * st.size / +sizeSel.value); st.size = +sizeSel.value;
        try { localStorage.setItem("ct2iep.libPageSize", st.size); } catch (_) { /* no storage */ }
        render();
      };
      pager.append(btn("«", 0, "First page"), btn("‹", st.page - 1, "Previous page"), pageSel,
        btn("›", st.page + 1, "Next page"), btn("»", pages - 1, "Last page"), sizeSel);
    }
  }
  function fillClasses() {
    const cur = st.cls;
    const classes = [...new Set((LIB.list || []).map(p => p.class))].sort();
    cls.innerHTML = '<option value="">All types</option>';
    for (const c of classes) cls.append(el("option", null, c));
    st.cls = classes.includes(cur) ? cur : ""; cls.value = st.cls;
  }
  q.addEventListener("input", () => { st.q = q.value.trim().toLowerCase(); st.page = 0; render(); });
  cls.addEventListener("change", () => { st.cls = cls.value; st.page = 0; render(); });
  src.addEventListener("change", () => { st.src = src.value; st.page = 0; render(); });
  return {
    async load(force) {
      list.innerHTML = '<p class="muted">Loading the library…</p>';
      try { await loadLibrary(force); fillClasses(); render(); }
      catch (e) { list.innerHTML = ""; list.append(el("p", "muted", e.message)); }
    },
    reset() { st.q = ""; q.value = ""; st.page = 0; },
    focus() { q.focus(); },
  };
}

// position where a new block goes: after the selected block
function insertPos() {
  const ids = (UI.chain?.blocks || []).map(b => b.id);
  const i = ids.indexOf(UI.selected);
  return i < 0 ? ids.length : i + 1;
}
async function addPlugin(p, at) {
  try {
    const r = await api("POST", "/api/chain/blocks", { uri: p.uri, at });
    UI.selected = r.id;
    toast(r.error ? `“${r.name}” added, but it did not load: ${r.error}` : `“${r.name}” added to the chain`, r.error ? 5000 : 2500);
    renderStrip(); renderPanel();
    return true;
  } catch (e) { toast(e.message); return false; }
}

// ---------- add a block (dialog) ----------
const modalBrowser = makeBrowser($("#libBrowser"), {
  onAdd: async p => { if (await addPlugin(p, LIB.at)) closeLibrary(); },
});
function openLibrary() {
  LIB.at = insertPos();
  const after = UI.chain.blocks[LIB.at - 1];
  $("#libWhere").textContent = after ? `It will be inserted after “${after.name}”. You can drag it elsewhere afterwards.` : "It will be inserted at the start of the chain.";
  $("#libModal").hidden = false;
  modalBrowser.reset(); modalBrowser.load();
  setTimeout(() => modalBrowser.focus(), 30);
}
function closeLibrary() { $("#libModal").hidden = true; }

$("#libClose").onclick = closeLibrary;
$("#libModal").addEventListener("click", e => { if (e.target.id === "libModal") closeLibrary(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#libModal").hidden) closeLibrary(); });

// ---------- browser in the Plugins tab ----------
const tabBrowser = makeBrowser($("#pluginBrowser"), {
  addTitle: () => { const b = UI.chain?.blocks[insertPos() - 1]; return b ? `Add to the chain after “${b.name}”` : "Add at the start of the chain"; },
  onAdd: p => addPlugin(p, insertPos()),
});

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
    tabBrowser.load(true);
  }
}
async function loadPackages() {
  try { const r = await api("GET", "/api/packages"); LIB.pkgs = r.packages; renderPackages(r.packages); }
  catch (e) { $("#packages").innerHTML = ""; $("#packages").append(el("p", "muted", e.message)); }
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
UI.tabs.plugins = { onShow: () => { loadPackages(); tabBrowser.load(); } };
