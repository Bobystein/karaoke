#!/usr/bin/env bash
# Updates yt-dlp in the project's venv and restarts the karaoke service so it
# picks up the new version. When downloads start failing all at once, it's
# almost always YouTube changing something, and this is the fix.
#
#   scripts/update-ytdlp.sh           # update, restart only if it changed
#   scripts/update-ytdlp.sh --force   # restart even if it was already current
set -euo pipefail

cd "$(dirname "$0")/.."
PY=.venv/bin/python
SERVICE=karaoke

version() { "$PY" -c 'from yt_dlp.version import __version__; print(__version__)'; }

before=$(version)
"$PY" -m pip install --upgrade --quiet yt-dlp
after=$(version)

if [[ "$before" == "$after" ]]; then
    echo "yt-dlp was already up to date ($after)."
    [[ "${1:-}" == "--force" ]] || exit 0
else
    echo "yt-dlp updated: $before -> $after"
fi

if systemctl list-unit-files --quiet "$SERVICE.service" &>/dev/null; then
    echo "Restarting $SERVICE (downloads in progress go back to the queue)..."
    sudo systemctl restart "$SERVICE"
    systemctl --no-pager --lines=0 status "$SERVICE" | head -n 3
else
    echo "No $SERVICE service installed; restart the app yourself."
fi
