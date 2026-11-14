"use strict";
// Edição da cadeia: arrastar blocos para mudar a ordem (pega ⋮⋮), mover com
// ◀ ▶ no painel e remover blocos. Depende de app.js (UI, api, el, toast…).

async function sendOrder(ids) {
  // otimista: reordena já na UI; o servidor confirma com uma mensagem "chain"
  const map = Object.fromEntries(UI.chain.blocks.map(b => [b.id, b]));
  UI.chain.blocks = ids.map(i => map[i]);
  renderStrip(); renderMeters();
  try { await api("POST", "/api/chain/order", { order: ids }); }
  catch (e) { toast("Não foi possível mudar a ordem: " + e.message); loadChain(); }
}

function startDrag(e, blk, strip) {
  if (e.button !== 0) return;
  e.preventDefault(); e.stopPropagation();
  const grip = e.currentTarget;
  grip.setPointerCapture(e.pointerId);
  const others = [...strip.querySelectorAll(".blk")].filter(b => b !== blk);
  const startX = e.clientX, startScroll = strip.scrollLeft;
  let target = null, timer = null, lastX = e.clientX;
  blk.classList.add("dragging");
  UI.busy = (UI.busy || 0) + 1;

  const mark = x => {
    others.forEach(b => b.classList.remove("ins-before", "ins-after"));
    let idx = others.findIndex(b => { const r = b.getBoundingClientRect(); return x < r.left + r.width / 2; });
    if (idx < 0) idx = others.length;
    if (idx < others.length) others[idx].classList.add("ins-before");
    else if (others.length) others[others.length - 1].classList.add("ins-after");
    target = idx;
  };
  const place = () => {
    blk.style.transform = `translateX(${lastX - startX + (strip.scrollLeft - startScroll)}px)`;
    mark(lastX);
  };
  // desliza a faixa quando se arrasta junto às margens
  const autoscroll = () => {
    const r = strip.getBoundingClientRect();
    const v = lastX < r.left + 48 ? -14 : lastX > r.right - 48 ? 14 : 0;
    if (v) { strip.scrollLeft += v; place(); }
  };
  timer = setInterval(autoscroll, 30);

  const move = ev => { lastX = ev.clientX; place(); };
  const end = () => {
    clearInterval(timer);
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", end);
    grip.removeEventListener("pointercancel", end);
    blk.classList.remove("dragging"); blk.style.transform = "";
    others.forEach(b => b.classList.remove("ins-before", "ins-after"));
    UI.busy = Math.max(0, (UI.busy || 1) - 1);
    const ids = others.map(b => b.dataset.id);
    if (target !== null) ids.splice(target, 0, blk.dataset.id);
    const cur = UI.chain.blocks.map(b => b.id);
    if (target !== null && ids.join() !== cur.join()) sendOrder(ids);
    else if (!UI.busy) flushDeferred();
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

// pega ⋮⋮ em cada bloco da faixa
UI.hooks.stripTail.push(strip => {
  strip.querySelectorAll(".blk").forEach(blk => {
    const g = el("span", "grip", "⋮⋮");
    g.title = "Arrastar para mudar a ordem";
    g.addEventListener("pointerdown", e => startDrag(e, blk, strip));
    g.addEventListener("click", e => e.stopPropagation());
    blk.prepend(g); blk.classList.add("has-grip");
  });
});

// ◀ ▶ e Remover no painel do bloco
UI.hooks.panelActions.push((b, actions) => {
  const ids = UI.chain.blocks.map(x => x.id);
  const i = ids.indexOf(b.id);
  const mv = (dir, label, title) => {
    const btn = el("button", "ghost icon", label); btn.title = title; btn.setAttribute("aria-label", title);
    btn.disabled = i + dir < 0 || i + dir >= ids.length;
    btn.onclick = () => { const n = ids.slice(); n.splice(i, 1); n.splice(i + dir, 0, b.id); sendOrder(n); };
    return btn;
  };
  const rm = el("button", "ghost danger", "Remover");
  rm.title = "Tirar este bloco da cadeia";
  rm.onclick = async () => {
    if (!confirm(`Remover "${b.name}" da cadeia?`)) return;
    const next = UI.chain.blocks[i + 1]?.id || UI.chain.blocks[i - 1]?.id || null;
    UI.selected = next;              // a mensagem "chain" do servidor já chega com este selecionado
    try { await api("DELETE", `/api/chain/blocks/${encodeURIComponent(b.id)}`); toast(`"${b.name}" removido`); }
    catch (e) { UI.selected = b.id; toast(e.message); }
  };
  actions.append(mv(-1, "◀", "Mover para trás na cadeia"), mv(1, "▶", "Mover para a frente na cadeia"), rm);
});
