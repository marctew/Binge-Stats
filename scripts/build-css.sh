#!/usr/bin/env bash
# Build bingestats/static/css/app.css with the standalone Tailwind CLI (no Node).
#   scripts/build-css.sh          one-off minified build
#   scripts/build-css.sh --watch  rebuild on template changes
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -x .cache/tailwindcss ]] || scripts/fetch-assets.sh tailwind
[[ -f bingestats/static/vendor/htmx.min.js ]] || scripts/fetch-assets.sh
args=(-c bingestats/static/css/tailwind.config.js -i bingestats/static/css/input.css -o bingestats/static/css/app.css)
if [[ "${1:-}" == "--watch" ]]; then
  exec .cache/tailwindcss "${args[@]}" --watch
fi
.cache/tailwindcss "${args[@]}" --minify
