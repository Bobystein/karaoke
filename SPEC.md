# SPEC: Sistema de karaoke para coatl

Karaoke casero. La pantalla (monitor o tele por HDMI) reproduce el video con la
letra; los invitados buscan y encolan canciones desde su celular por el WiFi de
la casa.

---

## 1. Decision de arquitectura

La pantalla NO es un reproductor aparte controlado por IPC. Es **una pagina web
mas de esta misma app**, abierta en Chromium en modo kiosko a pantalla completa.

El motivo: los requisitos de la pantalla (barra deslizante con las siguientes
dos canciones, pantalla de espera con codigos QR, transiciones entre canciones)
son trivialmente HTML y CSS, y serian dolorosos de dibujar encima de mpv. Con
esta decision hay un solo codigo, un solo estado en el servidor, y la pantalla
es un cliente igual que los celulares, solo que con otra vista.

El video se descarga con `yt-dlp` a disco local, el servidor lo sirve como
archivo estatico, y la pagina lo reproduce en una etiqueta `<video>`.

```
navegador en kiosko (pantalla)  ─┐
celulares de los invitados      ─┼─ WebSocket ─→ FastAPI ─→ SQLite
celular del admin               ─┘                   │
                                                     └─→ yt-dlp → media/
```

Corre en `/home/rober/karaoke`, puerto 8004, abierto al WiFi local (no solo
Tailscale).

---

## 2. Stack fijo

Python 3.14, FastAPI, Uvicorn, Jinja2, HTMX y Tailwind por CDN, `websockets`
(via FastAPI), SQLite estandar, `yt-dlp`, `segno` (para los QR, es Python puro
y escribe SVG). pytest para tests. Sin ORM, sin Node, sin build step.

---

## 3. Estructura

```
~/karaoke/
├── app/
│   ├── main.py          # rutas HTTP y WebSocket
│   ├── queue.py         # estado de la cola, la maquina de estados
│   ├── downloader.py    # envoltura de yt-dlp, busqueda y descarga
│   ├── db.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── pantalla.html    # la vista de la tele
│   │   ├── movil.html       # la vista del celular
│   │   └── partials/
│   └── static/
├── media/               # videos descargados (cache, fuera de git)
├── data/karaoke.db
├── schema.sql
├── deploy/              # unit de systemd, regla de ufw
├── requirements.txt
└── SPEC.md
```

---

## 4. Base de datos

```sql
-- Quien esta en la fiesta. Sin contrasena, solo un nombre.
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,        -- uuid4, va en cookie
    name        TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache de videos ya descargados. La clave es el id de YouTube.
CREATE TABLE IF NOT EXISTS songs (
    video_id     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    channel      TEXT,
    duration_s   INTEGER,
    file_path    TEXT,                   -- NULL mientras no se ha descargado
    thumb_url    TEXT,
    downloaded_at TEXT,
    play_count   INTEGER NOT NULL DEFAULT 0
);

-- La cola. Un renglon por peticion, aunque la cancion se repita.
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES songs(video_id),
    session_id    TEXT REFERENCES sessions(id),
    requested_by  TEXT NOT NULL,          -- copia del nombre, por si se borra la sesion
    state         TEXT NOT NULL           -- ver maquina de estados abajo
                  CHECK (state IN ('queued','downloading','ready','playing','played','failed','removed')),
    position      INTEGER NOT NULL,       -- orden en la cola, editable por el admin
    error         TEXT,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    played_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_state ON queue(state, position);
```

`requested_by` se guarda duplicado a proposito: el nombre que aparece en
pantalla no debe depender de que la sesion siga viva.

---

## 5. Maquina de estados de la cola

```
queued ──→ downloading ──→ ready ──→ playing ──→ played
              │
              └──→ failed
```

- **queued**: recien agregada. Si la cancion ya esta en cache (`songs.file_path`
  no es NULL y el archivo existe), pasa directo a `ready` sin descargar.
- **downloading**: `yt-dlp` trabajando en segundo plano. La UI muestra progreso.
- **ready**: archivo en disco, lista para sonar.
- **playing**: solo puede haber UNA en este estado a la vez. Invariante que los
  tests deben verificar.
- **played**: termino. Se queda en la tabla para el historial.
- **failed**: la descarga fallo. Guarda el motivo en `error`, se muestra al que
  la pidio, y la cola sigue con la siguiente.
- **removed**: la quito el admin o quien la pidio. No se borra el renglon.

El servidor descarga **con anticipacion**: mientras suena una cancion, ya esta
bajando las siguientes dos. Asi nunca hay espera entre canciones. Maximo dos
descargas simultaneas para no saturar la red.

**Regla de avance**: cuando la cancion actual termina, se busca la primera en
`ready` por `position`. Si la siguiente todavia esta en `downloading`, se salta
a la que si este lista y la que faltaba conserva su lugar para cuando baje. Si
no hay ninguna lista, la pantalla vuelve al modo espera.

---

## 6. Busqueda y descarga

`downloader.py` envuelve `yt-dlp` como libreria de Python, no por subproceso.

### Busqueda

`ytsearch12:<consulta> karaoke` con `extract_flat=True` para que sea rapido (no
resuelve cada video, solo lista). La palabra "karaoke" se agrega
automaticamente a la consulta, con una casilla en la UI para no agregarla.

Devuelve id, titulo, canal, duracion y miniatura. Filtrar resultados de mas de
15 minutos (suelen ser recopilaciones, no canciones).

### Descarga

Formato: preferir mp4 con H.264 y audio AAC, que es lo que el navegador
reproduce nativamente y la UHD 620 decodifica por hardware. Algo como
`bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[height<=1080][ext=mp4]`.
Si lo que baja no es reproducible en navegador, es un fallo real: reportar
`failed`, no dejarlo callado.

Guardar en `media/<video_id>.mp4`. Antes de descargar, verificar si ya existe.

**El cache es la funcion mas valiosa del sistema**: despues de un par de
fiestas ya hay biblioteca propia, las canciones repetidas arrancan al instante y
deja de depender de internet. Nunca borrar automaticamente.

### Seguridad

La consulta del usuario NUNCA se interpola en una linea de comando. yt-dlp se
usa como libreria con la consulta como parametro. Esto importa porque la app
esta abierta al WiFi de la casa y recibe texto de gente cualquiera.

---

## 7. La pantalla (`/pantalla`)

Pantalla completa, fondo oscuro, pensada para verse de lejos. Se conecta por
WebSocket y reacciona a lo que le mande el servidor.

### Modo espera

Cuando no hay nada sonando:

- Titulo grande: "Karaoke".
- **Dos codigos QR lado a lado**, generados con `segno` como SVG:
  - Izquierda: conectarse al WiFi. Usa el formato estandar
    `WIFI:T:WPA;S:<ssid>;P:<password>;;`, que Android e iOS reconocen para unirse
    con un escaneo. SSID y contrasena salen de variables de entorno, nunca del
    codigo.
  - Derecha: abrir la app, `http://<ip_lan>:8004`. La IP se detecta sola al
    arrancar, no se escribe a mano.
- Debajo, instrucciones de dos renglones.
- Si hay canciones en cola pero ninguna lista todavia, mostrar "descargando..."
  con cuales vienen.

### Modo reproduccion

- El video ocupa toda la pantalla, sin controles nativos.
- **Barra superior deslizante** que aparece y se oculta sola: muestra lo que
  suena (titulo y quien la pidio) y las siguientes DOS de la cola. Visible los
  primeros 10 segundos de cada cancion, luego se desliza hacia arriba, y vuelve
  a aparecer en los ultimos 15 segundos. Tambien aparece si la cola cambia.
- Tres segundos antes de terminar, aviso grande de quien sigue.
- Nada mas en pantalla. La cola completa se ve en el celular, no aqui.

### Detalles tecnicos que hay que resolver

- **Autoplay**: los navegadores bloquean reproduccion automatica con sonido.
  Chromium en kiosko necesita `--autoplay-policy=no-user-gesture-required`. Va
  en el script de arranque, documentado en el README.
- **Protector de pantalla**: apagarlo con `xset s off -dpms` antes de lanzar el
  kiosko, o a mitad de la fiesta se pone negro.
- **Fin de cancion**: el evento `ended` del `<video>` avisa al servidor por
  WebSocket, y el servidor decide que sigue. La pantalla nunca decide sola:
  el estado vive en el servidor.
- **Reconexion**: si se cae el WebSocket, reintentar cada 2 segundos sin
  recargar la pagina, para no interrumpir el video.

---

## 8. La vista del celular (`/`)

### Entrada

La primera vez pide solo un nombre. Se guarda en `sessions` y se pone una cookie
de larga duracion, para que al volver no lo pida otra vez. Sin contrasena.

### Vista principal

1. **Buscador** arriba, grande. Escribe, toca buscar, salen resultados con
   miniatura, titulo, canal y duracion. Un boton para agregar.
2. **La cola completa**, en orden, con: numero, titulo, quien la pidio y estado
   (descargando con progreso, lista, o sonando). La que suena, resaltada.
3. Cada quien puede quitar sus propias canciones, no las de los demas.
4. Actualizacion en vivo por el mismo WebSocket: si alguien agrega algo, todos
   lo ven sin recargar.

### Modo admin

Un enlace discreto abajo, "admin". Pide contrasena (variable de entorno
`KARAOKE_ADMIN_PASSWORD`, nunca en el codigo ni en git). Al acertar, marca
`is_admin` en la sesion y aparecen los controles:

- Saltar a la siguiente cancion.
- Pausar y reanudar.
- Reordenar la cola (subir y bajar, o arrastrar).
- Quitar cualquier cancion, no solo las propias.
- Agregar canciones igual que todos.
- Vaciar la cola.
- Control de volumen del sistema.

Los controles de admin son los unicos endpoints que verifican permiso. Verificar
**en el servidor**, en cada peticion, no solo escondiendo botones en la UI.

---

## 9. API

### HTTP

- `GET /` → vista movil (o el formulario de nombre si no hay sesion)
- `POST /session` → guarda el nombre, pone la cookie
- `GET /pantalla` → vista de la tele
- `GET /buscar?q=...&karaoke=1` → resultados de busqueda (fragmento HTMX)
- `POST /queue` → agrega `video_id` a la cola
- `DELETE /queue/{id}` → quita (propia, o cualquiera si es admin)
- `GET /media/{video_id}.mp4` → sirve el archivo con soporte de Range, que el
  navegador necesita para buscar dentro del video
- `POST /admin/login` → verifica contrasena
- `POST /admin/skip`, `/admin/pause`, `/admin/reorder`, `/admin/clear`
- `POST /admin/volume` → llama a `pactl set-sink-volume @DEFAULT_SINK@`

### WebSocket `/ws`

Mensajes del servidor a los clientes:

- `state`: estado completo (lo que suena, la cola, y el modo de la pantalla).
  Se manda al conectar y cuando cambia algo.
- `progress`: avance de descarga de una cancion.

Mensajes de la pantalla al servidor:

- `ended`: termino la cancion actual.
- `error`: el video no se pudo reproducir. El servidor lo marca `failed` y
  avanza.

Todo cambio de estado se transmite a todos los clientes conectados.

---

## 10. Despliegue

`deploy/karaoke.service`: usuario `rober`, puerto 8004,
`EnvironmentFile=/home/rober/karaoke/.env` con la contrasena de admin y los
datos del WiFi para el QR. El `.env` va en `.gitignore`.

`deploy/kiosko.sh`: apaga el protector de pantalla y lanza Chromium en kiosko
apuntando a `/pantalla`, con la bandera de autoplay. Pensado para llamarse
desde un alias.

Regla de firewall, documentada en el README:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8004 proto tcp
```

Ese rango hay que ajustarlo a la subred real de la casa. Solo LAN: nada de
abrirlo a internet.

Aliases sugeridos para `~/.bash_aliases` (el archivo ya existe y tiene su
funcion `halp`, hay que respetar el formato de comentarios `# nombre: que hace`
para que aparezcan solos en la ayuda):

```bash
# karaoke: lanza la pantalla de karaoke en el monitor
# karaoke-off: cierra la pantalla de karaoke
```

---

## 11. Tests

- Maquina de estados: nunca dos canciones en `playing` a la vez.
- Avance: si la siguiente esta `downloading`, se salta a la primera `ready` y la
  saltada conserva su posicion.
- Cola vacia o sin nada listo: la pantalla vuelve a modo espera.
- Cache: agregar una cancion ya descargada la deja en `ready` sin descargar.
- Permisos: un usuario normal no puede quitar canciones ajenas ni llamar
  endpoints de admin. Probar llamando el endpoint directo, no solo la UI.
- Busqueda: una consulta con comillas, punto y coma y acentos no rompe nada.
- Generacion del QR de WiFi con SSID y contrasena que traigan caracteres que el
  formato requiere escapar.

Los tests no deben llamar a YouTube: simular las respuestas de yt-dlp.

---

## 12. Fuera de alcance

Sin puntuaciones, sin efectos de voz, sin microfono conectado a la compu, sin
turnos rotativos por persona, sin cuentas permanentes, sin listas guardadas.
Si el sistema se usa y queda gusto, se agregan despues.