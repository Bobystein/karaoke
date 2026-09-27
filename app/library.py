"""Size limit for the video library (KARAOKE_MEDIA_MAX_GB).

When the media folder goes over the limit, the videos used least recently
(last requested or played) are deleted until it fits again. Songs that get
requested keep being "touched" and stay; the ones requested once and never
again end up leaving. A deleted song keeps its row in `songs` (title, play
count): if someone asks for it again, it's simply downloaded again.

Never deleted: the files of requests still active (queued, downloading, ready
or playing), and anything in the folder that isn't a song in the database.
"""

import logging
import sqlite3
from pathlib import Path

from app import db

log = logging.getLogger(__name__)

GB = 1000**3  # decimal, like disk sizes and `df -H`
DEFAULT_MAX_GB = 150


def folder_size(media_dir: Path) -> int:
    """Bytes used by every file in the folder, including partial downloads."""
    total = 0
    for f in Path(media_dir).iterdir():
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass  # deleted meanwhile
    return total


def enforce_limit(conn: sqlite3.Connection, media_dir: Path, max_bytes: int) -> list[str]:
    """Deletes the least recently used videos until the folder fits in `max_bytes`.

    Returns the video_ids deleted, oldest first. `max_bytes <= 0` = no limit.
    """
    if max_bytes <= 0 or not Path(media_dir).is_dir():
        return []
    used = folder_size(media_dir)
    if used <= max_bytes:
        return []

    media_dir = Path(media_dir).resolve()
    candidates = conn.execute(
        """
        SELECT video_id, file_path FROM songs
        WHERE file_path IS NOT NULL
          AND video_id NOT IN (
              SELECT video_id FROM queue
              WHERE state IN ('queued','downloading','ready','playing'))
        ORDER BY COALESCE(last_used_at, downloaded_at, '') ASC, video_id
        """
    ).fetchall()

    deleted = []
    for row in candidates:
        if used <= max_bytes:
            break
        path = Path(row["file_path"])
        # Only files inside the media folder, never something a bad row points to.
        if path.resolve().parent != media_dir:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
        except FileNotFoundError:
            size = 0  # already gone: just fix the database
        except OSError as e:
            log.warning("couldn't delete %s: %s", path, e)
            continue
        db.forget_file(conn, row["video_id"])
        used -= size
        deleted.append(row["video_id"])

    if deleted:
        log.info(
            "library over %.0f GB: deleted %d least recently used videos, now %.1f GB",
            max_bytes / GB, len(deleted), used / GB,
        )
    if used > max_bytes:
        log.warning(
            "library still over its limit (%.1f of %.0f GB): the rest is in the queue or isn't ours",
            used / GB, max_bytes / GB,
        )
    return deleted
