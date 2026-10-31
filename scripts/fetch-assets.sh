#!/usr/bin/env bash
# Download the pinned front-end assets and the Tailwind CLI listed in
# bingestats/static/vendor/VERSIONS, verifying each sha256.
#   scripts/fetch-assets.sh            -> vendor JS into bingestats/static/vendor/
#   scripts/fetch-assets.sh tailwind   -> Tailwind CLI into .cache/tailwindcss
set -euo pipefail
cd "$(dirname "$0")/.."
VERSIONS=bingestats/static/vendor/VERSIONS

fetch() {  # name dest
  local line sha url
  line=$(grep -E "^$1[[:space:]]" "$VERSIONS") || { echo "not in VERSIONS: $1" >&2; exit 1; }
  sha=$(awk '{print $3}' <<<"$line"); url=$(awk '{print $4}' <<<"$line")
  if [[ -f "$2" ]] && echo "$sha  $2" | sha256sum -c --quiet - 2>/dev/null; then return; fi
  mkdir -p "$(dirname "$2")"
  curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors -o "$2.part" "$url"
  echo "$sha  $2.part" | sha256sum -c --quiet - || { echo "checksum mismatch for $1" >&2; rm -f "$2.part"; exit 1; }
  mv "$2.part" "$2"
}

if [[ "${1:-}" == "tailwind" ]]; then
  case "$(uname -m)" in
    x86_64|amd64) arch=x64 ;;
    aarch64|arm64) arch=arm64 ;;
    *) echo "no Tailwind build for $(uname -m)" >&2; exit 1 ;;
  esac
  fetch "tailwindcss-linux-$arch" .cache/tailwindcss
  chmod +x .cache/tailwindcss
else
  fetch htmx.min.js bingestats/static/vendor/htmx.min.js
  fetch alpine.min.js bingestats/static/vendor/alpine.min.js
fi
