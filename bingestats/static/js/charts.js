// Binge Stats charts: one shared ECharts theme and a tiny component.
//
//   <div data-chart="bars|line|hbar|heatmap|flow" data-src="/api/v1/stats/..."
//        data-unit="plays|hours|streams|pct"></div>
//
// The element fetches its data (session cookie auth), renders, and shows an
// empty state when there's nothing to plot. Rules follow the dataviz guidance:
// a validated categorical palette in fixed order (colour follows the entity,
// via the `slot` the server sends), bars <= 24px with 4px rounded ends, a 2px
// surface gap between stacked segments, solid hairline grid, a legend only
// for two or more series, a hover tooltip on every chart.
(function () {
  "use strict";

  // Validated against the app's own card surfaces (dark #11141d, light #fff).
  const PALETTE = {
    dark: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
    light: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  };
  const OTHER = { dark: "#5c6378", light: "#a3a9b8" };
  // Sequential (one hue, blue), near-zero recedes toward the surface.
  const SEQ = {
    dark: ["#161c2b", "#104281", "#1c5cab", "#2a78d6", "#5598e7", "#9ec5f4"],
    light: ["#f2f6fc", "#cde2fb", "#9ec5f4", "#5598e7", "#256abf", "#104281"],
  };

  function mode() {
    return document.documentElement.dataset.theme === "light" ? "light" : "dark";
  }

  function cssVar(name) {
    const v = getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim();
    return v ? `rgb(${v.split(/\s+/).join(",")})` : "#888";
  }

  function tokens() {
    return {
      text: cssVar("text"), muted: cssVar("muted"), faint: cssVar("faint"),
      border: cssVar("border"), surface: cssVar("surface"), surface2: cssVar("surface-2"),
    };
  }

  function colourFor(s, i) {
    const m = mode();
    if (s.name === "Other") return OTHER[m];
    const slot = Number.isInteger(s.slot) ? s.slot : i;
    return PALETTE[m][slot % PALETTE[m].length];
  }

  function fmt(v, unit) {
    if (v == null) return "–";
    if (unit === "pct") return Math.round(v) + "%";
    if (unit === "gb") return v >= 1000 ? (v / 1000).toFixed(1) + " TB" : (v >= 100 ? Math.round(v) : v.toFixed(1)) + " GB";
    if (unit === "hours") {
      if (v < 1) return Math.round(v * 60) + "m";
      return (v >= 100 ? Math.round(v) : v.toFixed(1)).toLocaleString("en-GB") + "h";
    }
    return Number(v).toLocaleString("en-GB");
  }

  function axisNum(v) {
    if (Math.abs(v) >= 10000) return (v / 1000).toFixed(0) + "K";
    return Number(v).toLocaleString("en-GB");
  }

  function base(t) {
    return {
      animationDuration: 400,
      textStyle: { fontFamily: getComputedStyle(document.body).fontFamily, color: t.muted, fontSize: 11 },
      grid: { left: 8, right: 12, top: 16, bottom: 4, containLabel: true },
      tooltip: {
        backgroundColor: t.surface, borderColor: t.border, borderWidth: 1, padding: [8, 10],
        textStyle: { color: t.text, fontSize: 12 }, extraCssText: "border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.35);",
      },
    };
  }

  function legend(t, n) {
    if (n < 2) return { show: false };
    return { show: true, top: 0, left: 0, icon: "roundRect", itemWidth: 10, itemHeight: 10, itemGap: 14,
             textStyle: { color: t.muted, fontSize: 11 } };
  }

  function catAxis(t, labels, extra) {
    return Object.assign({
      type: "category", data: labels, axisTick: { show: false },
      axisLine: { lineStyle: { color: t.border } }, axisLabel: { color: t.muted, hideOverlap: true },
    }, extra || {});
  }

  function valAxis(t, unit, extra) {
    return Object.assign({
      type: "value", minInterval: unit === "hours" || unit === "gb" ? 0 : 1, max: unit === "pct" ? 100 : null,
      splitLine: { lineStyle: { color: t.border, width: 1, opacity: 0.6 } },
      axisLabel: { color: t.muted, formatter: (v) => unit === "hours" ? axisNum(v) + "h" : unit === "pct" ? v + "%"
                   : unit === "gb" ? (v >= 1000 ? (v / 1000).toFixed(v % 1000 ? 1 : 0) + " TB" : axisNum(v) + " GB") : axisNum(v) },
    }, extra || {});
  }

  function tooltipRows(params, unit) {
    const list = Array.isArray(params) ? params : [params];
    const head = list.length ? `<div style="margin-bottom:4px;font-weight:600">${list[0].axisValueLabel || list[0].name}</div>` : "";
    const rows = list.filter((p) => p.value).map((p) =>
      `<div style="display:flex;align-items:center;gap:8px;justify-content:space-between">
         <span>${p.marker}${p.seriesName === "Plays" || p.seriesName === "value" ? "" : p.seriesName}</span>
         <b style="font-variant-numeric:tabular-nums">${fmt(Array.isArray(p.value) ? p.value[2] : p.value, unit)}</b></div>`).join("");
    const total = list.reduce((a, p) => a + (Number(p.value) || 0), 0);
    const foot = unit !== "pct" && list.filter((p) => p.value).length > 1
      ? `<div style="margin-top:4px;border-top:1px solid rgba(127,127,127,.25);padding-top:4px;display:flex;justify-content:space-between"><span>Total</span><b>${fmt(Math.round(total * 10) / 10, unit)}</b></div>` : "";
    return head + (rows || "<div>Nothing</div>") + foot;
  }

  // -- builders ----------------------------------------------------------------

  const build = {
    bars(d, unit, t) {
      const n = d.series.length;
      return Object.assign(base(t), {
        legend: legend(t, n),
        grid: { left: 8, right: 12, top: n > 1 ? 34 : 16, bottom: 4, containLabel: true },
        tooltip: Object.assign(base(t).tooltip, { trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(127,127,127,.08)" } },
                                                  formatter: (p) => tooltipRows(p, unit) }),
        xAxis: catAxis(t, d.labels),
        yAxis: valAxis(t, unit),
        series: d.series.map((s, i) => ({
          name: s.name, type: "bar", stack: n > 1 ? "total" : undefined, data: s.data, barMaxWidth: 24,
          itemStyle: { color: colourFor(s, i), borderColor: t.surface, borderWidth: n > 1 ? 1 : 0,
                       borderRadius: i === n - 1 ? [4, 4, 0, 0] : 0 },
          emphasis: { focus: "series" },
        })),
      });
    },
    line(d, unit, t) {
      const n = d.series.length;
      return Object.assign(base(t), {
        legend: legend(t, n),
        grid: { left: 8, right: 12, top: n > 1 ? 34 : 16, bottom: 4, containLabel: true },
        tooltip: Object.assign(base(t).tooltip, { trigger: "axis", formatter: (p) => tooltipRows(p, unit),
                                                  axisPointer: { type: "line", lineStyle: { color: t.faint } } }),
        xAxis: catAxis(t, d.labels, { boundaryGap: false }),
        yAxis: valAxis(t, unit),
        series: d.series.map((s, i) => ({
          name: s.name, type: "line", data: s.data, step: unit === "streams" ? "middle" : false,
          smooth: unit === "streams" ? false : 0.25, showSymbol: false, symbolSize: 8,
          lineStyle: { width: 2, color: colourFor(s, i), cap: "round", join: "round" },
          itemStyle: { color: colourFor(s, i), borderColor: t.surface, borderWidth: 2 },
          areaStyle: { color: colourFor(s, i), opacity: 0.1 },
        })),
      });
    },
    hbar(d, unit, t) {
      const items = d.items.slice().reverse();
      return Object.assign(base(t), {
        grid: { left: 8, right: 48, top: 4, bottom: 4, containLabel: true },
        tooltip: Object.assign(base(t).tooltip, { trigger: "item", formatter: (p) => `<b>${p.name}</b><br>${fmt(p.value, unit)}` }),
        xAxis: valAxis(t, unit, { show: false }),
        yAxis: catAxis(t, items.map((x) => x.name), { axisLine: { show: false },
          axisLabel: { color: t.text, width: 140, overflow: "truncate" } }),
        series: [{
          type: "bar", data: items.map((x) => ({ value: x.value, itemStyle: x.name === "Other" ? { color: OTHER[mode()] } : undefined })),
          barMaxWidth: 18, itemStyle: { color: PALETTE[mode()][0], borderRadius: [0, 4, 4, 0] },
          label: { show: true, position: "right", color: t.muted, formatter: (p) => fmt(p.value, unit) },
        }],
      });
    },
    heatmap(d, unit, t) {
      return Object.assign(base(t), {
        grid: { left: 8, right: 12, top: 8, bottom: 40, containLabel: true },
        tooltip: Object.assign(base(t).tooltip, { trigger: "item",
          formatter: (p) => `<b>${d.days[p.value[1]]} ${d.hours[p.value[0]]}:00</b><br>${fmt(p.value[2], unit)}` }),
        xAxis: catAxis(t, d.hours, { splitArea: { show: false }, axisLine: { show: false },
                                      axisLabel: { color: t.muted, interval: 2 } }),
        yAxis: catAxis(t, d.days, { inverse: true, axisLine: { show: false } }),
        visualMap: { min: 0, max: Math.max(d.max, 1), calculable: false, orient: "horizontal", left: "center", bottom: 0,
                     itemWidth: 10, itemHeight: 120, text: [fmt(d.max, unit), "0"], textStyle: { color: t.muted },
                     inRange: { color: SEQ[mode()] } },
        series: [{ type: "heatmap", data: d.data, itemStyle: { borderColor: t.surface, borderWidth: 2, borderRadius: 4 },
                   emphasis: { itemStyle: { borderColor: t.text, borderWidth: 1 } } }],
      });
    },
    flow(d, unit, t) {
      // source resolution (rows) split by delivered resolution
      const n = d.series.length;
      return Object.assign(base(t), {
        legend: legend(t, n),
        grid: { left: 8, right: 12, top: n > 1 ? 34 : 8, bottom: 4, containLabel: true },
        tooltip: Object.assign(base(t).tooltip, { trigger: "axis", axisPointer: { type: "shadow", shadowStyle: { color: "rgba(127,127,127,.08)" } },
                                                  formatter: (p) => tooltipRows(p, unit) }),
        xAxis: valAxis(t, unit),
        yAxis: catAxis(t, d.labels, { inverse: true, axisLine: { show: false }, axisLabel: { color: t.text } }),
        series: d.series.map((s, i) => ({
          name: s.name, type: "bar", stack: "total", data: s.data, barMaxWidth: 18,
          itemStyle: { color: colourFor(s, i), borderColor: t.surface, borderWidth: 1,
                       borderRadius: i === n - 1 ? [0, 4, 4, 0] : 0 },
        })),
      });
    },
  };

  function isEmpty(kind, d) {
    if (kind === "hbar") return !d.items || !d.items.length;
    if (kind === "heatmap") return !d.max;
    return !d.series || !d.series.length || d.series.every((s) => s.data.every((v) => !v));
  }

  function showEmpty(el, msg) {
    el.innerHTML = `<div class="flex h-full items-center justify-center text-sm text-faint">${msg}</div>`;
  }

  async function render(el) {
    if (el.dataset.chartReady) return;
    el.dataset.chartReady = "1";
    const kind = el.dataset.chart, unit = el.dataset.unit || "plays";
    el.innerHTML = `<div class="flex h-full items-center justify-center"><div class="h-1.5 w-24 animate-pulse rounded-full bg-surface-2"></div></div>`;
    let d;
    try {
      const r = await fetch(el.dataset.src, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(r.status);
      d = await r.json();
    } catch (e) {
      showEmpty(el, "Couldn't load this chart");
      return;
    }
    if (isEmpty(kind, d)) {
      showEmpty(el, el.dataset.empty || "No plays in this range");
      return;
    }
    el.innerHTML = "";
    const chart = window.echarts.init(el, null, { renderer: "svg" });
    chart.setOption(build[kind](d, unit, tokens()));
    new ResizeObserver(() => chart.resize()).observe(el);
  }

  function initAll(root) {
    if (!window.echarts) return;
    (root || document).querySelectorAll("[data-chart]").forEach(render);
  }

  document.addEventListener("DOMContentLoaded", () => initAll());
  document.addEventListener("htmx:afterSettle", (e) => initAll(e.target));
  window.BingeCharts = { initAll };
})();
