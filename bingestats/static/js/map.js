// Location map: <div data-map='[{"label":..,"lat":..,"lon":..,"plays":..}]'></div>
// Leaflet with CARTO basemap tiles (dark or light to match the theme).
(function () {
  "use strict";
  function init(el) {
    if (el.dataset.mapReady || !window.L) return;
    el.dataset.mapReady = "1";
    const pts = JSON.parse(el.dataset.map || "[]").filter((p) => p.lat != null && p.lon != null);
    const dark = document.documentElement.dataset.theme !== "light";
    const map = L.map(el, { zoomControl: true, attributionControl: true, scrollWheelZoom: false, worldCopyJump: true });
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, {
      maxZoom: 12, subdomains: "abcd",
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
    }).addTo(map);
    if (!pts.length) { map.setView([54, -2], 4); return; }
    const max = Math.max(...pts.map((p) => p.plays));
    const markers = pts.map((p) => L.circleMarker([p.lat, p.lon], {
      radius: 5 + 10 * Math.sqrt(p.plays / max), color: dark ? "#11141d" : "#ffffff", weight: 2,
      fillColor: dark ? "#3987e5" : "#2a78d6", fillOpacity: 0.85,
    }).bindTooltip(`<b>${p.label}</b><br>${p.plays.toLocaleString("en-GB")} play${p.plays === 1 ? "" : "s"}`).addTo(map));
    const group = L.featureGroup(markers);
    map.fitBounds(group.getBounds().pad(0.4), { maxZoom: 6 });
    new ResizeObserver(() => map.invalidateSize()).observe(el);
  }
  document.addEventListener("DOMContentLoaded", () => document.querySelectorAll("[data-map]").forEach(init));
})();
