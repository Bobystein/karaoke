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

Runs in `/srv/karaoke`, port 8004, open to the local WiFi (not only
Tailscale).

---

## 2. Fixed stack

Python 3.14, FastAPI, Uvicorn, Jinja2, HTMX and Tailwind (their browser
builds, copied into `app/static/vendor/` so the phones need no internet),
`websockets` (via FastAPI), standard SQLite, `yt-dlp`, `segno` (for the QR
codes; it's pure Python and writes SVG). pytest for tests. No ORM, no Node, no
build step.

---

## 3. Structure

```
/srv/karaoke/
├── app/
│   ├── main.py          # HTTP and WebSocket routes
│   ├── queue.py         # queue state, the state machine
│   ├── downloader.py    # yt-dlp wrapper, search and download
│   ├── check.py         # health check of the routes to YouTube, for uptime-kuma
│   ├── catalog.py       # the downloaded songs and their artist, for the phone
│   ├── db.py
│   ├── templates/
│   │   ├── base.html
│   │   ├── screen.html      # the TV view
│   │   ├── mobile.html      # the phone view
│   │   └── partials/
│   └── static/          # our JS, and vendor/ with htmx and Tailwind
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
    artist       TEXT,                   -- set by an admin; NULL = guessed from the title (app/catalog.py)
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
    attempts      INTEGER NOT NULL DEFAULT 0,  -- automatic download retries so far
    retry_at      REAL,                   -- unix time of the next retry; NULL = not waiting for one
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
   ↑          │
   └─ retry ──┴──→ failed
```

- **queued**: just added. If the song is already cached (`songs.file_path` is
  not NULL and the file exists), it goes straight to `ready` without
  downloading.
- **downloading**: `yt-dlp` working in the background. The UI shows progress.
- **ready**: file on disk, ready to play.
- **playing**: only ONE can be in this state at a time. An invariant the tests
  must check.
- **played**: finished. Stays in the table as history.
- **failed**: the download failed for good: either for a reason that trying
  again won't fix, or after running out of automatic retries (below). The
  reason is stored in `error`, shown to whoever requested it, and the queue
  moves on to the next one.
- **removed**: removed by the admin or by whoever requested it. The row is not
  deleted.

The server downloads **ahead of time**, but only as fast as it needs to:
every download is a chance for YouTube to flag the IP. The policy
(`download_budget()` in `app/queue.py`):

- Nothing playing: download right away (someone is waiting), up to 2 at a time.
- Playing, and fewer than 2 requests `ready` after the current one: right away
  (we're running out of cushion), 1 at a time.
- Otherwise: 1 at a time, and only once `KARAOKE_DOWNLOAD_INTERVAL_S` (default
  90) seconds have passed since the previous download finished (failed ones
  count; files that were already cached don't). A timer checks again when the
  interval is up.

Downloads also pass yt-dlp `sleep_interval` 1 to 3 s and
`sleep_interval_requests` 1 s, to space out the requests within one
extraction. Searches don't, since someone is waiting for them.

**Automatic retries.** A download that fails for a reason that may pass goes
back to `queued`, keeping its spot, with `attempts` + 1 and `retry_at` set to
when it will be tried again; `error` holds a short note the phone shows
meanwhile ("retrying at 21:50"). It isn't claimed before `retry_at`, and it
doesn't block the songs behind it. The delays (`RETRY_DELAYS_S` in
`app/queue.py`), by kind of failure:

| Kind | Delays before each retry | Then |
|---|---|---|
| `bot` (YouTube's bot check, 429) | 5, 10, 15, 20 min | `failed` |
| `offline`, `other` (a 403 on one file, a dropped connection) | 1, 5, 10 min | `failed` |
| `unavailable`, `geo`, `format` | none | `failed` at once |

**Hold after a bot check.** While YouTube is turning the IP down every other
song would fail too, and each try counts against us. So a `bot` failure also
stops **all** downloads for a while: 5 min the first time, then 10, 15 and 20
(the longest) for each hold in a row; a download that gets through resets the
count. The song that hit it is retried when the hold ends. Songs already in
the library keep playing during a hold. The hold lives in memory
(`Karaoke.hold_until`): a restart forgets it, but not each song's `retry_at`.

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

**Routes.** A download can reach YouTube three ways, tried in this order
(`routes()` in `app/downloader.py`):

1. **direct**: the house's IP, no account. Always there, and what works
   almost always.
2. **cookies**: the same IP, signed in. Only if `KARAOKE_COOKIES_FILE` is set
   and the file exists; it's passed to yt-dlp as `cookiefile`. A fallback
   rather than the default so the account is used as little as possible.
3. **proxy**: another IP, no account. Only if `KARAOKE_PROXY_URL` is set (on
   coatl, the VPN's HTTP proxy at `http://127.0.0.1:8888`).

The next route is tried only when the failure is `bot` or `other`, which is
when coming from somewhere else can help; a video that is gone, geo-blocked,
unplayable or unreachable fails the same from anywhere. If every route
fails, the error reported is the direct route's. Every route is paced the
same. Searches and pasted links only use the direct route. Only once all
routes have failed do the automatic retries of section 5 come in.

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

### Health check

`python -m app.check` (`app/check.py`) asks YouTube for one known video
through every configured route, the way a download does but without
downloading it, and reports to a Push monitor in uptime-kuma
(`KARAOKE_KUMA_PUSH_URL`): `up` with "OK: direct, cookies, proxy", or `down`
naming the routes that failed, why, and which still work. uptime-kuma sends
the alert, also when no report arrives at all. `deploy/karaoke-check.timer`
runs it every 55 minutes. The cookies route is only asked once a day, or when
the file changes (the last result is repeated in between, kept in
`data/check.json`), so the account is used as little as possible.

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
5. **Recently played**, under the queue: the last 5 songs that played and who
   asked for them, newest first, each with an "Again" button. A closed
   `<details>`: it's there for whoever wants to look, not always on screen.
   Part of the `state` message (`recent`).
6. **Downloaded songs**, a closed `<details>` under the search: everything
   that is on disk, to request without internet. `GET /library` sends the
   whole list once, when the section is opened, and the phone filters and
   groups it on its own (it has to be instant and work offline):
   - **By artist** (the default): one collapsible group per artist, in
     alphabetical order, with how many songs each has.
   - **A–Z**: every song in one list, by title.
   - A **filter** box that matches the song or the artist, ignoring accents
     and case. While filtering, every artist with a match is open.
   - "Add" requests the song with the same `POST /queue`; it's on disk, so it
     goes straight to `ready`.

The phone view loads nothing from the internet: htmx and Tailwind are served
from `app/static/vendor/`. With the house offline the search fails with a
note pointing to "Downloaded songs", and everything else keeps working.

**The artist.** YouTube gives a title and a channel, and the channel is often
whoever uploaded the karaoke track. `app/catalog.py` guesses the artist from
the title, on every read, using the rest of the library: the official channel
when it's named in the title; else the left side of "Artist - Song"; the
other side when other songs show that one is the artist ("Penurias - Los
Vaguens"); a known artist named in a title with no separators; "Other" when
nothing fits. Spellings are merged ignoring accents and case. It gets some
wrong, so an admin can set it by hand (`songs.artist`, the ✎ next to each
song), and that always wins; empty goes back to the guess.

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
- Jump 5 seconds back or forward in the song that is playing
  (`POST /admin/seek`). The position lives in the screen's `<video>`, so the
  server only passes a `seek` message on to it.
- Set a song's artist in "Downloaded songs".

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
- `GET /library` → the downloaded songs as JSON (`video_id`, `title`,
  `artist`, `duration_s`, `play_count`), sorted by title
- `GET /media/{video_id}.mp4` → serves the file with Range support, which the
  browser needs to seek within the video
- `POST /admin/login` → checks the password
- `POST /admin/skip`, `/admin/pause`, `/admin/reorder`, `/admin/clear`
- `POST /admin/volume` → calls `pactl set-sink-volume @DEFAULT_SINK@`
- `POST /admin/seek` → `delta` seconds (−60 to 60, not 0) in the current
  song; 409 if nothing is playing
- `POST /admin/songs/{video_id}/artist` → sets the artist by hand (empty =
  guess it again)

### WebSocket `/ws`

Messages from the server to clients:

- `state`: full state (what's playing, the queue, and the screen mode:
  `playing` or `waiting`). Sent on connect and whenever something changes.
- `progress`: download progress of a song.
- `seek`: `{id, delta}`, an admin's jump within the current song. The screen
  applies it only if `id` is the request it is playing.

Messages from the screen to the server:

- `ended`: the current song finished.
- `error`: the video couldn't be played. The server marks it `failed` and
  advances.

Every state change is broadcast to all connected clients.

---

## 10. Deployment

`deploy/karaoke.service`: user `rober`, port 8004,
`EnvironmentFile=/srv/karaoke/.env` with the admin password and the
WiFi details for the QR code. `.env` is in `.gitignore`.

`deploy/karaoke-check.service` and `.timer`: the health check of section 6,
every 55 minutes, as `rober` with the same `.env`.

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
- Catalog: the artist guess on the kinds of titles YouTube really has, and
  that only files on disk are listed.
- The phone page references nothing outside this server.
- Retries: a passing failure goes back to `queued` and isn't tried before
  its time; a bot check holds every download; retries run out into `failed`.
- Routes: direct first; cookies and proxy only after a `bot` or `other`
  failure, never for searches.
- Search: a query with quotes, semicolons and accents doesn't break anything.
- WiFi QR generation with an SSID and password containing characters the
  format requires escaping.

Tests must not call YouTube: fake yt-dlp's responses.

---

## 12. Out of scope

No scoring, no voice effects, no microphone connected to the computer, no
per-person rotating turns, no permanent accounts, no saved playlists. If the
system gets used and people like it, these can be added later.
