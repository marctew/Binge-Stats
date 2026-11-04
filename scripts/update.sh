#!/usr/bin/env bash
# Pull the latest code and rebuild/restart the containers (homelab app standard, section 6).
#   scripts/update.sh            pull; rebuild and restart if what's running isn't HEAD
#   scripts/update.sh --force    rebuild and restart anyway, even if a job is running
#   scripts/update.sh --if-idle  as normal, but if an import or backup is running, leave it for the
#                                next update and still succeed (the design updater uses this)
# --pull is accepted too: Binge always pulls, because the published image is built from GitHub.
# Safe to run as root or as the repo's owner: git always runs as the owner
# (so the working tree never ends up with root-owned files), docker as root.
#   sudo ln -sf /opt/binge-stats/scripts/update.sh /usr/local/bin/binge-update
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
FORCE=0 IDLE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --if-idle) IDLE=1 ;;
    --pull) ;;
    *) echo "Unknown option: $a" >&2; exit 2 ;;
  esac
done
HEALTH=http://127.0.0.1:8181/healthz
busy() {   # /healthz's busy list, or nothing (Python, not curl, as the standard says)
  python3 - "$HEALTH" <<'EOF' 2>/dev/null || true
import json, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
    print(", ".join(json.load(r).get("busy", [])))
EOF
}

owner=$(stat -c %U .)
as_owner() {
  if [ "$(id -un)" = "$owner" ]; then "$@"; else sudo -u "$owner" -- "$@"; fi
}
as_root() {
  if [ "$(id -u)" = 0 ]; then "$@"; else sudo -- "$@"; fi
}

# Compare with what's actually running, not with HEAD before the pull, so a
# build that failed half-way (pulled but not deployed) is retried next time.
running=$(as_root docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' binge-stats-web-1 2>/dev/null \
          | sed -n 's/^GIT_SHA=//p')
as_owner git pull --ff-only --quiet
sha=$(as_owner git rev-parse --short HEAD)
if [ "$running" = "$sha" ] && [ "$FORCE" = 0 ]; then
  echo "Already up to date ($sha). Use --force to rebuild anyway."
  exit 0
fi
jobs=$(busy)
if [ -n "$jobs" ] && [ "$FORCE" = 0 ]; then
  if [ "$IDLE" = 1 ]; then
    echo "Busy ($jobs), so not rebuilding now. The next update picks it up."
    exit 0
  fi
  echo "Busy ($jobs). Try again when it's finished, or use --force." >&2
  exit 1
fi
echo "Updating ${running:-unknown} -> $sha"
if [ -n "$running" ] && as_owner git cat-file -e "$running" 2>/dev/null; then
  as_owner git log --oneline "$running..$sha" | sed 's/^/  /' || true
fi
# sudo on Ubuntu 26.04 ignores -E, so pass the variable through env.
# Docker Hub and the CDNs occasionally drop a connection: retry the build.
for attempt in 1 2 3; do
  if as_root env GIT_SHA="$sha" docker compose build; then break; fi
  [ "$attempt" = 3 ] && { echo "Build failed three times; nothing was changed." >&2; exit 1; }
  echo "Build failed (attempt $attempt), retrying in 10s..." >&2
  sleep 10
done
as_root env GIT_SHA="$sha" docker compose up -d
for _ in $(seq 1 20); do
  if python3 -c "import json,sys,urllib.request; sys.exit(not json.load(urllib.request.urlopen(sys.argv[1], timeout=3)).get('ok'))" "$HEALTH" 2>/dev/null; then
    echo "Binge Stats $sha is up."
    exit 0
  fi
  sleep 2
done
echo "Containers started but the health check hasn't passed yet. Check: docker compose logs --tail 50" >&2
exit 1
