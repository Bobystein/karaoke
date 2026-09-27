# coatl karaoke

Home karaoke. The TV (over HDMI) shows the video with the lyrics; guests
search and queue songs from their phones over the home WiFi. The full design
is in [SPEC.md](SPEC.md).

- **Screen**: `http://localhost:8004/screen`, in Chromium in kiosk mode.
- **Phones**: `http://<coatl-ip>:8004`. The waiting screen shows one QR code
  to join the WiFi and another to open the app. The same codes show up while
  a song is paused, and the app's QR is also in the "up next" bar.

Videos are downloaded with yt-dlp into `KARAOKE_MEDIA_DIR` (on coatl,
`/mnt/media/karaoke`, the big disk) and stay there: that's the library.
Repeated songs start instantly and without internet.
The folder has a size limit, `KARAOKE_MEDIA_MAX_GB` (150 GB by default). Past
it, the videos requested or played least recently are deleted, so popular songs
stay and one-offs eventually leave. Songs in the current queue are never
deleted, and a deleted song is just downloaded again if someone asks for it.

---

## Installation

System requirements: Python 3.14, `ffmpeg` (yt-dlp uses it to merge video and
audio) and Chromium for the screen:

```bash
sudo apt install ffmpeg
flatpak install flathub org.chromium.Chromium   # or chromium via apt/snap; kiosk.sh detects which one
```

The app:

```bash
cd ~/karaoke
python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env
nano .env        # admin password, video folder, WiFi SSID and password
```

`.env` is in `.gitignore`. The admin and WiFi passwords live only there.

### Service

```bash
sudo cp deploy/karaoke.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now karaoke
systemctl status karaoke
journalctl -u karaoke -f          # logs
```

### Firewall (LAN only)

```bash
deploy/ufw.sh                     # = sudo ufw allow from 192.168.0.0/24 to any port 8004 proto tcp
```

The default subnet is coatl's current one (`192.168.0.0/24`). If the router
changes, pass it as an argument: `deploy/ufw.sh 192.168.1.0/24`. **Don't open
the port to the internet**: the app has no accounts and accepts text from
anyone on the network.

### Aliases

Add to `~/.bash_aliases` (using the `# name: what it does` format so they
show up in `halp`):

```bash
# karaoke: launches the karaoke screen on the monitor
alias karaoke='~/karaoke/deploy/kiosk.sh'
# karaoke-off: closes the karaoke screen
alias karaoke-off='~/karaoke/deploy/kiosk.sh stop'
# karaoke-resume: reopens the karaoke screen keeping the queue
alias karaoke-resume='~/karaoke/deploy/kiosk.sh resume'
```

---

## At the party

1. `escritorio` (if the graphical session isn't up).
2. `karaoke`: starts a new party. It empties whatever was left in the queue
   from last time (including a song that was playing), turns off the
   screensaver and opens the full-screen view on the QR codes. Guests' names
   and the downloaded library stay. If you closed the screen mid-party, reopen
   it with **Ctrl+Alt+K** or `karaoke-resume`, which keep the queue.
3. Guests scan both QR codes: WiFi, then the app. They enter their name once
   and can then search and queue songs.
4. At the end, `karaoke-off`.

### Pinned QR code

While a song plays, the app's QR code normally only shows up with the top bar
(at the start and end of each song, and when the queue changes). To keep it
always visible in the top-left corner, so nobody who lost the page is stuck,
pin it:

- from the admin panel on the phone: **📌 Pin QR on screen** (tap again to unpin), or
- with the **Q** key on the karaoke machine's keyboard (press again to unpin).

The screen briefly confirms the change. The setting survives restarts and new
parties; unpinned, everything works as before.

### Exiting from the machine itself

**Ctrl+Alt+K** closes the karaoke screen and brings the desktop back; pressing
it again reopens it, keeping the queue. It's an XFCE keyboard shortcut that runs
`deploy/kiosk.sh toggle`, so it works even while Chromium is full screen. To
set it up on a new machine (with the graphical session running):

```bash
xfconf-query -c xfce4-keyboard-shortcuts -p '/commands/custom/<Primary><Alt>k' \
  -n -t string -s "$HOME/karaoke/deploy/kiosk.sh toggle"
```

Or in Settings → Keyboard → Application Shortcuts.

### Admin mode

On the phone, the discreet **admin** link at the bottom asks for the
`KARAOKE_ADMIN_PASSWORD` password (or the optional second one,
`KARAOKE_ADMIN_PASSWORD_2`; either works). It gives you: skip, pause/resume, volume
±5, move songs up/down, remove any song, clear the queue and pin the QR code
on the screen (see below). The server checks
permission on every request. If both are empty, admin mode
is disabled.

The volume is the system's (`pactl set-sink-volume @DEFAULT_SINK@`), so it
also controls the bluetooth speaker if that's the active output.

---

## How it's built

```
kiosk browser (screen)   ─┐
guests' phones           ─┼─ WebSocket ─→ FastAPI ─→ SQLite
admin's phone            ─┘                   │
                                              └─→ yt-dlp → media/
```

- `app/queue.py`: the queue state machine
  (`queued → downloading → ready → playing → played`, plus `failed` and `removed`).
- `app/downloader.py`: yt-dlp as a library. Downloads H.264 + AAC in mp4 and
  rejects any other format before downloading it.
- `app/main.py`: HTTP routes, WebSocket and the download orchestrator (at most
  two at a time, always fetching the upcoming songs ahead).
- The screen is just another page: state lives on the server and the screen
  only plays what it's told. Its "ended" and "failed" reports are only
  accepted from `localhost`, so a phone can't skip songs.
- The screen doesn't depend on any CDN (it works without internet). The phone
  view uses Tailwind and HTMX from a CDN.

### Development

```bash
set -a; source .env; set +a
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8004 --reload
.venv/bin/python -m pytest          # tests don't call YouTube
```

(If the service is running, stop it first: `sudo systemctl stop karaoke`.)

---

## Troubleshooting

**The screen has no sound, or a red "audio blocked" notice shows up.**
Chromium was opened without `--autoplay-policy=no-user-gesture-required`.
Close it and open it with `karaoke`, which sets the flag. If Chromium was
already open with your normal profile, that's fine: the kiosk uses a separate
profile (`karaoke-kiosk`).

**The screen goes black mid-party.** `kiosk.sh` runs `xset s off -dpms`, but
XFCE's power manager can turn it back on. If that happens: Settings → Power
Manager → Display, and disable blanking while the karaoke is on.

**All downloads start failing.** It's almost always YouTube changing
something. Updating yt-dlp usually fixes it (yt-dlp isn't pinned in
`requirements.txt` for this reason):

```bash
scripts/update-ytdlp.sh           # updates it in .venv and restarts the service if it changed
```

The phone of whoever requested the song shows a short reason (bot
check, video unavailable, blocked in this country, no connection, or no
playable format) plus a **Try another version** button that goes back to the
search with the same query and marks the version that failed. yt-dlp's full
error and traceback only go to `journalctl -u karaoke`. If the log mentions a
JavaScript runtime, install `deno`.

**"YouTube asked to confirm you're not a bot".** YouTube is rate-limiting
this IP. It often clears up on its own after a while. If it doesn't, export
the cookies of a throwaway Google account in `cookies.txt` (Netscape) format
and point `KARAOKE_COOKIES_FILE` in `.env` at it. The file must be writable by
`rober`, because yt-dlp saves refreshed cookies back to it. It's read on every
download, so adding or removing it doesn't need a restart. Changing `.env`
does (`sudo systemctl restart karaoke`).

**The volume control returns error 502.** `pactl` can't find the audio: the
service uses `XDG_RUNTIME_DIR=/run/user/1000`, which only exists while
`rober` has a session open (graphical or ssh). Try in a terminal: `pactl info`.

**Phones can't open the app.** Check that they're on the same WiFi (not
mobile data), that the ufw rule covers their subnet (`sudo ufw status`), and
that the IP in the QR code is right: it's detected when the service starts,
so if coatl's IP changed, `sudo systemctl restart karaoke`.

**The service won't start and mentions `/mnt/media`.** The big disk isn't
mounted, and the service refuses to start without it (otherwise downloads
would fill up the system disk). Check it with `findmnt /mnt/media` and mount it
with `sudo mount /mnt/media`.

**Backing up the library.** Everything is in `KARAOKE_MEDIA_DIR` (videos) and
`data/karaoke.db` (titles, history, how many times each song played).
