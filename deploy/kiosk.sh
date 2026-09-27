#!/usr/bin/env bash
# Karaoke screen: Chromium in kiosk mode on /screen.
#
#   kiosk.sh          new party: empties the queue and launches the screen
#                     (on the QR codes), with the screensaver off
#   kiosk.sh resume   launches the screen keeping the queue as it was
#   kiosk.sh stop     closes it and turns the screensaver back on
#   kiosk.sh toggle   closes it if it's open, reopens it (keeping the queue) if it isn't
#
# Meant for the `karaoke` and `karaoke-off` aliases, and `toggle` for the
# Ctrl+Alt+K keyboard shortcut, the way out on the machine itself (see README).
set -euo pipefail

URL="${KARAOKE_URL:-http://localhost:8004/screen}"
# Only a plain `kiosk.sh` starts a new party. Reopening with toggle (Ctrl+Alt+K)
# or resume is usually mid-party, after closing it by mistake.
NEW_PARTY=1
[[ "${1:-}" == "toggle" || "${1:-}" == "resume" ]] && NEW_PARTY=0
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# Dedicated Chromium profile: doesn't touch the normal browser, and its name
# on the command line lets us close only this window.
TAG="karaoke-kiosk"
PATTERN="user-data-dir=[^ ]*$TAG"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/$TAG.log"

stop() {
    if pkill -f -- "$PATTERN"; then
        echo "Karaoke screen closed."
    else
        echo "The karaoke screen wasn't open."
    fi
    xset s on +dpms 2>/dev/null || true
}

if [[ "${1:-}" == "stop" ]]; then
    stop
    exit 0
fi

if [[ "${1:-}" == "toggle" ]] && pgrep -f -- "$PATTERN" >/dev/null; then
    stop
    exit 0
fi

if ! xset q >/dev/null 2>&1; then
    echo "No graphical session on $DISPLAY. Start it first with: escritorio" >&2
    exit 1
fi

if pgrep -f -- "$PATTERN" >/dev/null; then
    echo "The screen is already open (karaoke-off to close it)."
    exit 0
fi

# --- Chromium: apt, snap or flatpak -------------------------------------------
CMD=()
PROFILE=""
for bin in chromium chromium-browser google-chrome google-chrome-stable; do
    path="$(command -v "$bin" 2>/dev/null || true)"
    [[ -n "$path" ]] || continue
    CMD=("$path")
    if [[ "$(readlink -f "$path")" == */snap* ]]; then
        # The snap can't write to hidden directories in $HOME.
        PROFILE="$HOME/snap/chromium/common/$TAG"
    else
        PROFILE="${XDG_CONFIG_HOME:-$HOME/.config}/$TAG"
    fi
    break
done
if [[ ${#CMD[@]} -eq 0 ]] && flatpak info org.chromium.Chromium >/dev/null 2>&1; then
    CMD=(flatpak run org.chromium.Chromium)
    # Inside the sandbox only its own data folder is writable.
    PROFILE="$HOME/.var/app/org.chromium.Chromium/$TAG"
fi
if [[ ${#CMD[@]} -eq 0 ]]; then
    echo "Chromium not found. Install it with: flatpak install flathub org.chromium.Chromium" >&2
    exit 1
fi
mkdir -p "$PROFILE" "$(dirname "$LOG")"

# --- the server has to be up --------------------------------------------------
for _ in $(seq 1 30); do
    curl -fsS -o /dev/null --max-time 2 "$URL" && break
    sleep 1
done
if ! curl -fsS -o /dev/null --max-time 2 "$URL"; then
    echo "The server isn't responding at $URL. Check: systemctl status karaoke" >&2
    exit 1
fi

# --- new party: last time's queue goes away -----------------------------------
if [[ "$NEW_PARTY" == 1 ]]; then
    if curl -fsS -o /dev/null --max-time 5 -X POST "${URL%/screen}/party/new"; then
        echo "New party: the queue is empty."
    else
        echo "Couldn't empty the queue (the screen opens anyway)." >&2
    fi
fi

# --- screensaver off, or the screen goes black mid-party ----------------------
xset s off
xset s noblank
xset -dpms

# --- launch -------------------------------------------------------------------
FLAGS=(
    --kiosk
    "--user-data-dir=$PROFILE"
    # Without this the browser blocks video with sound until someone taps.
    --autoplay-policy=no-user-gesture-required
    --no-first-run
    --noerrdialogs
    --disable-infobars
    --disable-session-crashed-bubble
    --disable-features=Translate,MediaRouter
    --check-for-update-interval=31536000
    --password-store=basic
    "$URL"
)

setsid "${CMD[@]}" "${FLAGS[@]}" >"$LOG" 2>&1 </dev/null &
echo "Karaoke screen open on $DISPLAY (log: $LOG)."
