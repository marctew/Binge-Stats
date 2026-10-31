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

before=$(as_owner git rev-parse --short HEAD)
as_owner git pull --ff-only --quiet
sha=$(as_owner git rev-parse --short HEAD)
if [ "$before" = "$sha" ] && [ "${1:-}" != "--force" ]; then
  echo "Already up to date ($sha). Use --force to rebuild anyway."
  exit 0
fi
echo "Updating $before -> $sha"
as_owner git log --oneline "$before..$sha" | sed 's/^/  /' || true
# sudo on Ubuntu 26.04 ignores -E, so pass the variable through env.
as_root env GIT_SHA="$sha" docker compose up -d --build
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8181/api/v1/health >/dev/null 2>&1; then
    echo "Binge Stats $sha is up."
    exit 0
  fi
  sleep 2
done
echo "Containers started but the health check hasn't passed yet. Check: docker compose logs --tail 50" >&2
exit 1
