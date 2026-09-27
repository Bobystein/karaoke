import sqlite3

import pytest

from app import db
from app.queue import Forbidden, NotFound, Queue


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def q(conn):
    return Queue(conn)


@pytest.fixture
def ana(conn):
    return db.create_session(conn, "Ana")


@pytest.fixture
def beto(conn):
    return db.create_session(conn, "Beto")


@pytest.fixture
def admin(conn):
    s = db.create_session(conn, "Admin")
    db.set_admin(conn, s["id"])
    return db.get_session(conn, s["id"])


def song(conn, vid):
    db.upsert_song(conn, vid, f"Song {vid}", "Channel", 200, None)
    return vid


def cached_song(conn, vid, tmp_path):
    song(conn, vid)
    f = tmp_path / f"{vid}.mp4"
    f.write_bytes(b"x")
    db.set_song_file(conn, vid, str(f))
    return vid


def states(q):
    return {i["video_id"]: i["state"] for i in q._items("1=1")}


def playing_count(conn):
    return conn.execute("SELECT COUNT(*) FROM queue WHERE state = 'playing'").fetchone()[0]


# --- invariant: only one playing --------------------------------------------

def test_never_two_playing(conn, q, ana, tmp_path):
    for v in "abcd":
        cached_song(conn, v, tmp_path)
        q.add(v, ana)
    q.start_if_idle()
    assert playing_count(conn) == 1
    # start_if_idle with something playing doesn't start another
    assert q.start_if_idle() is None
    assert playing_count(conn) == 1
    for _ in range(6):
        q.advance()
        assert playing_count(conn) <= 1


def test_db_rejects_second_playing(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    cached_song(conn, "b", tmp_path)
    q.add("a", ana)
    b = q.add("b", ana)
    q.start_if_idle()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE queue SET state = 'playing' WHERE id = ?", (b["id"],))


def test_advance_marks_played_and_counts(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    item = q.add("a", ana)
    q.start_if_idle()
    q.advance()
    assert q.get(item["id"])["state"] == "played"
    assert db.get_song(conn, "a")["play_count"] == 1


def test_stale_ended_is_ignored(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    cached_song(conn, "b", tmp_path)
    a = q.add("a", ana)
    b = q.add("b", ana)
    q.start_if_idle()
    assert q.advance(expected_id=a["id"])["id"] == b["id"]
    # a second `ended` for song a must not skip b
    assert q.advance(expected_id=a["id"]) is None
    assert q.current()["id"] == b["id"]


# --- advance rule ------------------------------------------------------------

def test_skips_downloading_and_keeps_position(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    song(conn, "b")
    cached_song(conn, "c", tmp_path)
    a = q.add("a", ana)
    b = q.add("b", ana)
    c = q.add("c", ana)
    q.claim_downloads()
    assert q.get(b["id"])["state"] == "downloading"

    q.start_if_idle()
    assert q.current()["id"] == a["id"]
    nxt = q.advance()
    assert nxt["id"] == c["id"]  # skipped b, which is still downloading
    b_now = q.get(b["id"])
    assert b_now["state"] == "downloading"
    assert b_now["position"] == b["position"]
    assert q.pending()[0]["id"] == b["id"]  # still first in the queue

    f = tmp_path / "b.mp4"
    f.write_bytes(b"x")
    q.mark_downloaded("b", str(f))
    assert q.advance()["id"] == b["id"]


def test_empty_queue_goes_to_waiting(q):
    assert q.advance() is None
    snap = q.snapshot()
    assert snap["mode"] == "waiting"
    assert snap["current"] is None


def test_nothing_ready_goes_to_waiting(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    song(conn, "b")
    q.add("a", ana)
    q.add("b", ana)
    q.claim_downloads()
    q.start_if_idle()
    assert q.snapshot()["mode"] == "playing"
    assert q.advance() is None
    snap = q.snapshot()
    assert snap["mode"] == "waiting"
    assert [i["video_id"] for i in snap["queue"]] == ["b"]
    assert snap["queue"][0]["state"] == "downloading"


def test_ready_while_idle_starts(conn, q, ana, tmp_path):
    song(conn, "a")
    q.add("a", ana)
    q.claim_downloads()
    assert q.start_if_idle() is None
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    q.mark_downloaded("a", str(f))
    assert q.start_if_idle()["video_id"] == "a"


def test_screen_error_fails_and_advances(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    cached_song(conn, "b", tmp_path)
    a = q.add("a", ana)
    q.add("b", ana)
    q.start_if_idle()
    nxt = q.fail_current("MEDIA_ERR_DECODE", expected_id=a["id"])
    assert nxt["video_id"] == "b"
    failed = q.get(a["id"])
    assert failed["state"] == "failed"
    assert failed["error"] == "MEDIA_ERR_DECODE"


# --- cache and downloads -----------------------------------------------------

def test_cached_song_goes_straight_to_ready(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    item = q.add("a", ana)
    assert item["state"] == "ready"
    assert q.claim_downloads() == []


def test_cache_entry_without_file_is_not_cached(conn, q, ana, tmp_path):
    song(conn, "a")
    db.set_song_file(conn, "a", str(tmp_path / "deleted.mp4"))
    assert q.add("a", ana)["state"] == "queued"


def test_at_most_two_downloads(conn, q, ana):
    for v in "abcd":
        song(conn, v)
        q.add(v, ana)
    assert q.claim_downloads() == ["a", "b"]
    assert q.claim_downloads() == []
    q.mark_download_failed("a", "no network")
    assert q.claim_downloads() == ["c"]
    assert states(q) == {"a": "failed", "b": "downloading", "c": "downloading", "d": "queued"}


def test_same_video_twice_downloads_once(conn, q, ana, beto, tmp_path):
    song(conn, "a")
    x = q.add("a", ana)
    y = q.add("a", beto)
    assert q.claim_downloads() == ["a"]
    assert q.get(y["id"])["state"] == "downloading"
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    q.mark_downloaded("a", str(f))
    assert q.get(x["id"])["state"] == "ready"
    assert q.get(y["id"])["state"] == "ready"


def test_download_failure_keeps_reason(conn, q, ana):
    song(conn, "a")
    item = q.add("a", ana)
    q.claim_downloads()
    q.mark_download_failed("a", "format not playable: vp9")
    got = q.get(item["id"])
    assert got["state"] == "failed"
    assert got["error"] == "format not playable: vp9"
    assert q.snapshot()["failed"][0]["id"] == item["id"]


def test_recover_requeues_interrupted_downloads(conn, q, ana):
    song(conn, "a")
    q.add("a", ana)
    q.claim_downloads()
    q.recover()
    assert states(q) == {"a": "queued"}
    assert q.claim_downloads() == ["a"]


def test_unknown_song_rejected(q, ana):
    with pytest.raises(NotFound):
        q.add("nope", ana)


# --- remove, permissions, reorder, clear -------------------------------------

def test_user_removes_own(conn, q, ana):
    song(conn, "a")
    item = q.add("a", ana)
    q.remove(item["id"], ana)
    assert q.get(item["id"])["state"] == "removed"  # the row isn't deleted


def test_user_cannot_remove_others(conn, q, ana, beto):
    song(conn, "a")
    item = q.add("a", ana)
    with pytest.raises(Forbidden):
        q.remove(item["id"], beto)
    assert q.get(item["id"])["state"] == "queued"


def test_admin_removes_any(conn, q, ana, admin):
    song(conn, "a")
    item = q.add("a", ana)
    q.remove(item["id"], admin)
    assert q.get(item["id"])["state"] == "removed"


def test_removing_playing_advances(conn, q, ana, admin, tmp_path):
    cached_song(conn, "a", tmp_path)
    cached_song(conn, "b", tmp_path)
    a = q.add("a", ana)
    q.add("b", ana)
    q.start_if_idle()
    assert q.remove(a["id"], admin) is True
    assert q.current()["video_id"] == "b"
    assert playing_count(conn) == 1


def test_remove_played_is_not_found(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    a = q.add("a", ana)
    q.start_if_idle()
    q.advance()
    with pytest.raises(NotFound):
        q.remove(a["id"], ana)


def test_reorder(conn, q, ana):
    ids = []
    for v in "abc":
        song(conn, v)
        ids.append(q.add(v, ana)["id"])
    q.reorder([ids[2], ids[0], ids[1]])
    assert [i["video_id"] for i in q.pending()] == ["c", "a", "b"]


def test_reorder_partial_list_keeps_missing(conn, q, ana):
    ids = []
    for v in "abc":
        song(conn, v)
        ids.append(q.add(v, ana)["id"])
    q.reorder([ids[2], 9999])
    assert [i["video_id"] for i in q.pending()] == ["c", "a", "b"]


def test_move_up_and_down(conn, q, ana):
    ids = []
    for v in "abc":
        song(conn, v)
        ids.append(q.add(v, ana)["id"])
    q.move(ids[2], -1)
    assert [i["video_id"] for i in q.pending()] == ["a", "c", "b"]
    q.move(ids[0], +5)
    assert [i["video_id"] for i in q.pending()] == ["c", "b", "a"]


def test_clear_keeps_playing(conn, q, ana, tmp_path):
    cached_song(conn, "a", tmp_path)
    song(conn, "b")
    q.add("a", ana)
    q.add("b", ana)
    q.start_if_idle()
    q.clear()
    assert q.pending() == []
    assert q.current()["video_id"] == "a"
    assert q.advance() is None
