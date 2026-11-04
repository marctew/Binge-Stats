/** Design Uniformity: shared Tailwind preset (standalone CLI, no Node needed).
 *
 * In the app's tailwind.config.js:
 *   module.exports = {
 *     presets: [require("./design/tailwind.preset.js")],
 *     content: ["./app/templates/**\/*.html", "./app/static/js/**\/*.js"],
 *   };
 * and start the app's input.css with css/tailwind-components.css.
 *
 * Colours are CSS variables, so dark and light share one class vocabulary:
 * bg-surface, text-muted, border-border, bg-accent/15 ...
 * Binge Stats' older names (play, stream, transcode) are kept as aliases.
 */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;
const TONES = ["accent", "success", "info", "warning", "danger"];
module.exports = {
  // stat tiles and badges build these from a tone name, which the scanner can't see
  safelist: TONES.flatMap((t) => [`bg-${t}/15`, `text-${t}`]),
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: v("bg"), surface: v("surface"), "surface-2": v("surface-2"), border: v("border"),
        text: v("text"), muted: v("muted"), faint: v("faint"),
        accent: v("accent"), "accent-2": v("accent-2"), "on-accent": v("on-accent"),
        success: v("success"), info: v("info"), warning: v("warning"), danger: v("danger"),
        play: v("success"), stream: v("info"), transcode: v("warning"),
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      boxShadow: { poster: "0 10px 30px -10px rgb(0 0 0 / 0.6)" },
    },
  },
};
