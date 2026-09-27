"""Library size limit tests: least recently used videos leave first."""

import sqlite3
from pathlib import Path

import pytest

from app import db, library
from app.main import Config, parse_max_gb
from app.queue import Queue


@pytest.fixture
def conn():
    return db.connect(":memory:")


@pytest.fixture
def media(tmp_path):
    d = tmp_path / "media"
    d.mkdir()
    return d


@pytest.fixture
def session(conn):
    return db.create_session(conn, "Ana")


def song(conn, media, vid, size, last_used):
    """A cached song of `size` bytes, last used on `last_used` (SQLite datetime)."""
    db.upsert_song(conn, vid, f"Song {vid}")
    f = media / f"{vid}.mp4"
    f.write_bytes(b"x" * size)
    db.set_song_file(conn, vid, str(f))
    conn.execute("UPDATE songs SET last_used_at = ? WHERE video_id = ?", (last_used, vid))
    return f


def names(media):
    return sorted(p.name for p in media.iterdir())


def test_under_the_limit_nothing_is_deleted(conn, media):
    song(conn, media, "aaaaaaaaaaa", 100, "2026-01-01 00:00:00")
    assert library.enforce_limit(conn, media, 100) == []
    assert names(media) == ["aaaaaaaaaaa.mp4"]


def test_deletes_least_recently_used_until_it_fits(conn, media):
    song(conn, media, "aaaaaaaaaaa", 100, "2026-03-01 00:00:00")  # newest
    song(conn, media, "bbbbbbbbbbb", 100, "2026-01-01 00:00:00")  # oldest
    song(conn, media, "ccccccccccc", 100, "2026-02-01 00:00:00")
    song(conn, media, "ddddddddddd", 100, "2026-02-15 00:00:00")
    assert library.enforce_limit(conn, media, 250) == ["bbbbbbbbbbb", "ccccccccccc"]
    assert names(media) == ["aaaaaaaaaaa.mp4", "ddddddddddd.mp4"]
    # The song stays known, just not cached: asking for it again downloads it.
    row = db.get_song(conn, "bbbbbbbbbbb")
    assert row["title"] == "Song bbbbbbbbbbb" and row["file_path"] is None
    assert db.cached_file(conn, "bbbbbbbbbbb") is None
    assert db.cached_file(conn, "aaaaaaaaaaa")


def test_requesting_or_playing_a_song_keeps_it(conn, media, session):
    song(conn, media, "aaaaaaaaaaa", 100, "2026-01-01 00:00:00")  # old, but requested again now
    song(conn, media, "bbbbbbbbbbb", 100, "2026-02-01 00:00:00")
    song(conn, media, "ccccccccccc", 100, "2026-03-01 00:00:00")  # old, played now
    q = Queue(conn)
    item = q.add("aaaaaaaaaaa", session)
    q.start_if_idle()
    q.advance(expected_id=item["id"])  # played and gone from the queue
    conn.execute("UPDATE songs SET last_used_at = '2026-01-01 00:00:00' WHERE video_id = 'ccccccccccc'")
    q.add("ccccccccccc", session)
    q.start_if_idle()
    q.advance()
    assert library.enforce_limit(conn, media, 200) == ["bbbbbbbbbbb"]


def test_active_requests_are_never_deleted(conn, media, session):
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"):
        song(conn, media, vid, 100, "2026-01-01 00:00:00")
    q = Queue(conn)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
        q.add(vid, session)
    q.start_if_idle()  # a: playing; b, c: ready
    conn.execute("UPDATE queue SET state = 'downloading' WHERE video_id = 'ccccccccccc'")
    conn.execute("UPDATE songs SET last_used_at = '2025-01-01 00:00:00'")  # all equally old
    assert library.enforce_limit(conn, media, 0 + 1) == ["ddddddddddd"]
    assert names(media) == ["aaaaaaaaaaa.mp4", "bbbbbbbbbbb.mp4", "ccccccccccc.mp4"]


def test_files_that_are_not_songs_count_but_are_never_deleted(conn, media):
    (media / "notes.txt").write_bytes(b"x" * 300)
    (media / "eeeeeeeeeee.f137.mp4.part").write_bytes(b"x" * 50)
    song(conn, media, "aaaaaaaaaaa", 100, "2026-01-01 00:00:00")
    assert library.enforce_limit(conn, media, 400) == ["aaaaaaaaaaa"]
    assert names(media) == ["eeeeeeeeeee.f137.mp4.part", "notes.txt"]


def test_never_deletes_outside_the_media_folder(conn, media, tmp_path):
    outside = tmp_path / "precious.mp4"
    outside.write_bytes(b"x" * 100)
    db.upsert_song(conn, "aaaaaaaaaaa", "A")
    db.set_song_file(conn, "aaaaaaaaaaa", str(media / ".." / "precious.mp4"))
    song(conn, media, "bbbbbbbbbbb", 100, "2026-01-01 00:00:00")
    library.enforce_limit(conn, media, 1)
    assert outside.is_file()


def test_missing_file_just_fixes_the_database(conn, media):
    song(conn, media, "aaaaaaaaaaa", 100, "2026-01-01 00:00:00").unlink()
    song(conn, media, "bbbbbbbbbbb", 100, "2026-02-01 00:00:00")
    song(conn, media, "ccccccccccc", 100, "2026-03-01 00:00:00")
    assert library.enforce_limit(conn, media, 150) == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    assert db.get_song(conn, "aaaaaaaaaaa")["file_path"] is None


def test_zero_means_no_limit(conn, media):
    song(conn, media, "aaaaaaaaaaa", 100, "2026-01-01 00:00:00")
    assert library.enforce_limit(conn, media, 0) == []


@pytest.mark.parametrize(
    "value,expected",
    [
        ("", 150 * library.GB),
        ("150", 150 * library.GB),
        ("100", 100 * library.GB),
        ("0.5", 500_000_000),
        ("0", 0),
        ("lots", 150 * library.GB),
        ("-5", 150 * library.GB),
        ("nan", 150 * library.GB),
    ],
)
def test_parse_max_gb(value, expected):
    assert parse_max_gb(value) == expected


def test_limit_from_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_MEDIA_MAX_GB", "100")
    assert Config.from_env().media_max_bytes == 100 * library.GB
    monkeypatch.delenv("KARAOKE_MEDIA_MAX_GB")
    assert Config.from_env().media_max_bytes == 150 * library.GB


def test_old_database_gets_last_used_backfilled(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        Path(db.SCHEMA_PATH).read_text()
        .replace("    last_used_at TEXT,", "")
        .replace("    query         TEXT,", "")
        .replace("    search_mode   TEXT,", "")
    )
    old.executemany(
        "INSERT INTO songs (video_id, title, downloaded_at) VALUES (?, ?, ?)",
        [("aaaaaaaaaaa", "A", "2025-01-01 00:00:00"), ("bbbbbbbbbbb", "B", "2025-01-01 00:00:00")],
    )
    old.execute(
        "INSERT INTO queue (video_id, requested_by, state, position, added_at, played_at) "
        "VALUES ('aaaaaaaaaaa', 'x', 'played', 1, '2026-05-01 20:00:00', '2026-05-01 21:00:00')"
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    last = dict(conn.execute("SELECT video_id, last_used_at FROM songs").fetchall())
    assert last == {"aaaaaaaaaaa": "2026-05-01 21:00:00", "bbbbbbbbbbb": "2025-01-01 00:00:00"}
