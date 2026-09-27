# Karaoke de coatl

Karaoke casero. La tele (por HDMI) muestra el video con la letra; los invitados
buscan y encolan canciones desde su celular por el WiFi de la casa. El diseño
completo está en [SPEC.md](SPEC.md).

- **Pantalla**: `http://localhost:8004/pantalla`, en Chromium en modo kiosko.
- **Celulares**: `http://<ip-de-coatl>:8004`. La pantalla de espera muestra un
  QR para unirse al WiFi y otro para abrir la app.

Los videos se bajan con yt-dlp a `media/` y se quedan ahí: esa es la
biblioteca. Las canciones repetidas arrancan al instante y sin internet.
**Nunca se borran solas.**

---

## Instalación

Requisitos del sistema: Python 3.14, `ffmpeg` (yt-dlp lo usa para unir video y
audio) y Chromium para la pantalla:

```bash
sudo apt install ffmpeg
flatpak install flathub org.chromium.Chromium   # o chromium por apt/snap; kiosko.sh detecta cuál hay
```

La app:

```bash
cd ~/karaoke
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
nano .env        # contraseña de admin, SSID y contraseña del WiFi
```

`.env` está en `.gitignore`. La contraseña de admin y la del WiFi viven solo ahí.

### Servicio

```bash
sudo cp deploy/karaoke.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now karaoke
systemctl status karaoke
journalctl -u karaoke -f          # logs
```

### Firewall (solo LAN)

```bash
deploy/ufw.sh                     # = sudo ufw allow from 192.168.0.0/24 to any port 8004 proto tcp
```

La subred por omisión es la de coatl hoy (`192.168.0.0/24`). Si cambia el
router, pásala como argumento: `deploy/ufw.sh 192.168.1.0/24`. **No abrir el
puerto a internet**: la app no tiene cuentas y recibe texto de cualquiera en la
red.

### Aliases

Agregar a `~/.bash_aliases` (con el formato `# nombre: qué hace` para que
aparezcan en `halp`):

```bash
# karaoke: lanza la pantalla de karaoke en el monitor
alias karaoke='~/karaoke/deploy/kiosko.sh'
# karaoke-off: cierra la pantalla de karaoke
alias karaoke-off='~/karaoke/deploy/kiosko.sh stop'
```

---

## En la fiesta

1. `escritorio` (si la sesión gráfica no está arriba).
2. `karaoke`: apaga el protector de pantalla y abre la pantalla completa.
3. Los invitados escanean los dos QR: WiFi y luego la app. Escriben su nombre
   una vez y ya pueden buscar y encolar.
4. Al final, `karaoke-off`.

### Modo admin

En el celular, el enlace discreto **admin** hasta abajo pide la contraseña de
`KARAOKE_ADMIN_PASSWORD`. Da: saltar, pausar/reanudar, volumen ±5, subir/bajar
canciones, quitar cualquiera y vaciar la cola. El servidor verifica el permiso
en cada petición. Si `KARAOKE_ADMIN_PASSWORD` está vacía, el modo admin queda
deshabilitado.

El volumen es el del sistema (`pactl set-sink-volume @DEFAULT_SINK@`), así que
también mueve la bocina bluetooth si es la salida activa.

---

## Cómo está hecho

```
navegador en kiosko (pantalla)  ─┐
celulares de los invitados      ─┼─ WebSocket ─→ FastAPI ─→ SQLite
celular del admin               ─┘                   │
                                                     └─→ yt-dlp → media/
```

- `app/queue.py`: la máquina de estados de la cola
  (`queued → downloading → ready → playing → played`, más `failed` y `removed`).
- `app/downloader.py`: yt-dlp como librería. Baja H.264 + AAC en mp4 y rechaza
  cualquier otro formato antes de descargarlo.
- `app/main.py`: rutas HTTP, WebSocket y el orquestador de descargas (máximo
  dos a la vez, siempre adelantando las siguientes).
- La pantalla es una página más: el estado vive en el servidor y ella solo
  reproduce lo que le dicen. Sus avisos de "terminó" y "falló" solo se aceptan
  desde `localhost`, para que un celular no pueda saltarse canciones.
- La pantalla no depende de ningún CDN (funciona sin internet). La vista del
  celular usa Tailwind y HTMX por CDN.

### Desarrollo

```bash
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8004 --reload
.venv/bin/python -m pytest          # los tests no llaman a YouTube
```

(Con el servicio corriendo, detenlo antes: `sudo systemctl stop karaoke`.)

---

## Problemas comunes

**La pantalla no suena, o sale un aviso rojo de audio bloqueado.** Chromium se
abrió sin `--autoplay-policy=no-user-gesture-required`. Ciérralo y ábrelo con
`karaoke`, que pone la bandera. Si Chromium ya estaba abierto con tu perfil
normal, no pasa nada: el kiosko usa un perfil aparte (`karaoke-kiosko`).

**La pantalla se pone negra a mitad de la fiesta.** `kiosko.sh` corre
`xset s off -dpms`, pero el administrador de energía de XFCE puede volver a
activarlo. Si pasa: Configuración → Administrador de energía → Pantalla, y
desactivar el apagado mientras haya karaoke.

**Las descargas empiezan a fallar todas.** Casi siempre es YouTube cambiando
algo. Actualizar yt-dlp suele bastar:

```bash
.venv/bin/pip install -U yt-dlp && sudo systemctl restart karaoke
```

Si el error menciona un runtime de JavaScript, instala `deno`. El motivo del
fallo se ve en el celular de quien pidió la canción y en
`journalctl -u karaoke`.

**El control de volumen da error 502.** `pactl` no encuentra el audio: el
servicio usa `XDG_RUNTIME_DIR=/run/user/1000`, que existe solo si hay una sesión
de `rober` abierta (la gráfica o ssh). Prueba en una terminal: `pactl info`.

**Los celulares no abren la app.** Revisa que estén en el mismo WiFi (no en
datos), que la regla de ufw cubra su subred (`sudo ufw status`), y que la IP del
QR sea la correcta: se detecta al arrancar el servicio, así que si cambió la IP
de coatl, `sudo systemctl restart karaoke`.

**Respaldo de la biblioteca.** Todo está en `media/` (videos) y
`data/karaoke.db` (títulos, historial, cuántas veces sonó cada una).
