/* Design Uniformity: the scroll fade (spec 7.17), with no framework.
 *
 * Give a sideways scroller the class scroll-fade:
 *   <div class="scroll-x scroll-fade" style="display:flex;gap:12px">...cards...</div>
 * This sets data-fade-l / data-fade-r on it while there's more to scroll that way, and the CSS
 * fades that edge. It runs on load, on scroll and on resize, after an htmx swap, and when a row's
 * size changes (images arriving). Call duScrollFade() after changing a row any other way.
 *
 * Load it once per page: <script src="/static/design/scroll-fade.js" defer></script>
 */
(function () {
  var seen = typeof WeakSet === "function" ? new WeakSet() : null;
  var sizes = typeof ResizeObserver === "function"
    ? new ResizeObserver(function (entries) { entries.forEach(function (e) { update(e.target); }); })
    : null;

  function update(el) {
    var max = el.scrollWidth - el.clientWidth;
    el.toggleAttribute("data-fade-l", el.scrollLeft > 2);
    el.toggleAttribute("data-fade-r", max > 2 && el.scrollLeft < max - 2);
  }

  function all() {
    document.querySelectorAll(".scroll-fade").forEach(function (el) {
      update(el);
      if (sizes && seen && !seen.has(el)) { seen.add(el); sizes.observe(el); }
    });
  }

  document.addEventListener("scroll", function (e) {
    if (e.target instanceof Element && e.target.classList.contains("scroll-fade")) update(e.target);
  }, { capture: true, passive: true });
  window.addEventListener("resize", all, { passive: true });
  document.addEventListener("htmx:afterSwap", all);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", all);
  else all();
  window.duScrollFade = all;
})();
