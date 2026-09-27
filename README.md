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
**They are never deleted automatically.**

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
```

---

## At the party

1. `escritorio` (if the graphical session isn't up).
2. `karaoke`: turns off the screensaver and opens the full-screen view.
3. Guests scan both QR codes: WiFi, then the app. They enter their name once
   and can then search and queue songs.
4. At the end, `karaoke-off`.

### Exiting from the machine itself

**Ctrl+Alt+K** closes the karaoke screen and brings the desktop back; pressing
it again reopens it. It's an XFCE keyboard shortcut that runs
`deploy/kiosk.sh toggle`, so it works even while Chromium is full screen. To
set it up on a new machine (with the graphical session running):

```bash
xfconf-query -c xfce4-keyboard-shortcuts -p '/commands/custom/<Primary><Alt>k' \
  -n -t string -s "$HOME/karaoke/deploy/kiosk.sh toggle"
```

Or in Settings → Keyboard → Application Shortcuts.

### Admin mode

On the phone, the discreet **admin** link at the bottom asks for the
`KARAOKE_ADMIN_PASSWORD` password. It gives you: skip, pause/resume, volume
±5, move songs up/down, remove any song and clear the queue. The server checks
permission on every request. If `KARAOKE_ADMIN_PASSWORD` is empty, admin mode
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
something. Updating yt-dlp usually fixes it:

```bash
.venv/bin/pip install -U yt-dlp && sudo systemctl restart karaoke
```

If the error mentions a JavaScript runtime, install `deno`. The failure reason
shows up on the phone of whoever requested the song and in
`journalctl -u karaoke`.

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
