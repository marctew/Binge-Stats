#!/usr/bin/env bash
# Pull the latest code and rebuild/restart the containers.
# Safe to run as root or as the repo's owner: git always runs as the owner
# (so the working tree never ends up with root-owned files), docker as root.
#   sudo ln -sf /opt/binge-stats/scripts/update.sh /usr/local/bin/binge-update
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."

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
if [ "$running" = "$sha" ] && [ "${1:-}" != "--force" ]; then
  echo "Already up to date ($sha). Use --force to rebuild anyway."
  exit 0
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
  if curl -fsS http://127.0.0.1:8181/api/v1/health >/dev/null 2>&1; then
    echo "Binge Stats $sha is up."
    exit 0
  fi
  sleep 2
done
echo "Containers started but the health check hasn't passed yet. Check: docker compose logs --tail 50" >&2
exit 1
