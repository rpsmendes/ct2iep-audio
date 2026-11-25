"use strict";
// Chain editing: drag blocks to change the order (⋮⋮ handle), move them with
// ◀ ▶ in the panel and remove blocks. Depends on app.js (UI, api, el, toast…).

async function sendOrder(ids) {
  // optimistic: reorder in the UI right away; the server confirms with a "chain" message
  const map = Object.fromEntries(UI.chain.blocks.map(b => [b.id, b]));
  UI.chain.blocks = ids.map(i => map[i]);
  renderStrip(); renderMeters();
  try { await api("POST", "/api/chain/order", { order: ids }); }
  catch (e) { toast("Could not change the order: " + e.message, 4000, true); loadChain(); }
}

function startDrag(e, blk, strip) {
  if (e.button !== 0) return;
  e.preventDefault(); e.stopPropagation();
  const grip = e.currentTarget;
  grip.setPointerCapture(e.pointerId);
  const others = [...strip.querySelectorAll(".blk")].filter(b => b !== blk);
  const startX = e.clientX, startY = e.clientY, startScroll = window.scrollY;
  let target = null, timer = null, lastX = e.clientX, lastY = e.clientY;
  blk.classList.add("dragging");
  UI.busy = (UI.busy || 0) + 1;

  // the blocks wrap in rows (signal order = reading order): insert before the
  // first block that is on a lower row, or on the same row and right of the pointer
  const mark = (x, y) => {
    others.forEach(b => b.classList.remove("ins-before", "ins-after"));
    let idx = others.findIndex(b => {
      const r = b.getBoundingClientRect();
      return y < r.top || (y <= r.bottom && x < r.left + r.width / 2);
    });
    if (idx < 0) idx = others.length;
    if (idx < others.length) others[idx].classList.add("ins-before");
    else if (others.length) others[others.length - 1].classList.add("ins-after");
    target = idx;
  };
  const place = () => {
    blk.style.transform = `translate(${lastX - startX}px, ${lastY - startY + (window.scrollY - startScroll)}px)`;
    mark(lastX, lastY);
  };
  // scroll the page while dragging near the top/bottom of the window
  const autoscroll = () => {
    const v = lastY < 60 ? -14 : lastY > window.innerHeight - 60 ? 14 : 0;
    if (v) { window.scrollBy(0, v); place(); }
  };
  timer = setInterval(autoscroll, 30);

  const move = ev => { lastX = ev.clientX; lastY = ev.clientY; place(); };
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

// ⋮⋮ handle on every block of the strip
UI.hooks.stripTail.push(strip => {
  strip.querySelectorAll(".blk").forEach(blk => {
    const g = el("span", "grip", "⋮⋮");
    g.title = "Drag to change the order";
    g.addEventListener("pointerdown", e => startDrag(e, blk, strip));
    g.addEventListener("click", e => e.stopPropagation());
    blk.prepend(g); blk.classList.add("has-grip");
  });
});

// ◀ ▶ and Remove in the block panel
UI.hooks.panelActions.push((b, actions) => {
  const ids = UI.chain.blocks.map(x => x.id);
  const i = ids.indexOf(b.id);
  const mv = (dir, label, title) => {
    const btn = el("button", "ghost icon", label); btn.title = title; btn.setAttribute("aria-label", title);
    btn.disabled = i + dir < 0 || i + dir >= ids.length;
    btn.onclick = () => { const n = ids.slice(); n.splice(i, 1); n.splice(i + dir, 0, b.id); sendOrder(n); };
    return btn;
  };
  const rm = el("button", "ghost danger", "Remove");
  rm.title = "Take this block out of the chain";
  rm.onclick = async () => {
    if (!confirm(`Remove "${b.name}" from the chain?`)) return;
    const next = UI.chain.blocks[i + 1]?.id || UI.chain.blocks[i - 1]?.id || null;
    UI.selected = next;              // the server's "chain" message then arrives with this one selected
    try { await api("DELETE", `/api/chain/blocks/${encodeURIComponent(b.id)}`); toast(`"${b.name}" removed`); }
    catch (e) { UI.selected = b.id; toast(e.message, 4000, true); }
  };
  actions.append(mv(-1, "◀", "Move earlier in the chain"), mv(1, "▶", "Move later in the chain"), rm);
});
