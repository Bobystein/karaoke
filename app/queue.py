"""Queue state: the state machine from SPEC.md, section 5.

    queued ──→ downloading ──→ ready ──→ playing ──→ played
       ↑          │
       └─ retry ──┴──→ failed

Plus `removed` from any active state. Everything is synchronous and lives
in SQLite; orchestration (download threads, WebSocket broadcast) belongs to
main.py, which calls these methods and then broadcasts `snapshot()`.
"""

import sqlite3
import threading
import time

from app import db

# Download policy. Every download is a chance for YouTube to flag the IP, so
# we only hurry when someone is waiting:
#   - nothing playing: download right away, up to 2 at a time;
#   - playing, but fewer than READY_CUSHION songs ready after it: right away,
#     1 at a time (we're about to run out);
#   - otherwise: 1 at a time, and only once `min_interval` seconds have passed
#     since the previous download finished.
MAX_DOWNLOADS_IDLE = 2
MAX_DOWNLOADS_PLAYING = 1
READY_CUSHION = 2
DEFAULT_DOWNLOAD_INTERVAL_S = 90.0

# Automatic retries: seconds to wait before each one, by kind of failure
# (downloader.error_kind). A request whose download fails for one of these
# goes back to `queued` and is tried again later; after the last delay it
# becomes `failed`. The other kinds (video gone, geo, no playable format)
# would fail the same way again, so they fail at once.
#   - bot: YouTube is turning this IP down. Last time it lasted about 40
#     minutes, and asking again too soon only makes it longer.
#   - offline / other: usually a passing glitch (a 403 on one file, the
#     connection dropping).
RETRY_DELAYS_S = {
    "bot": (300.0, 600.0, 900.0, 1200.0),
    "offline": (60.0, 300.0, 600.0),
    "other": (60.0, 300.0, 600.0),
}


def retry_delay(kind: str, attempts: int) -> float | None:
    """Seconds until the next automatic retry of a download that failed with
    `kind` after `attempts` retries, or None if it shouldn't be retried."""
    delays = RETRY_DELAYS_S.get(kind, ())
    return delays[attempts] if attempts < len(delays) else None


def bot_hold(strikes: int) -> float:
    """Seconds with no downloads at all after YouTube's bot check, given how
    many holds in a row came before this one. While YouTube is turning the IP
    down every other song would fail too, and each try counts against us."""
    delays = RETRY_DELAYS_S["bot"]
    return delays[min(strikes, len(delays) - 1)]


# How many already played songs the phone shows under "Recently played".
RECENT_LIMIT = 5

# States that count as "in the queue" (still waiting to play).
PENDING = ("queued", "downloading", "ready")


def download_budget(
    playing: bool,
    ready: int,
    active: int,
    since_last: float | None,
    min_interval: float,
) -> tuple[int, float | None]:
    """How many downloads may start now, and how long to wait if the interval is what stops them.

    `ready`: requests in `ready` (the playing one isn't counted). `active`:
    videos downloading. `since_last`: seconds since the previous download
    finished (None = there wasn't one). The wait is None when there's nothing
    to wait for: either a download may start now, or one is running and its
    end will check again.
    """
    if not playing:
        return max(0, MAX_DOWNLOADS_IDLE - active), None
    slots = max(0, MAX_DOWNLOADS_PLAYING - active)
    if slots == 0 or ready < READY_CUSHION:
        return slots, None
    if since_last is None or since_last >= min_interval:
        return slots, None
    return 0, min_interval - since_last


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


class Queue:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        # Downloads finish on other threads; all access is serialized.
        self.lock = threading.RLock()

    # --- reads --------------------------------------------------------------

    def _items(self, where: str, params: tuple = (), order: str = "q.position, q.id") -> list[dict]:
        rows = self.conn.execute(
            f"""
            SELECT q.id, q.video_id, q.session_id, q.requested_by, q.state,
                   q.position, q.error, q.query, q.search_mode, q.added_at, q.played_at,
                   q.attempts, q.retry_at,
                   s.title, s.channel, s.duration_s, s.thumb_url
            FROM queue q JOIN songs s ON s.video_id = q.video_id
            WHERE {where}
            ORDER BY {order}
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get(self, item_id: int) -> dict | None:
        items = self._items("q.id = ?", (item_id,))
        return items[0] if items else None

    def current(self) -> dict | None:
        items = self._items("q.state = 'playing'")
        return items[0] if items else None

    def pending(self) -> list[dict]:
        return self._items("q.state IN ('queued','downloading','ready')")

    def failed(self) -> list[dict]:
        return self._items("q.state = 'failed'")

    def recent(self, limit: int = RECENT_LIMIT) -> list[dict]:
        """The last songs that played, newest first."""
        return self._items(
            "q.state = 'played' AND q.id IN ("
            "SELECT id FROM queue WHERE state = 'played' ORDER BY played_at DESC, id DESC LIMIT ?)",
            (limit,),
            order="q.played_at DESC, q.id DESC",
        )

    def next_ready(self) -> dict | None:
        items = self._items("q.state = 'ready'")
        return items[0] if items else None

    def snapshot(self) -> dict:
        """Full state sent to clients in the `state` message."""
        with self.lock:
            current = self.current()
            pending = self.pending()
            return {
                "mode": "playing" if current else "waiting",
                "current": current,
                "queue": pending,
                # What the screen's bar shows as "up next".
                "up_next": pending[:2],
                # What will actually play next (advance rule).
                "next_ready": self.next_ready(),
                "failed": self.failed(),
                "recent": self.recent(),
            }

    # --- adding -------------------------------------------------------------

    def _next_position(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM queue").fetchone()
        return row[0]

    def add(
        self, video_id: str, session: dict, query: str | None = None, search_mode: str | None = None
    ) -> dict:
        """Adds a request. If the song is already cached it goes straight to `ready`.

        The song must exist in `songs` (main.py registers it from the search
        result with db.upsert_song before calling this). `query` and
        `search_mode` are the search it came from, to offer another version if
        it fails.
        """
        with self.lock:
            if db.get_song(self.conn, video_id) is None:
                raise NotFound(f"unknown song: {video_id}")
            state = "ready" if db.cached_file(self.conn, video_id) else "queued"
            db.touch_song(self.conn, video_id)
            cur = self.conn.execute(
                """
                INSERT INTO queue
                    (video_id, session_id, requested_by, state, position, query, search_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    video_id, session["id"], session["name"], state, self._next_position(),
                    query, search_mode,
                ),
            )
            return self.get(cur.lastrowid)

    # --- downloads ----------------------------------------------------------

    def _active_downloads(self) -> set[str]:
        return {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT video_id FROM queue WHERE state = 'downloading'"
            )
        }

    def download_budget(
        self, since_last: float | None = None, min_interval: float = 0.0, now: float | None = None
    ) -> tuple[int, float | None]:
        """`download_budget()` applied to the current queue. With nothing queued
        there's nothing to wait for. Requests waiting for a retry that isn't
        due yet (at `now`, unix time) don't count as queued."""
        now = time.time() if now is None else now
        with self.lock:
            queued = self.conn.execute(
                "SELECT COUNT(*) FROM queue WHERE state = 'queued' AND COALESCE(retry_at, 0) <= ?",
                (now,),
            ).fetchone()[0]
            if not queued:
                return 0, None
            ready = self.conn.execute("SELECT COUNT(*) FROM queue WHERE state = 'ready'").fetchone()[0]
            return download_budget(
                self.current() is not None, ready, len(self._active_downloads()), since_last, min_interval
            )

    def next_retry_wait(self, now: float | None = None) -> float | None:
        """Seconds until the earliest retry that isn't due yet, or None if no request is waiting for one."""
        now = time.time() if now is None else now
        with self.lock:
            at = self.conn.execute(
                "SELECT MIN(retry_at) FROM queue WHERE state = 'queued' AND retry_at > ?", (now,)
            ).fetchone()[0]
            return None if at is None else at - now

    def claim_downloads(
        self,
        since_last: float | None = None,
        min_interval: float = 0.0,
        now: float | None = None,
        hold: bool = False,
    ) -> list[str]:
        """Moves the next queued requests to `downloading`, as the download policy allows.

        Returns the video_ids main.py should start downloading. If the same
        video is already downloading for another request it isn't fetched
        twice: that request waits in `queued` and becomes ready with the other.
        Requests waiting for a retry are skipped until it's due (at `now`,
        unix time). With `hold`, nothing new starts (see `bot_hold()`).
        """
        now = time.time() if now is None else now
        with self.lock, db.transaction(self.conn):
            # Downloaded while it was waiting (another request for the same
            # video): ready, whatever the policy says.
            for item in self._items("q.state = 'queued'"):
                if db.cached_file(self.conn, item["video_id"]):
                    self.conn.execute(
                        "UPDATE queue SET state = 'ready', error = NULL, retry_at = NULL WHERE id = ?",
                        (item["id"],),
                    )
            if hold:
                return []
            slots, _ = self.download_budget(since_last, min_interval, now)
            active = self._active_downloads()
            started: list[str] = []
            for item in self._items("q.state = 'queued' AND COALESCE(q.retry_at, 0) <= ?", (now,)):
                if slots <= 0:
                    break
                vid = item["video_id"]
                if vid in active:
                    continue
                self.conn.execute(
                    "UPDATE queue SET state = 'downloading', retry_at = NULL "
                    "WHERE video_id = ? AND state = 'queued'",
                    (vid,),
                )
                active.add(vid)
                started.append(vid)
                slots -= 1
            return started

    def mark_downloaded(self, video_id: str, file_path: str) -> None:
        with self.lock, db.transaction(self.conn):
            db.set_song_file(self.conn, video_id, file_path)
            self.conn.execute(
                "UPDATE queue SET state = 'ready', error = NULL, retry_at = NULL "
                "WHERE video_id = ? AND state IN ('queued','downloading')",
                (video_id,),
            )

    def mark_download_failed(self, video_id: str, error: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE queue SET state = 'failed', error = ?, retry_at = NULL "
                "WHERE video_id = ? AND state IN ('queued','downloading')",
                (error, video_id),
            )

    def download_attempts(self, video_id: str) -> int:
        """Automatic retries already spent on this video by the requests still waiting for it."""
        with self.lock:
            return self.conn.execute(
                "SELECT COALESCE(MAX(attempts), 0) FROM queue "
                "WHERE video_id = ? AND state IN ('queued','downloading')",
                (video_id,),
            ).fetchone()[0]

    def retry_download(self, video_id: str, error: str, retry_at: float) -> None:
        """The download failed but is worth another try: its requests go back
        to `queued`, keeping their spot, and wait until `retry_at` (unix time).
        `error` is shown on the phone meanwhile."""
        with self.lock:
            attempts = self.download_attempts(video_id) + 1
            self.conn.execute(
                "UPDATE queue SET state = 'queued', error = ?, attempts = ?, retry_at = ? "
                "WHERE video_id = ? AND state IN ('queued','downloading')",
                (error, attempts, retry_at, video_id),
            )

    # --- playback -----------------------------------------------------------

    def _start_next(self) -> dict | None:
        """Advance rule: the first `ready` one by position. Songs still
        downloading keep their spot. With nothing ready, the screen waits."""
        nxt = self.next_ready()
        if nxt is None:
            return None
        self.conn.execute(
            "UPDATE queue SET state = 'playing', played_at = datetime('now') WHERE id = ?",
            (nxt["id"],),
        )
        self.conn.execute(
            "UPDATE songs SET play_count = play_count + 1, last_used_at = datetime('now') "
            "WHERE video_id = ?",
            (nxt["video_id"],),
        )
        return self.get(nxt["id"])

    def start_if_idle(self) -> dict | None:
        """If nothing is playing and something is ready, starts it. Returns what started."""
        with self.lock, db.transaction(self.conn):
            if self.current() is not None:
                return None
            return self._start_next()

    def advance(self, expected_id: int | None = None) -> dict | None:
        """Finishes the current song (`played`) and starts the next ready one.

        `expected_id` guards against a duplicate or late `ended`: if that is no
        longer the song playing, nothing happens.
        """
        with self.lock, db.transaction(self.conn):
            cur = self.current()
            if cur is not None:
                if expected_id is not None and cur["id"] != expected_id:
                    return None
                self.conn.execute("UPDATE queue SET state = 'played' WHERE id = ?", (cur["id"],))
            elif expected_id is not None:
                return None
            return self._start_next()

    def fail_current(self, error: str, expected_id: int | None = None) -> dict | None:
        """The screen couldn't play it: mark `failed` and advance."""
        with self.lock, db.transaction(self.conn):
            cur = self.current()
            if cur is None or (expected_id is not None and cur["id"] != expected_id):
                return None
            self.conn.execute(
                "UPDATE queue SET state = 'failed', error = ? WHERE id = ?", (error, cur["id"])
            )
            return self._start_next()

    # --- editing ------------------------------------------------------------

    def remove(self, item_id: int, session: dict) -> bool:
        """Removes a request (your own, or any if admin).

        Returns True if the playing song was removed (the next one has then started).
        """
        with self.lock, db.transaction(self.conn):
            item = self.get(item_id)
            if item is None or item["state"] in ("played", "removed"):
                raise NotFound(f"request {item_id} does not exist")
            if not session.get("is_admin") and item["session_id"] != session["id"]:
                raise Forbidden("you can only remove your own songs")
            self.conn.execute("UPDATE queue SET state = 'removed' WHERE id = ?", (item_id,))
            if item["state"] == "playing":
                self._start_next()
                return True
            return False

    def reorder(self, ordered_ids: list[int]) -> None:
        """Reassigns positions of pending requests in the given order (admin only).

        Pending requests missing from the list stay at the end in their current
        order, so a stale list never loses songs.
        """
        with self.lock, db.transaction(self.conn):
            pending = [i["id"] for i in self.pending()]
            pending_set = set(pending)
            order = [i for i in dict.fromkeys(ordered_ids) if i in pending_set]
            given = set(order)
            order += [i for i in pending if i not in given]
            base = self.conn.execute(
                "SELECT COALESCE(MIN(position), 1) FROM queue WHERE state IN ('queued','downloading','ready')"
            ).fetchone()[0]
            for offset, item_id in enumerate(order):
                self.conn.execute(
                    "UPDATE queue SET position = ? WHERE id = ?", (base + offset, item_id)
                )

    def move(self, item_id: int, delta: int) -> None:
        """Moves a pending request up (delta < 0) or down (delta > 0)."""
        with self.lock:
            ids = [i["id"] for i in self.pending()]
            if item_id not in ids:
                raise NotFound(f"not in the queue: {item_id}")
            idx = ids.index(item_id)
            new = max(0, min(len(ids) - 1, idx + delta))
            ids.insert(new, ids.pop(idx))
            self.reorder(ids)

    def clear(self) -> None:
        """Clears pending and failed requests. The current song keeps playing."""
        with self.lock:
            self.conn.execute(
                "UPDATE queue SET state = 'removed' "
                "WHERE state IN ('queued','downloading','ready','failed')"
            )

    def new_party(self) -> None:
        """Starts from zero: nothing playing, nothing queued, no failures left
        over from last time. Sessions (guests' names) and the downloaded
        library stay."""
        with self.lock:
            self.conn.execute(
                "UPDATE queue SET state = 'removed' "
                "WHERE state IN ('queued','downloading','ready','playing','failed')"
            )

    def recover(self) -> None:
        """On startup: interrupted downloads go back to the queue."""
        with self.lock:
            self.conn.execute("UPDATE queue SET state = 'queued' WHERE state = 'downloading'")
