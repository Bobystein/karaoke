# SPEC: Karaoke system for coatl

Home karaoke. The screen (monitor or TV over HDMI) plays the video with the
lyrics; guests search and queue songs from their phones over the home WiFi.

---

## 1. Architecture decision

The screen is NOT a separate player controlled over IPC. It is **just another
web page of this same app**, opened in Chromium in full-screen kiosk mode.

The reason: the screen's requirements (a sliding bar with the next two songs,
a waiting screen with QR codes, transitions between songs) are trivial in HTML
and CSS, and would be painful to draw on top of mpv. With this decision there
is a single codebase, a single state on the server, and the screen is a client
just like the phones, only with a different view.

The video is downloaded with `yt-dlp` to local disk, the server serves it as a
static file, and the page plays it in a `<video>` tag.

```
kiosk browser (screen)   ─┐
guests' phones           ─┼─ WebSocket ─→ FastAPI ─→ SQLite
admin's phone            ─┘                   │
                                              └─→ yt-dlp → media/
```

Runs in `/home/rober/karaoke`, port 8004, open to the local WiFi (not only
Tailscale).

---

## 2. Fixed stack

Python 3.14, FastAPI, Uvicorn, Jinja2, HTMX and Tailwind from a CDN,
`websockets` (via FastAPI), standard SQLite, `yt-dlp`, `segno` (for the QR
codes; it's pure Python and writes SVG). pytest for tests. No ORM, no Node, no
build step.

---

## 3. Structure

```
~/karaoke/
├── app/
│   ├── main.py          # HTTP and WebSocket routes
│   ├── queue.py         # queue state, the state machine
│   ├── downloader.py    # yt-dlp wrapper, search and download
│   ├── db.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── screen.html      # the TV view
│   │   ├── mobile.html      # the phone view
│   │   └── partials/
│   └── static/
├── media/               # downloaded videos by default (KARAOKE_MEDIA_DIR; cache, outside git)
├── data/karaoke.db
├── schema.sql
├── deploy/              # systemd unit, ufw rule, kiosk script
├── requirements.txt
└── SPEC.md
```

---

## 4. Database

```sql
-- Who's at the party. No password, just a name.
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,        -- uuid4, stored in a cookie
    name        TEXT NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cache of downloaded videos. The key is the YouTube id.
CREATE TABLE IF NOT EXISTS songs (
    video_id     TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    channel      TEXT,
    duration_s   INTEGER,
    file_path    TEXT,                   -- NULL until downloaded
    thumb_url    TEXT,
    downloaded_at TEXT,
    last_used_at TEXT,                   -- last requested or played; the library evicts the oldest
    play_count   INTEGER NOT NULL DEFAULT 0
);

-- The queue. One row per request, even if the song repeats.
CREATE TABLE IF NOT EXISTS queue (
    id            INTEGER PRIMARY KEY,
    video_id      TEXT NOT NULL REFERENCES songs(video_id),
    session_id    TEXT REFERENCES sessions(id),
    requested_by  TEXT NOT NULL,          -- copy of the name, in case the session is deleted
    state         TEXT NOT NULL           -- see the state machine below
                  CHECK (state IN ('queued','downloading','ready','playing','played','failed','removed')),
    position      INTEGER NOT NULL,       -- order in the queue, editable by the admin
    error         TEXT,
    query         TEXT,                   -- what was searched, for "try another version"
    search_mode   TEXT,                   -- karaoke / lyrics / youtube
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    played_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_state ON queue(state, position);
```

`requested_by` is duplicated on purpose: the name shown on screen must not
depend on the session still being alive.

---

## 5. Queue state machine

```
queued ──→ downloading ──→ ready ──→ playing ──→ played
              │
              └──→ failed
```

- **queued**: just added. If the song is already cached (`songs.file_path` is
  not NULL and the file exists), it goes straight to `ready` without
  downloading.
- **downloading**: `yt-dlp` working in the background. The UI shows progress.
- **ready**: file on disk, ready to play.
- **playing**: only ONE can be in this state at a time. An invariant the tests
  must check.
- **played**: finished. Stays in the table as history.
- **failed**: the download failed. The reason is stored in `error`, shown to
  whoever requested it, and the queue moves on to the next one.
- **removed**: removed by the admin or by whoever requested it. The row is not
  deleted.

The server downloads **ahead of time**: while a song plays, it's already
downloading the next two. That way there's never a wait between songs. At most
two simultaneous downloads so the network isn't saturated.

**Advance rule**: when the current song ends, look for the first `ready` one by
`position`. If the next one is still `downloading`, skip to the one that is
ready; the missing one keeps its spot for when it finishes. If none is ready,
the screen goes back to waiting mode.

---

## 6. Search and download

`downloader.py` wraps `yt-dlp` as a Python library, not as a subprocess.

### Search

`ytsearch12:<query> karaoke` with `extract_flat=True` so it's fast (it doesn't
resolve each video, it only lists them). Three always-visible chips under the
search box pick the mode, remembered for the browser session:

- **Karaoke** (default): appends "karaoke" to the query.
- **Lyrics**: appends "lyrics".
- **YouTube**: the query as typed.

If the text is a YouTube link (`youtube.com/watch?v=<id>`, `youtu.be/<id>`) or a
bare 11-character id, it isn't searched: the id is extracted and validated,
and only `https://www.youtube.com/watch?v=<id>` built by us reaches yt-dlp,
which resolves it as a single result ready to queue. A bare 11-character word
that turns out not to be a video falls back to a normal search.

Returns id, title, channel, duration and thumbnail. Filter out results longer
than 15 minutes (usually compilations, not songs).

### Download

Format: prefer mp4 with H.264 and AAC audio, which is what the browser plays
natively and the UHD 620 decodes in hardware. Something like
`bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[height<=1080][ext=mp4]`.
If what comes down isn't playable in the browser, that's a real failure:
report `failed`, don't swallow it.

Errors are translated to a short, actionable message before being
stored in `queue.error` (bot check, video unavailable, geo restriction, no
connection, no playable format, or a generic fallback). The UI never shows
yt-dlp's text or a traceback; that goes to the log. A failed request keeps the
query and mode it came from, so the phone can offer "Try another version".

If `KARAOKE_COOKIES_FILE` is set and the file exists, it's passed to yt-dlp as
`cookiefile`.

Save to `<KARAOKE_MEDIA_DIR>/<video_id>.mp4`. Before downloading, check whether it already
exists.

**The cache is the most valuable feature of the system**: after a couple of
parties there's a library of our own, repeated songs start instantly and it
stops depending on the internet.

The library has a size limit (`KARAOKE_MEDIA_MAX_GB`, 150 GB by default, 0 =
none), enforced by `app/library.py` after each download and at startup. Past
it, songs are deleted by `songs.last_used_at` (last requested or played),
oldest first, until it fits. Never deleted: files of active requests (queued,
downloading, ready, playing) and files in the folder that aren't songs in the
database. The `songs` row stays with `file_path = NULL`, so a new request just
downloads it again.

### Security

The user's query is NEVER interpolated into a command line. yt-dlp is used as
a library with the query as a parameter. This matters because the app is open
to the home WiFi and receives text from anyone.

---

## 7. The screen (`/screen`)

Full screen, dark background, designed to be read from a distance. Connects
over WebSocket and reacts to whatever the server sends.

### Waiting mode

When nothing is playing:

- Big title: "Karaoke".
- **Two QR codes side by side**, generated with `segno` as SVG:
  - Left: join the WiFi. Uses the standard format
    `WIFI:T:WPA;S:<ssid>;P:<password>;;`, which Android and iOS recognize to
    join with one scan. SSID and password come from environment variables,
    never from the code.
  - Right: open the app, `http://<lan_ip>:8004`. The IP is detected
    automatically at startup, not typed in by hand.
- Below, two lines of instructions.
- If there are songs in the queue but none ready yet, show "downloading..."
  with the upcoming ones.

### Playing mode

- The video fills the screen, with no native controls.
- **Sliding top bar** that shows and hides by itself: shows what's playing
  (title and who requested it), the next TWO in the queue, and a small QR code
  to open the app so anyone can add a song mid-song. Visible for the first 10
  seconds of each song, then slides up, and comes back for the last 15
  seconds. It also appears when the queue changes.
- Three seconds before the end, a big notice of who's up next.
- **While paused**, a dark overlay with "PAUSED" and the same two QR codes as
  the waiting screen, for anyone who wants to join the queue or lost the link.
- Nothing else on screen. The full queue is on the phone, not here.

### Technical details to solve

- **Autoplay**: browsers block automatic playback with sound. Chromium in
  kiosk mode needs `--autoplay-policy=no-user-gesture-required`. It goes in the
  launch script, documented in the README.
- **Screensaver**: turn it off with `xset s off -dpms` before launching the
  kiosk, or the screen goes black mid-party.
- **End of song**: the `<video>`'s `ended` event notifies the server over
  WebSocket, and the server decides what's next. The screen never decides on
  its own: state lives on the server.
- **Reconnection**: if the WebSocket drops, retry every 2 seconds without
  reloading the page, so the video isn't interrupted.

---

## 8. The phone view (`/`)

### Sign-in

The first time it only asks for a name. It's stored in `sessions` and a
long-lived cookie is set, so it isn't asked again on return. No password.

### Main view

1. **Search** at the top, big. Type, tap search, results come up with
   thumbnail, title, channel and duration. A button to add.
2. **The full queue**, in order, with: number, title, who requested it and
   status (downloading with progress, ready, or playing). The playing one is
   highlighted.
3. Everyone can remove their own songs, not other people's.
4. Live updates over the same WebSocket: if someone adds something, everyone
   sees it without reloading.

### Admin mode

A discreet link at the bottom, "admin". Asks for a password (environment
variable `KARAOKE_ADMIN_PASSWORD`, plus an optional second one in
`KARAOKE_ADMIN_PASSWORD_2`; either is accepted. Never in the code or in git). When correct,
it sets `is_admin` on the session and the controls appear:

- Skip to the next song.
- Pause and resume.
- Reorder the queue (move up and down, or drag).
- Remove any song, not just your own.
- Add songs like everyone else.
- Clear the queue.
- Pin the app's QR code in the screen's top-left corner while songs play
  (`POST /admin/qr`). The **Q** key on the screen does the same, sent over the
  WebSocket and only accepted from the karaoke machine. Stored in the
  `settings` table, so it survives restarts; it's part of the `state` message
  (`qr_pinned`).
- System volume control.

The admin controls are the only endpoints that check permission. Check **on
the server**, on every request, not just by hiding buttons in the UI.

---

## 9. API

### HTTP

- `GET /` → phone view (or the name form if there's no session)
- `POST /session` → stores the name, sets the cookie
- `POST /session/name` → changes your name (also on your requests still in the queue)
- `GET /screen` → TV view
- `POST /party/new` → empties the queue (including what's playing) for a new
  party; only from the karaoke machine itself
- `GET /search?q=...&mode=karaoke|lyrics|youtube[&failed=<video_id>]` → search results (HTMX fragment)
- `POST /queue` → adds `video_id` to the queue (plus the `q` and `mode` it was found with)
- `DELETE /queue/{id}` → removes (your own, or any if admin)
- `GET /media/{video_id}.mp4` → serves the file with Range support, which the
  browser needs to seek within the video
- `POST /admin/login` → checks the password
- `POST /admin/skip`, `/admin/pause`, `/admin/reorder`, `/admin/clear`
- `POST /admin/volume` → calls `pactl set-sink-volume @DEFAULT_SINK@`

### WebSocket `/ws`

Messages from the server to clients:

- `state`: full state (what's playing, the queue, and the screen mode:
  `playing` or `waiting`). Sent on connect and whenever something changes.
- `progress`: download progress of a song.

Messages from the screen to the server:

- `ended`: the current song finished.
- `error`: the video couldn't be played. The server marks it `failed` and
  advances.

Every state change is broadcast to all connected clients.

---

## 10. Deployment

`deploy/karaoke.service`: user `rober`, port 8004,
`EnvironmentFile=/home/rober/karaoke/.env` with the admin password and the
WiFi details for the QR code. `.env` is in `.gitignore`.

`deploy/kiosk.sh`: turns off the screensaver and launches Chromium in kiosk
mode pointing at `/screen`, with the autoplay flag. Meant to be called from an
alias. A plain launch is a new party: it first calls `POST /party/new`, so the
screen starts on the QR codes with nothing left from last time. `resume` and
`toggle` reopen it without touching the queue.

Firewall rule, documented in the README:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8004 proto tcp
```

That range must be adjusted to the home's real subnet. LAN only: never open it
to the internet.

Suggested aliases for `~/.bash_aliases` (the file already exists and has its
`halp` function; keep the `# name: what it does` comment format so they show
up in the help automatically):

```bash
# karaoke: launches the karaoke screen on the monitor
# karaoke-off: closes the karaoke screen
```

---

## 11. Tests

- State machine: never two songs in `playing` at once.
- Advance: if the next one is `downloading`, skip to the first `ready` one and
  the skipped one keeps its position.
- Empty queue or nothing ready: the screen goes back to waiting mode.
- Cache: adding an already downloaded song leaves it in `ready` without
  downloading.
- Permissions: a normal user can't remove other people's songs or call admin
  endpoints. Test by calling the endpoint directly, not only through the UI.
- Search: a query with quotes, semicolons and accents doesn't break anything.
- WiFi QR generation with an SSID and password containing characters the
  format requires escaping.

Tests must not call YouTube: fake yt-dlp's responses.

---

## 12. Out of scope

No scoring, no voice effects, no microphone connected to the computer, no
per-person rotating turns, no permanent accounts, no saved playlists. If the
system gets used and people like it, these can be added later.
