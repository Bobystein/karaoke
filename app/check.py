"""Health check of the routes to YouTube, reported to uptime-kuma.

    .venv/bin/python -m app.check

Asks YouTube for one known video through each of `downloader.routes()`
(direct, cookies, proxy) the way a download does, without downloading it: the
bot check shows up right there. The result goes to a Push monitor in
uptime-kuma (`KARAOKE_KUMA_PUSH_URL`), which sends the alerts. Run by
`deploy/karaoke-check.timer`.

Every check is one more request to YouTube, so the cookies route, which also
wears the account, is only asked once a day (or when the file changes); the
rest of the time its last result is reported again.
"""

import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from app import db, downloader

log = logging.getLogger(__name__)

# A video that has been up for years and has H.264 + AAC formats.
DEFAULT_VIDEO_ID = "dQw4w9WgXcQ"
COOKIES_EVERY_S = 24 * 3600
STATE_PATH = db.DEFAULT_DB_PATH.parent / "check.json"


def check_route(route_opts: dict, video_id: str) -> str | None:
    """None if YouTube answers through this route with a format we can play,
    else the kind of failure (downloader.error_kind)."""
    opts = downloader.ydl_opts(format=downloader.FORMAT, **route_opts)
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with downloader.YoutubeDL(opts) as ydl:
            downloader.check_playable(ydl.extract_info(url, download=False))
    except Exception as e:
        text = downloader._clean(e, limit=None)
        log.warning("check failed: %s", text)
        return downloader.error_kind(text)
    return None


def cookies_due(state: dict, route_opts: dict, now: float) -> bool:
    """Whether the cookies route should be asked now: its last check is a day
    old, or the file was replaced since."""
    last = state.get("cookies")
    if not last or now - last["at"] >= COOKIES_EVERY_S:
        return True
    try:
        # yt-dlp rewrites the file when it uses it; that one doesn't count.
        return Path(route_opts["cookiefile"]).stat().st_mtime > last["mtime"]
    except OSError:
        return True


def run(video_id: str, state: dict, now: float) -> tuple[dict[str, str | None], dict]:
    """Checks every route. Returns ({route: failure kind or None}, new state)."""
    results: dict[str, str | None] = {}
    for route, route_opts in downloader.routes():
        if route == "cookies" and not cookies_due(state, route_opts, now):
            results[route] = state["cookies"]["result"]
            continue
        log.info("checking the %s route", route)
        results[route] = check_route(route_opts, video_id)
        if route == "cookies":
            state = {
                **state,
                "cookies": {
                    "at": now,
                    "result": results[route],
                    "mtime": Path(route_opts["cookiefile"]).stat().st_mtime,
                },
            }
    return results, state


def summary(results: dict[str, str | None]) -> tuple[bool, str]:
    """(everything fine, the message for the alert)."""
    failed = {route: kind for route, kind in results.items() if kind}
    if not failed:
        return True, "OK: " + ", ".join(results)
    parts = [f"{route} FAILED ({kind})" for route, kind in failed.items()]
    working = [route for route in results if route not in failed]
    parts.append("still working: " + ", ".join(working) if working else "no route works")
    return False, "; ".join(parts)


def push(url: str, ok: bool, msg: str, ping_ms: int) -> None:
    """Reports to uptime-kuma's Push monitor. Whatever query the URL came with is replaced."""
    base = url.split("?", 1)[0]
    query = urllib.parse.urlencode({"status": "up" if ok else "down", "msg": msg, "ping": ping_ms})
    with urllib.request.urlopen(f"{base}?{query}", timeout=15) as r:
        r.read()


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    video_id = os.environ.get("KARAOKE_CHECK_VIDEO_ID", "").strip() or DEFAULT_VIDEO_ID
    started = time.monotonic()
    results, state = run(video_id, load_state(STATE_PATH), time.time())
    STATE_PATH.write_text(json.dumps(state))
    ok, msg = summary(results)
    log.info(msg)

    url = os.environ.get("KARAOKE_KUMA_PUSH_URL", "").strip()
    if not url:
        log.warning("KARAOKE_KUMA_PUSH_URL is not set: nobody was told")
        return 0 if ok else 1
    try:
        push(url, ok, msg, int((time.monotonic() - started) * 1000))
    except OSError as e:
        log.error("couldn't report to uptime-kuma: %s", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
