"use strict";
// Plugins a pedido: biblioteca para adicionar blocos à cadeia (botão + na
// faixa) e separador "Plugins" para instalar/remover pacotes de plugins.

const LIB = { list: null, at: null };

async function loadLibrary(force) {
  if (!LIB.list || force) LIB.list = (await api("GET", "/api/library")).plugins;
  const n = $("#libCount"); if (n) n.textContent = `· ${LIB.list.length} plugins disponíveis`;
  return LIB.list;
}

// ---------- adicionar bloco ----------
function openLibrary() {
  const ids = UI.chain.blocks.map(b => b.id);
  const i = ids.indexOf(UI.selected);
  LIB.at = i < 0 ? ids.length : i + 1;
  const after = UI.chain.blocks[LIB.at - 1];
  $("#libWhere").textContent = after ? `Será inserido depois de “${after.name}”. Depois podes arrastá-lo para outra posição.` : "Será inserido no início da cadeia.";
  $("#libModal").hidden = false;
  $("#libSearch").value = "";
  $("#libList").innerHTML = '<p class="muted">A carregar a biblioteca…</p>';
  loadLibrary().then(list => {
    const sel = $("#libClass"), cur = sel.value;
    const classes = [...new Set(list.map(p => p.class))].sort();
    sel.innerHTML = '<option value="">Todos os tipos</option>' + classes.map(c => `<option>${c}</option>`).join("");
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
  if (!list.length) { box.append(el("p", "muted", "Nenhum plugin encontrado.")); return; }
  for (const p of list.slice(0, 300)) {
    const row = el("div", "lib-row");
    const info = el("div", "lib-info");
    const title = el("div", "lib-name", p.name);
    const badges = el("span", "badges");
    badges.append(el("span", "badge", p.ins > 1 ? "estéreo" : "mono"));
    if (p.sidechain) badges.append(el("span", "badge", "sidechain"));
    if (p.extra) badges.append(el("span", "badge extra", "pacote extra"));
    title.append(badges);
    info.append(title, el("div", "muted small", [p.class, p.author, p.bundle].filter(Boolean).join(" · ")));
    const add = el("button", null, "Adicionar");
    add.onclick = async () => {
      add.disabled = true;
      try {
        const r = await api("POST", "/api/chain/blocks", { uri: p.uri, at: LIB.at });
        UI.selected = r.id; closeLibrary();
        toast(r.error ? `“${r.name}” adicionado, mas não carregou: ${r.error}` : `“${r.name}” adicionado à cadeia`, r.error ? 5000 : 2500);
        renderStrip(); renderPanel();
      } catch (e) { toast(e.message); add.disabled = false; }
    };
    row.append(info, add); box.append(row);
  }
  if (list.length > 300) box.append(el("p", "muted small", `… e mais ${list.length - 300}: refina a pesquisa.`));
}

$("#libClose").onclick = closeLibrary;
$("#libModal").addEventListener("click", e => { if (e.target.id === "libModal") closeLibrary(); });
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#libModal").hidden) closeLibrary(); });
$("#libSearch").addEventListener("input", renderLibrary);
$("#libClass").addEventListener("change", renderLibrary);

UI.hooks.stripTail.push(strip => {
  const add = el("button", "blk-add", "+");
  add.title = "Adicionar um plugin à cadeia (depois do bloco selecionado)";
  add.setAttribute("aria-label", add.title);
  add.onclick = openLibrary;
  strip.append(add);
});

// ---------- separador Plugins (pacotes) ----------
let pkgBusy = false;
function showPkgLog(lines, open) {
  const box = $("#pkgLogBox"); box.hidden = false; box.open = !!open;
  $("#pkgLog").textContent = (lines || []).join("\n");
}
async function pkgAction(action, pkg) {
  if (pkgBusy) return;
  const verb = action === "install" ? "Instalar" : "Remover";
  if (!confirm(`${verb} o pacote ${pkg}?\n\nO motor de áudio reinicia (cerca de 2 s sem áudio). Não o faças durante uma emissão.`)) return;
  pkgBusy = true; renderPackages(LIB.pkgs, action === "install" ? pkg : null, action === "remove" ? pkg : null);
  try {
    const r = await api("POST", `/api/packages/${action}`, { pkg });
    showPkgLog(r.log, false);
    const m = r.manifest;
    if (action === "install") {
      toast(m.missing_libs.length ? `${pkg} instalado, mas faltam bibliotecas: ${m.missing_libs.join(", ")}` : `${pkg} instalado (${m.bundles.length} plugins). Usa o + na Cadeia para os adicionar.`, 6000);
    } else toast(`${pkg} removido`);
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
    const state = { base: "na imagem", installed: `instalado${p.version ? " · " + p.version : ""}`, available: "por instalar" }[p.state];
    head.append(t, el("span", "badge " + p.state, state));
    card.append(head, el("div", "muted small", p.desc));
    if (p.missing_libs?.length) card.append(el("div", "small warn-text", `Bibliotecas em falta: ${p.missing_libs.join(", ")}`));
    if (p.in_use) card.append(el("div", "small muted", "Em uso na cadeia."));
    const btnRow = el("div", "pkg-actions");
    if (p.state === "available") {
      const b = el("button", null, installing === p.pkg ? "A instalar…" : "Instalar");
      b.disabled = pkgBusy; b.onclick = () => pkgAction("install", p.pkg); btnRow.append(b);
    } else if (p.state === "installed") {
      const b = el("button", "ghost danger", removing === p.pkg ? "A remover…" : "Remover");
      b.disabled = pkgBusy || p.in_use; if (p.in_use) b.title = "Remove primeiro da cadeia os blocos que usam este pacote";
      b.onclick = () => pkgAction("remove", p.pkg); btnRow.append(b);
    }
    if (installing === p.pkg) btnRow.append(el("span", "muted small", "a descarregar do repositório Debian; pode demorar um minuto"));
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
