/** Tailwind config for the standalone CLI (no Node). Tokens, colours and fonts come from
 * the Design Uniformity preset in ./design; this file only adds Binge's own paths. */
const TONES = ["accent", "success", "info", "warning", "danger", "play", "stream", "transcode"];
module.exports = {
  presets: [require("./design/tailwind.preset.js")],
  content: [
    "./bingestats/templates/**/*.html",
    "./bingestats/static/js/**/*.js",
  ],
  // stat_tile builds these from a tone name, which the scanner can't see.
  safelist: TONES.flatMap((t) => [`bg-${t}/15`, `text-${t}`]),
  plugins: [],
};
