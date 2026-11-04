// Downloads: move jobs in SABnzbd's queue. Loaded only when Settings allows it.
//
// Drag a TV/film card by its grip, or use its menu (top / bottom), or "TV & films
// first". The server works out SABnzbd's calls from the jobs either side of the drop.
// The page polls every 2 s; polling pauses while dragging, and after a move the page
// keeps what you see until a snapshot from a full queue read after the move arrives,
// so the card doesn't jump back to where it was for a few seconds.
(() => {
  const HOLD_MS = 60000;
  let sortable = null;
  let movedAt = 0;          // server time of the last move
  let holdUntil = 0;        // client time to stop waiting for a fresh read
  window.bsDragging = false;

  const host = () => document.getElementById("downloads-live");
  const csrf = () => {
    try { return JSON.parse(document.body.getAttribute("hx-headers") || "{}")["X-CSRF-Token"] || ""; } catch { return ""; }
  };

  function toast(message, kind) {
    const box = document.getElementById("toasts");
    if (!box) return;
    const el = document.createElement("div");
    el.className = "pointer-events-auto rounded-2xl border px-4 py-3 text-sm shadow-2xl backdrop-blur transition-opacity duration-300 " +
      (kind === "error" ? "border-danger/40 bg-danger/20" : "border-play/40 bg-surface/95");
    el.textContent = message;
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 4500);
  }

  async function post(url, data) {
    try {
      const r = await fetch(url, { method: "POST", headers: { "X-CSRF-Token": csrf() }, body: new URLSearchParams(data || {}) });
      try { return await r.json(); } catch { return { ok: false, error: r.status === 400 ? "Your session expired. Reload the page." : `HTTP ${r.status}` }; }
    } catch {
      return { ok: false, error: "Couldn't reach Binge Stats" };
    }
  }

  // After a move: say so, and hold the page until a full read after it has landed.
  function after(res, card, revert) {
    if (res.ok && res.moved) {
      movedAt = res.at || 0;
      holdUntil = Date.now() + HOLD_MS;
      const was = card && card.dataset.priority;
      if (res.priority_name && was && res.priority_name !== was) {
        toast(`Moved. It's ${res.priority_name} priority now, so it can sit there.`, "ok");
      } else if (res.moved > 1) {
        toast(`Moved ${res.moved} jobs to the front.`, "ok");
      }
    } else if (!res.ok) {
      if (revert) revert();
      if (card) card.classList.remove("dl-moving");
      toast(res.error || "SABnzbd didn't move it", "error");
    } else if (card) {
      card.classList.remove("dl-moving");              // nothing to do: it was already there
    }
    window.bsDragging = false;
  }

  document.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target !== host() || !movedAt) return;
    if (Date.now() > holdUntil) { movedAt = 0; return; }
    const m = /data-full-at="(\d+)"/.exec(e.detail.serverResponse || "");
    if (m && Number(m[1]) >= movedAt) { movedAt = 0; return; }
    e.detail.shouldSwap = false;                         // still the order from before the move
  });

  function bind() {
    const list = document.getElementById("dl-list");
    if (sortable && sortable.el === list) return;
    if (sortable) { sortable.destroy(); sortable = null; }
    if (!list || !window.Sortable || !host() || !host().dataset.moveUrl) return;
    let order = null;
    sortable = window.Sortable.create(list, {
      draggable: ".dl-row",          // groups are positions you can drop beside, but have no handle
      handle: ".drag-handle",
      delay: 150, delayOnTouchOnly: true, animation: 150,
      ghostClass: "dl-ghost", chosenClass: "dl-chosen", dragClass: "dl-dragging",
      onStart() { window.bsDragging = true; order = sortable.toArray(); },
      onEnd(evt) {
        if (evt.oldIndex === evt.newIndex) { window.bsDragging = false; return; }
        const card = evt.item;
        const prev = card.previousElementSibling, next = card.nextElementSibling;
        const above = prev ? (prev.dataset.id || prev.dataset.last || "") : "";
        const below = next ? (next.dataset.id || next.dataset.first || "") : "";
        card.classList.add("dl-moving");
        post(host().dataset.moveUrl, { nzo_id: card.dataset.id, above, below }).then((res) => {
          if (res.ok) card.classList.remove("dl-moving");
          after(res, card, () => sortable && sortable.sort(order, true));
        });
      },
    });
  }

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-move-to], [data-move-front]");
    if (!btn || !host()) return;
    e.preventDefault();
    if (btn.hasAttribute("data-move-front")) {
      btn.disabled = true;
      window.bsDragging = true;
      post(host().dataset.frontUrl).then((res) => { btn.disabled = false; after(res, null); });
      return;
    }
    const card = btn.closest(".dl-row");
    if (!card) return;
    card.classList.add("dl-moving");
    window.bsDragging = true;
    post(host().dataset.moveUrl, { nzo_id: card.dataset.id, to: btn.dataset.moveTo }).then((res) => after(res, card));
  });

  document.addEventListener("htmx:afterSettle", bind);
  document.addEventListener("DOMContentLoaded", bind);
})();
