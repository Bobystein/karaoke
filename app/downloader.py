"""yt-dlp wrapper: search and download.

yt-dlp is used as a library. The user's query is a Python parameter, never
interpolated into a command line.
"""

import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL  # module-level reference so tests can fake it

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEDIA_DIR = ROOT / "media"

SEARCH_RESULTS = 12
MAX_DURATION_S = 15 * 60
MAX_QUERY_LEN = 200

# H.264 + AAC in mp4: what the browser plays natively and the UHD 620
# decodes in hardware.
FORMAT = (
    "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]"
    "/best[height<=1080][ext=mp4]"
)

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Search modes: the word appended to the query (None = the query as typed).
SEARCH_MODES = {"karaoke": "karaoke", "lyrics": "lyrics", "youtube": None}
DEFAULT_MODE = "karaoke"

# Hosts accepted in pasted links. Exact match: "youtube.com.evil.net" is not one.
WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SHORT_HOSTS = {"youtu.be", "www.youtu.be"}

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
}


class DownloadError(Exception):
    """The download failed. The message is stored in queue.error and shown to the user,
    so it is always one of the readable messages from `explain()`, never yt-dlp's text."""


def is_valid_video_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.match(video_id or ""))


def ydl_opts(**extra) -> dict:
    """BASE_OPTS plus the cookie file, if KARAOKE_COOKIES_FILE points to one that exists.

    Read on every call, so dropping in or removing the file needs no restart.
    yt-dlp writes refreshed cookies back to it, so it must be writable.
    """
    opts = {**BASE_OPTS, **extra}
    cookies = os.environ.get("KARAOKE_COOKIES_FILE", "").strip()
    if cookies:
        if Path(cookies).is_file():
            opts["cookiefile"] = cookies
        else:
            log.warning("KARAOKE_COOKIES_FILE=%s does not exist; continuing without cookies", cookies)
    return opts


# --- errors -----------------------------------------------------------------

# (kind, patterns in yt-dlp's text, message for the phone). The first match
# wins, so the bot check goes before "no connection": a 429 also comes as
# "Unable to download webpage".
ERRORS = [
    (
        "bot",
        ("not a bot", "http error 429", "too many requests"),
        "YouTube asked to confirm you're not a bot. Try another version in a few "
        "minutes; if it keeps happening, the admin should set KARAOKE_COOKIES_FILE "
        "or update yt-dlp (scripts/update-ytdlp.sh).",
    ),
    (
        "geo",
        ("available in your country", "geo restrict", "geo-restrict", "blocked it in your country"),
        "This video is blocked in this country. Try another version.",
    ),
    (
        "unavailable",
        (
            "video unavailable", "video is unavailable", "private video", "has been removed", "been terminated",
            "no longer available", "members-only", "join this channel", "confirm your age",
            "age-restricted", "premieres in", "live event will begin",
        ),
        "This video is no longer available on YouTube (private, removed or restricted). "
        "Try another version.",
    ),
    (
        "offline",
        (
            "unable to download webpage", "failed to resolve", "name resolution",
            "name or service not known", "network is unreachable", "timed out",
            "connection refused", "connection reset", "remote end closed", "getaddrinfo",
            "no route to host", "ssl:",
        ),
        "Can't reach YouTube. Check that the karaoke has internet and try again.",
    ),
    (
        "format",
        ("not playable", "requested format is not available", "no video formats"),
        "This video has no version that can play here (it needs H.264 + AAC in "
        "mp4). Try another version.",
    ),
]

MESSAGES = {kind: msg for kind, _, msg in ERRORS} | {
    "other": "Couldn't download this video. Try another version; if they all fail, "
    "the admin should update yt-dlp (scripts/update-ytdlp.sh).",
    "search": "The search failed. Try again; if it keeps failing, the admin should "
    "update yt-dlp (scripts/update-ytdlp.sh).",
}


def error_kind(text: str) -> str:
    low = text.lower()
    for kind, patterns, _ in ERRORS:
        if any(p in low for p in patterns):
            return kind
    return "other"


def explain(text: str, fallback: str = "other") -> str:
    """Turns yt-dlp's (or our own) technical text into a message someone at the party can act on."""
    kind = error_kind(text)
    return MESSAGES[fallback if kind == "other" else kind]


def _fail(what: str, e: BaseException, fallback: str = "other") -> DownloadError:
    """Logs the full error (with traceback) and returns the readable one for the UI."""
    text = _clean(e, limit=None)
    log.warning("%s: %s", what, text, exc_info=e)
    return DownloadError(explain(text, fallback))


def thumb_for(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


# --- search -----------------------------------------------------------------

def normalize_mode(mode: str | None) -> str:
    return mode if mode in SEARCH_MODES else DEFAULT_MODE


def build_query(query: str, mode: str = DEFAULT_MODE) -> str:
    q = " ".join((query or "").split())[:MAX_QUERY_LEN]
    word = SEARCH_MODES[normalize_mode(mode)]
    if q and word and word not in q.lower():
        q = f"{q} {word}"
    return q.strip()


def parse_video_ref(text: str) -> str | None:
    """The video id if `text` is a YouTube link or a bare id, else None.

    Only youtube.com/watch?v=<id>, youtu.be/<id> and a bare 11-character id
    count. The id is validated and, from here on, only the id is used: the
    text itself never reaches yt-dlp.
    """
    text = (text or "").strip()
    if not text or len(text) > MAX_QUERY_LEN or any(c.isspace() for c in text):
        return None
    if is_valid_video_id(text):
        return text
    if "://" not in text:
        text = "https://" + text
    try:
        url = urlsplit(text)
        host = (url.hostname or "").lower()
        port = url.port
    except ValueError:  # malformed netloc or port
        return None
    if url.scheme not in ("http", "https") or url.username or port not in (None, 80, 443):
        return None
    if host in WATCH_HOSTS and url.path.rstrip("/") == "/watch":
        vid = (parse_qs(url.query).get("v") or [""])[0]
    elif host in SHORT_HOSTS:
        vid = url.path.strip("/")
    else:
        return None
    return vid if is_valid_video_id(vid) else None


def _result(vid: str, entry: dict) -> dict | None:
    """A search result, or None if it's too long to be a song."""
    duration = entry.get("duration")
    duration = int(duration) if duration else None
    if duration and duration > MAX_DURATION_S:
        return None  # compilations
    return {
        "video_id": vid,
        "title": entry.get("title") or vid,
        "channel": entry.get("channel") or entry.get("uploader"),
        "duration_s": duration,
        "thumb_url": thumb_for(vid),
    }


def search(query: str, mode: str = DEFAULT_MODE) -> list[dict]:
    """Searches YouTube. Returns dicts with video_id, title, channel, duration_s, thumb_url."""
    q = build_query(query, mode)
    if not q:
        return []
    try:
        with YoutubeDL(ydl_opts(extract_flat=True, skip_download=True)) as ydl:
            info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{q}", download=False)
    except Exception as e:
        raise _fail(f"search {q!r} failed", e, "search") from e

    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id")
        if not is_valid_video_id(vid):
            continue  # channels, playlists, etc.
        if (r := _result(vid, entry)) is not None:
            results.append(r)
    return results


class TooLong(DownloadError):
    pass


def resolve(video_id: str) -> dict:
    """Metadata for one video (a pasted link), in the same shape as a search result.

    Raises DownloadError with a readable message if it doesn't exist or can't
    be fetched, and TooLong if it goes over the same limit as the search.
    """
    if not is_valid_video_id(video_id):
        raise DownloadError(MESSAGES["unavailable"])
    try:
        with YoutubeDL(ydl_opts(skip_download=True)) as ydl:
            # process=False: only the metadata, without resolving formats.
            url = f"https://www.youtube.com/watch?v={video_id}"
            info = ydl.extract_info(url, download=False, process=False)
    except Exception as e:
        raise _fail(f"resolve {video_id} failed", e) from e
    if not info or info.get("id") != video_id:
        raise DownloadError(MESSAGES["unavailable"])
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise DownloadError(MESSAGES["unavailable"])
    result = _result(video_id, info)
    if result is None:
        raise TooLong(f"That video is longer than {MAX_DURATION_S // 60} minutes; it doesn't look like a song.")
    return result


# --- download ---------------------------------------------------------------

def media_path(video_id: str, media_dir: Path = DEFAULT_MEDIA_DIR) -> Path:
    if not is_valid_video_id(video_id):
        raise DownloadError(f"invalid video id: {video_id!r}")
    return Path(media_dir) / f"{video_id}.mp4"


def _codecs(info: dict) -> tuple[str, str, str]:
    """(vcodec, acodec, ext) of the chosen format, whether merged or a single file."""
    parts = info.get("requested_formats") or [info]
    vcodec = next((p["vcodec"] for p in parts if p.get("vcodec") not in (None, "none")), "none")
    acodec = next((p["acodec"] for p in parts if p.get("acodec") not in (None, "none")), "none")
    return vcodec or "none", acodec or "none", info.get("ext") or ""


class NotPlayable(Exception):
    """Technical reason; download() logs it and turns it into a DownloadError."""


def check_playable(info: dict) -> None:
    """Fails if the chosen format isn't H.264 + AAC in mp4."""
    vcodec, acodec, ext = _codecs(info)
    if not vcodec.startswith("avc1") or not acodec.startswith("mp4a") or ext != "mp4":
        raise NotPlayable(
            f"format not playable in the browser: video={vcodec} audio={acodec} container={ext}"
        )


def download(
    video_id: str,
    media_dir: Path = DEFAULT_MEDIA_DIR,
    on_progress: Callable[[float], None] | None = None,
) -> dict:
    """Downloads the video to media/<video_id>.mp4 unless it's already there.

    Returns {"file_path", "title", "channel", "duration_s", "cached"}.
    `on_progress` receives a 0-100 percentage (video and audio combined).
    Raises DownloadError with a readable reason if anything fails.
    """
    target = media_path(video_id, media_dir)
    if target.is_file():
        return {"file_path": str(target), "cached": True}
    target.parent.mkdir(parents=True, exist_ok=True)

    streams = {"total": 1, "done": 0}

    def hook(d: dict) -> None:
        if on_progress is None:
            return
        if d.get("status") == "finished":
            streams["done"] += 1
            frac = 1.0
        elif d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0.0
        else:
            return
        done = min(streams["done"], streams["total"])
        if d.get("status") == "finished":
            pct = done / streams["total"]
        else:
            pct = (done + min(frac, 1.0)) / streams["total"]
        on_progress(round(min(pct, 1.0) * 100, 1))

    opts = ydl_opts(
        format=FORMAT,
        merge_output_format="mp4",
        outtmpl=str(Path(media_dir) / "%(id)s.%(ext)s"),
        progress_hooks=[hook],
    )
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(opts) as ydl:
            # Resolve the format first without downloading, to reject it
            # before spending bandwidth if it isn't playable.
            info = ydl.extract_info(url, download=False)
            check_playable(info)
            streams["total"] = len(info.get("requested_formats") or [info])
            info = ydl.process_ie_result(info, download=True)
            if not target.is_file():
                raise RuntimeError(f"yt-dlp finished but left no {target}")
    except Exception as e:  # yt-dlp, ffmpeg, our format check or anything else
        _cleanup_partials(video_id, media_dir)
        raise _fail(f"download {video_id} failed", e) from e

    return {
        "file_path": str(target),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration_s": int(info["duration"]) if info.get("duration") else None,
        "cached": False,
    }


def _cleanup_partials(video_id: str, media_dir: Path) -> None:
    """Deletes leftovers of a failed download (.part, unmerged streams). Never the final .mp4."""
    final = Path(media_dir) / f"{video_id}.mp4"
    for f in Path(media_dir).glob(f"{video_id}.*"):
        if f != final:
            f.unlink(missing_ok=True)


def _clean(e: BaseException, limit: int | None = 300) -> str:
    msg = str(e) or type(e).__name__
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # ANSI colors
    return msg.removeprefix("ERROR: ").strip()[:limit]
