"""Conexion a SQLite y helpers de sessions y songs. Sin ORM."""

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
DEFAULT_DB_PATH = ROOT / "data" / "karaoke.db"


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Abre la base y aplica el esquema. Autocommit; las transacciones van con `transaction()`."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


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


def set_admin(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("UPDATE sessions SET is_admin = 1 WHERE id = ?", (session_id,))


# --- songs ------------------------------------------------------------------

def upsert_song(
    conn: sqlite3.Connection,
    video_id: str,
    title: str,
    channel: str | None = None,
    duration_s: int | None = None,
    thumb_url: str | None = None,
) -> None:
    """Registra los metadatos de un video. No toca file_path ni play_count."""
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


def set_song_file(conn: sqlite3.Connection, video_id: str, file_path: str) -> None:
    conn.execute(
        "UPDATE songs SET file_path = ?, downloaded_at = datetime('now') WHERE video_id = ?",
        (file_path, video_id),
    )


def cached_file(conn: sqlite3.Connection, video_id: str) -> str | None:
    """Ruta del archivo si la cancion ya esta en cache y el archivo existe en disco."""
    song = get_song(conn, video_id)
    if song and song["file_path"] and Path(song["file_path"]).is_file():
        return song["file_path"]
    return None
