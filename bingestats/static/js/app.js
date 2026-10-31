// Binge Stats front-end glue. Alpine components and htmx config only.

// Progress bar that keeps moving between 3 s refreshes. The server says where
// playback was at `observedAt` (server clock); we add the time since, using
// the server's `now` from the partial so client clock skew doesn't matter.
document.addEventListener("alpine:init", () => {
  window.Alpine.data("progress", (offset, duration, state, observedAt) => ({
    pct: duration ? Math.min(100, (offset / duration) * 100) : 0,
    label: "",
    timer: null,
    init() {
      const host = this.$el.closest("[data-server-now]");
      const serverNow = host ? Number(host.dataset.serverNow) : observedAt;
      const loadedAt = Date.now() / 1000;
      const tick = () => {
        let pos = offset;
        if (state === "playing") pos += serverNow - observedAt + (Date.now() / 1000 - loadedAt);
        if (duration) pos = Math.min(pos, duration);
        this.pct = duration ? (pos / duration) * 100 : 0;
        this.label = fmt(pos);
      };
      tick();
      if (state === "playing") this.timer = setInterval(tick, 1000);
    },
    destroy() { clearInterval(this.timer); },
  }));

  window.Alpine.data("toast", () => ({
    show: true,
    init() { setTimeout(() => { this.show = false; setTimeout(() => this.$el.remove(), 300); }, 4000); },
  }));
});

function fmt(s) {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(sec).padStart(2, "0");
}
