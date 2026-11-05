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

// Notification rule editor: condition builder + variable inserter.
document.addEventListener("alpine:init", () => {
  window.Alpine.data("ruleEditor", () => {
    const data = JSON.parse(document.getElementById("rule-data").textContent);
    return {
      trigger: data.trigger,
      conditions: data.conditions.map((c) => ({ ...c })),
      fields: data.fields, choices: data.choices, opLabels: data.opLabels, variables: data.variables,
      target: null,
      field(c) {
        return this.fields[this.trigger].find((f) => f.field === c.field) || { ops: [], kind: "str" };
      },
      blank(f) {
        return f.kind === "choice" ? [] : f.kind === "list" ? (this.choices[f.choices][0] || {}).value : f.kind === "int" ? 0 : "";
      },
      addCondition() {
        const f = this.fields[this.trigger][0];
        this.conditions.push({ field: f.field, op: f.ops[0], value: this.blank(f) });
      },
      fieldChanged(c) {
        const f = this.field(c);
        c.op = f.ops[0];
        c.value = this.blank(f);
      },
      toggleChoice(c, v) {
        const vals = Array.isArray(c.value) ? c.value : [];
        c.value = vals.includes(v) ? vals.filter((x) => x !== v) : [...vals, v];
      },
      triggerChanged() {
        const ok = new Set(this.fields[this.trigger].map((f) => f.field));
        this.conditions = this.conditions.filter((c) => ok.has(c.field));
      },
      insert(name) {
        const el = this.target || document.getElementById("body_template");
        const text = "{{ " + name + " }}";
        const at = el.selectionStart ?? el.value.length;
        el.value = el.value.slice(0, at) + text + el.value.slice(el.selectionEnd ?? at);
        el.focus();
        el.selectionStart = el.selectionEnd = at + text.length;
        el.dispatchEvent(new Event("input", { bubbles: true }));
      },
    };
  });
});

// Filter forms marked data-tidy-query leave out empty fields (and defaults named in the
// attribute, e.g. "sort=recent"), so the address bar and bookmarks stay short.
document.addEventListener("htmx:configRequest", (e) => {
  const form = e.detail.elt;
  if (!form.matches || !form.matches("form[data-tidy-query]")) return;
  const defaults = new Set((form.dataset.tidyQuery || "").split(" ").filter(Boolean));
  const p = e.detail.formData;
  for (const k of [...new Set(p.keys())]) {
    const keep = p.getAll(k).filter((v) => v !== "" && !defaults.has(`${k}=${v}`));
    p.delete(k);
    keep.forEach((v) => p.append(k, v));
  }
});
