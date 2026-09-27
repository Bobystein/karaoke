#!/usr/bin/env bash
# Pantalla del karaoke: Chromium en modo kiosko sobre /pantalla.
#
#   kiosko.sh          apaga el protector de pantalla y lanza la pantalla
#   kiosko.sh stop     la cierra y reactiva el protector de pantalla
#
# Pensado para los alias `karaoke` y `karaoke-off` (ver README).
set -euo pipefail

URL="${KARAOKE_URL:-http://localhost:8004/pantalla}"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-$HOME/.Xauthority}"

# Perfil propio de Chromium: no toca el navegador normal y su nombre en la
# linea de comando permite cerrar solo esta ventana.
MARCA="karaoke-kiosko"
PATRON="user-data-dir=[^ ]*$MARCA"
LOG="${XDG_STATE_HOME:-$HOME/.local/state}/$MARCA.log"

detener() {
    if pkill -f -- "$PATRON"; then
        echo "Pantalla de karaoke cerrada."
    else
        echo "La pantalla de karaoke no estaba abierta."
    fi
    xset s on +dpms 2>/dev/null || true
}

if [[ "${1:-}" == "stop" ]]; then
    detener
    exit 0
fi

if ! xset q >/dev/null 2>&1; then
    echo "No hay sesion grafica en $DISPLAY. Levantala primero con: escritorio" >&2
    exit 1
fi

if pgrep -f -- "$PATRON" >/dev/null; then
    echo "La pantalla ya esta abierta (karaoke-off para cerrarla)."
    exit 0
fi

# --- Chromium: apt, snap o flatpak ------------------------------------------
CMD=()
PERFIL=""
for bin in chromium chromium-browser google-chrome google-chrome-stable; do
    ruta="$(command -v "$bin" 2>/dev/null || true)"
    [[ -n "$ruta" ]] || continue
    CMD=("$ruta")
    if [[ "$(readlink -f "$ruta")" == */snap* ]]; then
        # El snap no puede escribir en directorios ocultos del $HOME.
        PERFIL="$HOME/snap/chromium/common/$MARCA"
    else
        PERFIL="${XDG_CONFIG_HOME:-$HOME/.config}/$MARCA"
    fi
    break
done
if [[ ${#CMD[@]} -eq 0 ]] && flatpak info org.chromium.Chromium >/dev/null 2>&1; then
    CMD=(flatpak run org.chromium.Chromium)
    # Dentro del sandbox solo su carpeta de datos es escribible.
    PERFIL="$HOME/.var/app/org.chromium.Chromium/$MARCA"
fi
if [[ ${#CMD[@]} -eq 0 ]]; then
    echo "No encontre Chromium. Instalalo con: flatpak install flathub org.chromium.Chromium" >&2
    exit 1
fi
mkdir -p "$PERFIL" "$(dirname "$LOG")"

# --- el servidor tiene que estar arriba ---------------------------------------
for _ in $(seq 1 30); do
    curl -fsS -o /dev/null --max-time 2 "$URL" && break
    sleep 1
done
if ! curl -fsS -o /dev/null --max-time 2 "$URL"; then
    echo "El servidor no responde en $URL. Revisa: systemctl status karaoke" >&2
    exit 1
fi

# --- protector de pantalla: fuera, o a mitad de la fiesta se pone negro -------
xset s off
xset s noblank
xset -dpms

# --- lanzar -----------------------------------------------------------------
FLAGS=(
    --kiosk
    "--user-data-dir=$PERFIL"
    # Sin esto el navegador bloquea el video con sonido hasta que alguien toque.
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
echo "Pantalla de karaoke abierta en $DISPLAY (log: $LOG)."
