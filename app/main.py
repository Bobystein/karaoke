"""HTTP and WebSocket routes. See SPEC.md, sections 7 to 9.

All orchestration lives here: state is in SQLite (app/queue.py), downloads
run in threads (asyncio.to_thread) and the database is only touched from the
asyncio loop. Every change ends in `changed()`, which starts pending
downloads, starts the next song if the screen is free and broadcasts the
full state to every client.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import socket
import subprocess
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import segno
from fastapi import Depends, FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, downloader, library
from app.queue import Forbidden, NotFound, Queue

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
PORT = 8004
COOKIE = "karaoke_sid"
COOKIE_MAX_AGE = 5 * 365 * 24 * 3600
MAX_NAME_LEN = 40
SEARCH_CACHE_SIZE = 500

templates = Jinja2Templates(directory=APP_DIR / "templates")


def mmss(seconds: int | None) -> str:
    if not seconds:
        return ""
    return f"{seconds // 60}:{seconds % 60:02d}"


templates.env.filters["mmss"] = mmss


# --- configuration ----------------------------------------------------------

@dataclass
class Config:
    db_path: Path = db.DEFAULT_DB_PATH
    media_dir: Path = downloader.DEFAULT_MEDIA_DIR
    # Size limit of the video library; the least recently used go first. 0 = no limit.
    media_max_bytes: int = library.DEFAULT_MAX_GB * library.GB
    admin_password: str = ""
    # A second, independent password (e.g. for someone else to use as admin).
    admin_password_2: str = ""
    wifi_ssid: str = ""
    wifi_password: str = ""
    port: int = PORT
    # Only the screen (Chromium on this same machine) may send `ended` and
    # `error` over the WebSocket, so a phone can't skip songs that way.
    screen_hosts: frozenset = field(default_factory=lambda: frozenset({"127.0.0.1", "::1"}))

    @property
    def admin_passwords(self) -> list[str]:
        """The admin passwords that are set. Empty = admin mode disabled."""
        return [p for p in (self.admin_password, self.admin_password_2) if p]

    @classmethod
    def from_env(cls) -> "Config":
        media_dir = os.environ.get("KARAOKE_MEDIA_DIR")
        return cls(
            media_dir=Path(media_dir) if media_dir else downloader.DEFAULT_MEDIA_DIR,
            media_max_bytes=parse_max_gb(os.environ.get("KARAOKE_MEDIA_MAX_GB", "")),
            admin_password=os.environ.get("KARAOKE_ADMIN_PASSWORD", ""),
            admin_password_2=os.environ.get("KARAOKE_ADMIN_PASSWORD_2", ""),
            wifi_ssid=os.environ.get("KARAOKE_WIFI_SSID", ""),
            wifi_password=os.environ.get("KARAOKE_WIFI_PASSWORD", ""),
        )


def parse_max_gb(value: str) -> int:
    """KARAOKE_MEDIA_MAX_GB in bytes. Empty or invalid = the default; 0 = no limit."""
    value = value.strip()
    if not value:
        return library.DEFAULT_MAX_GB * library.GB
    try:
        gb = float(value)
        if gb < 0 or gb != gb:  # negative or NaN
            raise ValueError
    except ValueError:
        log.warning("KARAOKE_MEDIA_MAX_GB=%r isn't a number of GB; using %d", value, library.DEFAULT_MAX_GB)
        return library.DEFAULT_MAX_GB * library.GB
    return int(gb * library.GB)


# --- QR ---------------------------------------------------------------------

def wifi_escape(value: str) -> str:
    """Escapes the WIFI format's special characters: (\\ ; , : ")."""
    return re.sub(r'([\\;,:"])', r"\\\1", value)


def wifi_payload(ssid: str, password: str) -> str:
    if password:
        return f"WIFI:T:WPA;S:{wifi_escape(ssid)};P:{wifi_escape(password)};;"
    return f"WIFI:T:nopass;S:{wifi_escape(ssid)};;"


def qr_svg(data: str) -> str:
    """Inline SVG without fixed width/height, so CSS can size it."""
    return segno.make(data, error="m").svg_inline(scale=10, border=2, omitsize=True)


def detect_lan_ip() -> str:
    """IP of the interface with the default route. A UDP connect sends no packets."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def owner_tag(session_id: str | None) -> str | None:
    """Public identifier of a request's owner.

    The session id is the credential (it lives in the cookie), so it is never
    broadcast: clients get this hash to tell which requests are theirs.
    """
    if not session_id:
        return None
    return hashlib.sha256(session_id.encode()).hexdigest()[:16]


def run_pactl(args: list[str]) -> None:
    """Runs pactl with fixed arguments (a list, no shell)."""
    subprocess.run(["pactl", *args], check=True, capture_output=True, timeout=5)


# --- app state --------------------------------------------------------------

class Karaoke:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = None
        self.queue: Queue | None = None
        self.clients: set[WebSocket] = set()
        self.progress: dict[str, float] = {}
        # Id of the paused request. The pause only applies to that song: if
        # the current one changes, the new one starts playing with no extra logic.
        self.paused_id: int | None = None
        self.search_cache: OrderedDict[str, dict] = OrderedDict()
        self.tasks: set[asyncio.Task] = set()
        self.lan_ip = "127.0.0.1"
        self.app_url = ""
        self.wifi_qr: str | None = None
        self.app_qr = ""

    def start(self) -> None:
        self.conn = db.connect(self.cfg.db_path)
        self.queue = Queue(self.conn)
        self.queue.recover()
        Path(self.cfg.media_dir).mkdir(parents=True, exist_ok=True)
        self.trim_library()  # in case the limit was lowered
        self.lan_ip = detect_lan_ip()
        self.app_url = f"http://{self.lan_ip}:{self.cfg.port}"
        self.app_qr = qr_svg(self.app_url)
        if self.cfg.wifi_ssid:
            self.wifi_qr = qr_svg(wifi_payload(self.cfg.wifi_ssid, self.cfg.wifi_password))

    @property
    def paused(self) -> bool:
        current = self.queue.current()
        return current is not None and current["id"] == self.paused_id

    def set_paused(self, paused: bool) -> None:
        current = self.queue.current()
        self.paused_id = current["id"] if (paused and current) else None

    @property
    def qr_pinned(self) -> bool:
        """The app's QR stays in the screen's corner while a song plays."""
        return db.get_setting(self.conn, "qr_pinned") == "1"

    def set_qr_pinned(self, pinned: bool) -> None:
        db.set_setting(self.conn, "qr_pinned", "1" if pinned else "0")

    def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.conn is not None:
            self.conn.close()

    def trim_library(self) -> None:
        with self.queue.lock:
            library.enforce_limit(self.conn, self.cfg.media_dir, self.cfg.media_max_bytes)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # --- state and broadcast ---

    def state_message(self) -> dict:
        snap = self.queue.snapshot()

        def public(item):
            if item is None:
                return None
            item = dict(item)
            item["owner"] = owner_tag(item.pop("session_id"))
            item["progress"] = self.progress.get(item["video_id"])
            return item

        return {
            "type": "state",
            "mode": snap["mode"],
            "current": public(snap["current"]),
            "queue": [public(i) for i in snap["queue"]],
            "up_next": [public(i) for i in snap["up_next"]],
            "next_ready": public(snap["next_ready"]),
            "failed": [public(i) for i in snap["failed"]],
            "paused": self.paused,
            "qr_pinned": self.qr_pinned,
        }

    async def broadcast(self, msg: dict) -> None:
        clients = list(self.clients)
        results = await asyncio.gather(
            *(ws.send_json(msg) for ws in clients), return_exceptions=True
        )
        for ws, res in zip(clients, results):
            if isinstance(res, Exception):
                self.clients.discard(ws)

    async def changed(self) -> None:
        self.pump()
        self.queue.start_if_idle()
        await self.broadcast(self.state_message())

    # --- downloads ---

    def pump(self) -> None:
        for vid in self.queue.claim_downloads():
            self.progress[vid] = 0.0
            self._spawn(self._download(vid))

    async def _download(self, video_id: str) -> None:
        loop = asyncio.get_running_loop()
        last = [-100.0]

        def on_progress(pct: float) -> None:  # runs in the download thread
            if pct - last[0] >= 2 or pct >= 100:
                last[0] = pct
                loop.call_soon_threadsafe(self._on_progress, video_id, pct)

        try:
            res = await asyncio.to_thread(
                downloader.download, video_id, self.cfg.media_dir, on_progress
            )
        except downloader.DownloadError as e:
            self.queue.mark_download_failed(video_id, str(e))
        except Exception:
            log.exception("download %s failed", video_id)
            self.queue.mark_download_failed(video_id, downloader.MESSAGES["other"])
        else:
            self.queue.mark_downloaded(video_id, res["file_path"])
            if not res.get("cached"):
                self.trim_library()
        finally:
            self.progress.pop(video_id, None)
        await self.changed()

    def _on_progress(self, video_id: str, pct: float) -> None:
        if video_id not in self.progress:
            return
        self.progress[video_id] = pct
        self._spawn(self.broadcast({"type": "progress", "video_id": video_id, "pct": pct}))

    # --- search ---

    def remember_results(self, results: list[dict]) -> None:
        for r in results:
            self.search_cache[r["video_id"]] = r
            self.search_cache.move_to_end(r["video_id"])
        while len(self.search_cache) > SEARCH_CACHE_SIZE:
            self.search_cache.popitem(last=False)


# --- app --------------------------------------------------------------------

def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    k = Karaoke(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        k.start()
        await k.changed()
        yield
        k.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.k = k
    app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")

    # --- sessions and permissions ---

    def get_session(request: Request) -> dict | None:
        return db.get_session(k.conn, request.cookies.get(COOKIE))

    def require_session(session=Depends(get_session)) -> dict:
        if session is None:
            raise HTTPException(401, "enter your name first")
        return session

    def require_admin(session=Depends(require_session)) -> dict:
        if not session["is_admin"]:
            raise HTTPException(403, "admin only")
        return session

    # --- views ---

    @app.get("/", response_class=HTMLResponse)
    async def mobile(request: Request, session=Depends(get_session)):
        return templates.TemplateResponse(
            request,
            "mobile.html",
            {
                "session": session,
                "owner": owner_tag(session["id"]) if session else None,
                "admin_enabled": bool(cfg.admin_passwords),
            },
        )

    def clean_name(name: str) -> str:
        return " ".join(name.split())[:MAX_NAME_LEN]

    @app.post("/session")
    async def create_session(request: Request, name: str = Form("")):
        name = clean_name(name)
        if not name:
            return templates.TemplateResponse(
                request,
                "mobile.html",
                {"session": None, "owner": None, "error": "Enter your name"},
                status_code=400,
            )
        session = db.create_session(k.conn, name)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, session["id"], max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax"
        )
        return resp

    @app.post("/session/name")
    async def rename_session(name: str = Form(""), session=Depends(require_session)):
        name = clean_name(name)
        if not name:
            raise HTTPException(400, "Enter your name")
        db.rename_session(k.conn, session["id"], name)
        await k.changed()
        return {"ok": True, "name": name}

    @app.post("/party/new")
    async def new_party(request: Request):
        """Called by kiosk.sh when it opens the screen for a new party. Only
        from this machine: a phone can't wipe the queue."""
        if request.client is None or request.client.host not in cfg.screen_hosts:
            raise HTTPException(403, "only from the karaoke machine")
        k.queue.new_party()
        k.paused_id = None
        await k.changed()
        return {"ok": True}

    @app.get("/screen", response_class=HTMLResponse)
    async def screen(request: Request):
        return templates.TemplateResponse(
            request,
            "screen.html",
            {
                "wifi_qr": k.wifi_qr,
                "wifi_ssid": cfg.wifi_ssid,
                "app_qr": k.app_qr,
                "app_url": k.app_url,
            },
        )

    # --- search and queue ---

    @app.get("/search", response_class=HTMLResponse)
    async def search(
        request: Request,
        q: str = "",
        mode: str = downloader.DEFAULT_MODE,
        failed: str = "",
        session=Depends(require_session),
    ):
        mode = downloader.normalize_mode(mode)
        error = None
        results: list[dict] = []
        link_id = downloader.parse_video_ref(q)
        try:
            if link_id is not None:
                try:
                    results = [await asyncio.to_thread(downloader.resolve, link_id)]
                except downloader.TooLong:
                    raise
                except downloader.DownloadError:
                    # A bare 11-character word ("Bittersweet") looks like an
                    # id; if it isn't a video, it was a search after all.
                    if q.strip() != link_id:
                        raise
                    link_id = None
            if link_id is None:
                results = await asyncio.to_thread(downloader.search, q, mode)
        except downloader.DownloadError as e:
            error = str(e)
        k.remember_results(results)
        return templates.TemplateResponse(
            request,
            "partials/results.html",
            {
                "results": results,
                "error": error,
                "q": q,
                "mode": mode,
                "is_link": link_id is not None,
                "failed": failed if downloader.is_valid_video_id(failed) else None,
            },
        )

    @app.post("/queue")
    async def add_to_queue(
        video_id: str = Form(...),
        q: str = Form(""),
        mode: str = Form(downloader.DEFAULT_MODE),
        session=Depends(require_session),
    ):
        if not downloader.is_valid_video_id(video_id):
            raise HTTPException(400, "invalid video id")
        # Only videos that came up in a search or are already in the library
        # are accepted, so we never trust metadata sent by the client.
        meta = k.search_cache.get(video_id)
        if meta is not None:
            db.upsert_song(
                k.conn, video_id, meta["title"], meta["channel"], meta["duration_s"], meta["thumb_url"]
            )
        elif db.get_song(k.conn, video_id) is None:
            raise HTTPException(404, "search for the song again")
        # Remembered for "try another version". A pasted link isn't worth
        # repeating (it gives the same video): the phone falls back to the title.
        query = " ".join(q.split())[: downloader.MAX_QUERY_LEN]
        if not query or downloader.parse_video_ref(query):
            query = None
        item = k.queue.add(video_id, session, query, downloader.normalize_mode(mode))
        await k.changed()
        return HTMLResponse(
            '<span class="shrink-0 px-3 py-3 font-bold text-emerald-400">Queued ✓</span>',
            status_code=201,
            headers={"X-Queue-Id": str(item["id"])},
        )

    @app.delete("/queue/{item_id}")
    async def remove_from_queue(item_id: int, session=Depends(require_session)):
        try:
            k.queue.remove(item_id, session)
        except NotFound as e:
            raise HTTPException(404, str(e))
        except Forbidden as e:
            raise HTTPException(403, str(e))
        await k.changed()
        return Response(status_code=200)

    @app.get("/media/{video_id}.mp4")
    async def media(video_id: str):
        if not downloader.is_valid_video_id(video_id):
            raise HTTPException(404)
        path = downloader.media_path(video_id, cfg.media_dir)
        if not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4")

    # --- admin ---

    @app.post("/admin/login")
    async def admin_login(password: str = Form(""), session=Depends(require_session)):
        if not cfg.admin_passwords:
            raise HTTPException(503, "admin disabled: KARAOKE_ADMIN_PASSWORD is not set")
        # Compare against every password (no short-circuit), so the response
        # time doesn't hint at which one was close.
        matches = [hmac.compare_digest(password.encode(), p.encode()) for p in cfg.admin_passwords]
        if not any(matches):
            await asyncio.sleep(1)  # slows down brute-force attempts
            raise HTTPException(403, "wrong password")
        db.set_admin(k.conn, session["id"])
        return JSONResponse({"ok": True}, headers={"HX-Refresh": "true"})

    @app.post("/admin/skip")
    async def admin_skip(_=Depends(require_admin)):
        current = k.queue.current()
        if current is not None:
            k.queue.advance(expected_id=current["id"])
        await k.changed()
        return {"ok": True}

    @app.post("/admin/pause")
    async def admin_pause(paused: str | None = Form(None), _=Depends(require_admin)):
        k.set_paused((not k.paused) if paused is None else paused in ("1", "true", "on"))
        await k.changed()
        return {"ok": True, "paused": k.paused}

    @app.post("/admin/qr")
    async def admin_qr(pinned: str | None = Form(None), _=Depends(require_admin)):
        k.set_qr_pinned((not k.qr_pinned) if pinned is None else pinned in ("1", "true", "on"))
        await k.changed()
        return {"ok": True, "qr_pinned": k.qr_pinned}

    @app.post("/admin/reorder")
    async def admin_reorder(
        order: str | None = Form(None),
        id: int | None = Form(None),
        delta: int | None = Form(None),
        _=Depends(require_admin),
    ):
        if order is not None:
            try:
                ids = [int(x) for x in order.split(",") if x.strip()]
            except ValueError:
                raise HTTPException(400, "order must be a comma-separated list of ids")
            k.queue.reorder(ids)
        elif id is not None and delta is not None:
            try:
                k.queue.move(id, delta)
            except NotFound as e:
                raise HTTPException(404, str(e))
        else:
            raise HTTPException(400, "send order, or id and delta")
        await k.changed()
        return {"ok": True}

    @app.post("/admin/clear")
    async def admin_clear(_=Depends(require_admin)):
        k.queue.clear()
        await k.changed()
        return {"ok": True}

    @app.post("/admin/volume")
    async def admin_volume(
        level: int | None = Form(None),
        delta: int | None = Form(None),
        _=Depends(require_admin),
    ):
        if level is not None:
            if not 0 <= level <= 150:
                raise HTTPException(400, "level must be between 0 and 150")
            arg = f"{level}%"
        elif delta is not None:
            if not -50 <= delta <= 50 or delta == 0:
                raise HTTPException(400, "delta must be between -50 and 50")
            arg = f"{delta:+d}%"
        else:
            raise HTTPException(400, "send level or delta")
        try:
            await asyncio.to_thread(run_pactl, ["set-sink-volume", "@DEFAULT_SINK@", arg])
        except (subprocess.SubprocessError, OSError) as e:
            raise HTTPException(502, f"pactl failed: {e}")
        return {"ok": True, "volume": arg}

    # --- WebSocket ---

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        k.clients.add(ws)
        try:
            await ws.send_json(k.state_message())
            while True:
                raw = await ws.receive_text()
                if ws.client is None or ws.client.host not in cfg.screen_hosts:
                    continue
                try:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                    if kind == "toggle-qr":  # the Q key on the karaoke machine
                        k.set_qr_pinned(not k.qr_pinned)
                        await k.changed()
                        continue
                    item_id = int(msg["id"])
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
                current = k.queue.current()
                if current is None or current["id"] != item_id:
                    continue  # repeated or late report for a song that is no longer playing
                if kind == "ended":
                    k.queue.advance(expected_id=item_id)
                elif kind == "error":
                    reason = str(msg.get("error") or "the video could not be played")[:300]
                    k.queue.fail_current(reason, expected_id=item_id)
                else:
                    continue
                await k.changed()
        except WebSocketDisconnect:
            pass
        finally:
            k.clients.discard(ws)

    return app


app = create_app()
