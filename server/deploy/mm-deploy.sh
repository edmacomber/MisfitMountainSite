#!/bin/bash
# Pull the latest Misfit Monsters repo and restart the leaderboard
# service if server code actually changed. Called by the mm-deploy
# systemd timer every 60 seconds.
#
# Idempotent. Quiet when nothing changed.

set -euo pipefail

REPO_DIR="/opt/MisfitMountainSite"
WATCH_PATH="server/"
SERVICE="mm-leaderboard"
SUDO_RESTART="/usr/bin/sudo /bin/systemctl restart ${SERVICE}"

cd "${REPO_DIR}"

# Snapshot the pre-pull commit hash of the watch path.
BEFORE="$(git rev-parse HEAD:${WATCH_PATH} 2>/dev/null || echo initial)"

# Fetch + fast-forward. Force to origin/main so a failed pull doesn't
# leave the tree in a half-merged state.
git fetch --quiet origin main
git reset --hard --quiet origin/main

AFTER="$(git rev-parse HEAD:${WATCH_PATH})"

if [ "${BEFORE}" != "${AFTER}" ]; then
  echo "[$(date -u +%FT%TZ)] server/ changed ${BEFORE:0:12} -> ${AFTER:0:12} — restarting ${SERVICE}"
  ${SUDO_RESTART}
else
  # Uncomment for debugging:
  # echo "[$(date -u +%FT%TZ)] no server/ changes"
  :
fi
