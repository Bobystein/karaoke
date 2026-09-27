"""SQLite connection and helpers for sessions and songs. No ORM."""

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_DB_PATH = ROOT / "data" / "karaoke.db"


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Opens the database and applies the schema. Autocommit; transactions go through `transaction()`."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_PATH.read_text())
    migrate(conn)
    return conn


# Columns added after the first release. CREATE TABLE IF NOT EXISTS doesn't
# touch an existing table, so they are added here, with an optional statement
# that fills them in for the rows that already existed.
ADDED_COLUMNS = {
    "queue": [("query", "TEXT", None), ("search_mode", "TEXT", None)],
    "songs": [
        (
            "last_used_at",
            "TEXT",
            """UPDATE songs SET last_used_at = COALESCE(
                   (SELECT MAX(COALESCE(q.played_at, q.added_at)) FROM queue q
                    WHERE q.video_id = songs.video_id),
                   downloaded_at)""",
        ),
    ],
}


def migrate(conn: sqlite3.Connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl, backfill in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                if backfill:
                    conn.execute(backfill)


@contextmanager
def transaction(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- sessions ---------------------------------------------------------------

def create_session(conn: sqlite3.Connection, name: str) -> dict:
    sid = str(uuid.uuid4())
    conn.execute("INSERT INTO sessions (id, name) VALUES (?, ?)", (sid, name))
    return get_session(conn, sid)


def get_session(conn: sqlite3.Connection, session_id: str | None) -> dict | None:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return dict(row) if row else None


def rename_session(conn: sqlite3.Connection, session_id: str, name: str) -> None:
    """Changes the name, also on the requests still in the queue (or failed), so
    everyone sees the new one. Songs already played keep the name they had."""
    with transaction(conn):
        conn.execute("UPDATE sessions SET name = ? WHERE id = ?", (name, session_id))
        conn.execute(
            "UPDATE queue SET requested_by = ? WHERE session_id = ? "
            "AND state IN ('queued','downloading','ready','playing','failed')",
            (name, session_id),
        )


def set_admin(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET is_admin = 1 WHERE id = ?", (session_id,))


# --- settings ---------------------------------------------------------------

def get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --- songs ------------------------------------------------------------------

def upsert_song(
    conn: sqlite3.Connection,
    video_id: str,
    title: str,
    channel: str | None = None,
    duration_s: int | None = None,
    thumb_url: str | None = None,
) -> None:
    """Records a video's metadata. Doesn't touch file_path or play_count."""
    conn.execute(
        """
        INSERT INTO songs (video_id, title, channel, duration_s, thumb_url)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title,
            channel = COALESCE(excluded.channel, songs.channel),
            duration_s = COALESCE(excluded.duration_s, songs.duration_s),
            thumb_url = COALESCE(excluded.thumb_url, songs.thumb_url)
        """,
        (video_id, title, channel, duration_s, thumb_url),
    )


def get_song(conn: sqlite3.Connection, video_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM songs WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


def touch_song(conn: sqlite3.Connection, video_id: str) -> None:
    """Marks the song as just used (requested or played), for the library's eviction order."""
    conn.execute("UPDATE songs SET last_used_at = datetime('now') WHERE video_id = ?", (video_id,))


def forget_file(conn: sqlite3.Connection, video_id: str) -> None:
    """The file was deleted: the song stays (metadata, play count) but isn't cached anymore."""
    conn.execute(
        "UPDATE songs SET file_path = NULL, downloaded_at = NULL WHERE video_id = ?", (video_id,)
    )


def set_song_file(conn: sqlite3.Connection, video_id: str, file_path: str) -> None:
    conn.execute(
        "UPDATE songs SET file_path = ?, downloaded_at = datetime('now') WHERE video_id = ?",
        (file_path, video_id),
    )


def cached_file(conn: sqlite3.Connection, video_id: str) -> str | None:
    """File path if the song is cached and the file exists on disk."""
    song = get_song(conn, video_id)
    if song and song["file_path"] and Path(song["file_path"]).is_file():
        return song["file_path"]
    return None
