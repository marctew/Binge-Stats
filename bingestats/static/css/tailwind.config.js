/** Tailwind config for the standalone CLI (no Node). Colours are CSS
 * variables so dark and light themes share one class vocabulary. */
const v = (name) => `rgb(var(--${name}) / <alpha-value>)`;
module.exports = {
  content: [
    "./bingestats/templates/**/*.html",
    "./bingestats/static/js/**/*.js",
  ],
  // stat_tile builds these from a tone name, which the scanner can't see.
  safelist: ["accent", "play", "stream", "transcode", "danger"].flatMap((t) => [`bg-${t}/15`, `text-${t}`]),
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        bg: v("bg"), surface: v("surface"), "surface-2": v("surface-2"), border: v("border"),
        text: v("text"), muted: v("muted"), faint: v("faint"),
        accent: v("accent"), "accent-2": v("accent-2"),
        play: v("play"), stream: v("stream"), transcode: v("transcode"), danger: v("danger"),
      },
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      boxShadow: { poster: "0 10px 30px -10px rgb(0 0 0 / 0.6)" },
    },
  },
  plugins: [],
};
