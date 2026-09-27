"""yt-dlp wrapper: search and download.

yt-dlp is used as a library. The user's query is a Python parameter, never
interpolated into a command line.
"""

import re
from collections.abc import Callable
from pathlib import Path

import yt_dlp
from yt_dlp import YoutubeDL  # module-level reference so tests can fake it

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

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "noplaylist": True,
}


class DownloadError(Exception):
    """The download failed. The message is stored in queue.error and shown to the user."""


def is_valid_video_id(video_id: str) -> bool:
    return bool(VIDEO_ID_RE.match(video_id or ""))


def thumb_for(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


# --- search -----------------------------------------------------------------

def build_query(query: str, add_karaoke: bool = True) -> str:
    q = " ".join((query or "").split())[:MAX_QUERY_LEN]
    if q and add_karaoke and "karaoke" not in q.lower():
        q = f"{q} karaoke"
    return q.strip()


def search(query: str, add_karaoke: bool = True) -> list[dict]:
    """Searches YouTube. Returns dicts with video_id, title, channel, duration_s, thumb_url."""
    q = build_query(query, add_karaoke)
    if not q:
        return []
    opts = {**BASE_OPTS, "extract_flat": True, "skip_download": True}
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{SEARCH_RESULTS}:{q}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(f"search failed: {_clean(e)}") from e

    results = []
    for entry in (info or {}).get("entries") or []:
        if not entry:
            continue
        vid = entry.get("id")
        if not is_valid_video_id(vid):
            continue  # channels, playlists, etc.
        duration = entry.get("duration")
        duration = int(duration) if duration else None
        if duration and duration > MAX_DURATION_S:
            continue  # compilations
        results.append(
            {
                "video_id": vid,
                "title": entry.get("title") or vid,
                "channel": entry.get("channel") or entry.get("uploader"),
                "duration_s": duration,
                "thumb_url": thumb_for(vid),
            }
        )
    return results


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


def check_playable(info: dict) -> None:
    """Fails if the chosen format isn't H.264 + AAC in mp4."""
    vcodec, acodec, ext = _codecs(info)
    if not vcodec.startswith("avc1") or not acodec.startswith("mp4a") or ext != "mp4":
        raise DownloadError(
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

    opts = {
        **BASE_OPTS,
        "format": FORMAT,
        "merge_output_format": "mp4",
        "outtmpl": str(Path(media_dir) / "%(id)s.%(ext)s"),
        "progress_hooks": [hook],
    }
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(opts) as ydl:
            # Resolve the format first without downloading, to reject it
            # before spending bandwidth if it isn't playable.
            info = ydl.extract_info(url, download=False)
            check_playable(info)
            streams["total"] = len(info.get("requested_formats") or [info])
            info = ydl.process_ie_result(info, download=True)
    except yt_dlp.utils.DownloadError as e:
        _cleanup_partials(video_id, media_dir)
        raise DownloadError(_clean(e)) from e
    except DownloadError:
        raise
    except Exception as e:  # anything else from yt-dlp or ffmpeg
        _cleanup_partials(video_id, media_dir)
        raise DownloadError(f"unexpected error: {e}") from e

    if not target.is_file():
        _cleanup_partials(video_id, media_dir)
        raise DownloadError("yt-dlp finished but left no media/<id>.mp4")

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


def _clean(e: Exception) -> str:
    msg = str(e)
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # ANSI colors
    return msg.removeprefix("ERROR: ").strip()[:300]
