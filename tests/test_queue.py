import sqlite3

import pytest

from app import db
from app.queue import RETRY_DELAYS_S, Forbidden, NotFound, Queue, bot_hold, download_budget, retry_delay


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


# --- download policy ---------------------------------------------------------

INTERVAL = 90.0


@pytest.mark.parametrize(
    "playing,ready,active,since_last,expected",
    [
        # Nothing playing: someone is waiting, right away and up to 2 at a time.
        (False, 0, 0, 5.0, (2, None)),
        (False, 5, 0, 5.0, (2, None)),
        (False, 0, 1, 5.0, (1, None)),
        (False, 0, 2, 5.0, (0, None)),
        # Playing with fewer than 2 ready after it: right away, but only 1.
        (True, 0, 0, 5.0, (1, None)),
        (True, 1, 0, 5.0, (1, None)),
        (True, 1, 1, 5.0, (0, None)),
        # Playing with enough cushion: wait for the interval.
        (True, 2, 0, 5.0, (0, 85.0)),
        (True, 3, 0, 89.0, (0, 1.0)),
        (True, 2, 0, 90.0, (1, None)),
        (True, 2, 0, None, (1, None)),  # no previous download
        (True, 2, 1, 5.0, (0, None)),  # one running: its end checks again
    ],
)
def test_download_budget(playing, ready, active, since_last, expected):
    assert download_budget(playing, ready, active, since_last, INTERVAL) == expected


def test_policy_empty_queue_downloads_now(conn, q, ana):
    """Nothing playing (empty queue): the first request downloads right away,
    even if the previous download just finished."""
    song(conn, "a")
    q.add("a", ana)
    assert q.claim_downloads(since_last=1.0, min_interval=INTERVAL) == ["a"]


def playing_with_ready(conn, q, ana, tmp_path, n_ready):
    """One song playing, `n_ready` cached ones after it and one new one queued."""
    cached_song(conn, "p", tmp_path)
    q.add("p", ana)
    q.start_if_idle()
    for i in range(n_ready):
        cached_song(conn, f"r{i}", tmp_path)
        q.add(f"r{i}", ana)
    song(conn, "new")
    return q.add("new", ana)


def test_policy_enough_cushion_waits_for_the_interval(conn, q, ana, tmp_path):
    item = playing_with_ready(conn, q, ana, tmp_path, n_ready=2)
    assert q.claim_downloads(since_last=10.0, min_interval=INTERVAL) == []
    assert q.get(item["id"])["state"] == "queued"
    assert q.download_budget(since_last=10.0, min_interval=INTERVAL) == (0, 80.0)
    # Once the interval has passed, it goes.
    assert q.claim_downloads(since_last=INTERVAL, min_interval=INTERVAL) == ["new"]


def test_policy_low_cushion_downloads_now_despite_the_interval(conn, q, ana, tmp_path):
    item = playing_with_ready(conn, q, ana, tmp_path, n_ready=1)
    assert q.claim_downloads(since_last=1.0, min_interval=INTERVAL) == ["new"]
    assert q.get(item["id"])["state"] == "downloading"


def test_policy_one_download_at_a_time_while_playing(conn, q, ana, tmp_path):
    playing_with_ready(conn, q, ana, tmp_path, n_ready=0)
    song(conn, "new2")
    q.add("new2", ana)
    assert q.claim_downloads(min_interval=INTERVAL) == ["new"]
    assert q.claim_downloads(min_interval=INTERVAL) == []
    q.mark_download_failed("new", "x")
    assert q.claim_downloads(min_interval=INTERVAL) == ["new2"]


def test_policy_cached_requests_become_ready_even_while_waiting(conn, q, ana, tmp_path):
    playing_with_ready(conn, q, ana, tmp_path, n_ready=2)
    song(conn, "dup")
    item = q.add("dup", ana)
    f = tmp_path / "dup.mp4"
    f.write_bytes(b"x")
    db.set_song_file(conn, "dup", str(f))  # downloaded by another request meanwhile
    assert q.claim_downloads(since_last=1.0, min_interval=INTERVAL) == []
    assert q.get(item["id"])["state"] == "ready"


def test_nothing_queued_nothing_to_wait_for(conn, q, ana, tmp_path):
    cached_song(conn, "p", tmp_path)
    q.add("p", ana)
    q.start_if_idle()
    assert q.download_budget(since_last=1.0, min_interval=INTERVAL) == (0, None)


# --- automatic retries --------------------------------------------------------

@pytest.mark.parametrize(
    "kind,attempts,expected",
    [
        ("bot", 0, 300), ("bot", 3, 1200), ("bot", 4, None),
        ("other", 0, 60), ("other", 2, 600), ("other", 3, None),
        ("offline", 0, 60), ("offline", 3, None),
        # Trying again wouldn't change anything.
        ("unavailable", 0, None), ("geo", 0, None), ("format", 0, None),
    ],
)
def test_retry_delay(kind, attempts, expected):
    assert retry_delay(kind, attempts) == expected


def test_bot_hold_grows_and_stays_at_the_longest():
    assert [bot_hold(n) for n in range(6)] == [300, 600, 900, 1200, 1200, 1200]
    assert max(RETRY_DELAYS_S["bot"]) == 1200


def test_retry_goes_back_to_queued_and_keeps_its_spot(conn, q, ana, beto):
    first = q.add(song(conn, "a"), ana)
    second = q.add(song(conn, "b"), beto)
    assert q.claim_downloads(now=1000) == ["a", "b"]
    q.retry_download("a", "will retry", retry_at=1300)
    item = q.get(first["id"])
    assert (item["state"], item["error"], item["attempts"], item["retry_at"]) == (
        "queued", "will retry", 1, 1300,
    )
    assert [i["id"] for i in q.pending()] == [first["id"], second["id"]]
    assert q.failed() == []


def test_retry_is_not_claimed_before_it_is_due(conn, q, ana):
    item = q.add(song(conn, "a"), ana)
    q.claim_downloads(now=1000)
    q.retry_download("a", "will retry", retry_at=1300)
    assert q.claim_downloads(now=1299) == []
    assert q.download_budget(now=1299) == (0, None)
    assert q.next_retry_wait(now=1200) == 100
    assert q.claim_downloads(now=1300) == ["a"]
    assert q.next_retry_wait(now=1300) is None
    item = q.get(item["id"])
    assert item["state"] == "downloading" and item["retry_at"] is None and item["attempts"] == 1


def test_a_waiting_retry_does_not_block_the_songs_behind_it(conn, q, ana, beto):
    q.add(song(conn, "a"), ana)
    q.claim_downloads(now=1000)
    q.retry_download("a", "will retry", retry_at=1300)
    q.add(song(conn, "b"), beto)
    assert q.claim_downloads(now=1001) == ["b"]


def test_attempts_add_up_across_retries(conn, q, ana):
    q.add(song(conn, "a"), ana)
    for n in (1, 2, 3):
        assert q.claim_downloads(now=n * 1000) == ["a"]
        q.retry_download("a", "will retry", retry_at=n * 1000 + 1)
        assert q.download_attempts("a") == n


def test_hold_starts_nothing_but_still_readies_what_is_cached(conn, q, ana, beto, tmp_path):
    waiting = q.add(song(conn, "a"), ana)
    q.add(song(conn, "b"), beto)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    db.set_song_file(conn, "a", str(f))
    assert q.claim_downloads(now=1000, hold=True) == []
    assert q.get(waiting["id"])["state"] == "ready"
    assert [i["state"] for i in q.pending()] == ["ready", "queued"]


def test_downloaded_or_failed_clears_the_retry(conn, q, ana, beto, tmp_path):
    a = q.add(song(conn, "a"), ana)
    b = q.add(song(conn, "b"), beto)
    q.claim_downloads(now=1000)
    q.retry_download("a", "will retry", retry_at=1300)
    q.retry_download("b", "will retry", retry_at=1300)
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    q.mark_downloaded("a", str(f))
    q.mark_download_failed("b", "gave up")
    assert (q.get(a["id"])["state"], q.get(a["id"])["error"], q.get(a["id"])["retry_at"]) == ("ready", None, None)
    assert (q.get(b["id"])["state"], q.get(b["id"])["error"], q.get(b["id"])["retry_at"]) == ("failed", "gave up", None)


def test_old_database_gets_the_retry_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE songs (video_id TEXT PRIMARY KEY, title TEXT NOT NULL, channel TEXT,
            duration_s INTEGER, file_path TEXT, thumb_url TEXT, downloaded_at TEXT,
            play_count INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE queue (id INTEGER PRIMARY KEY, video_id TEXT NOT NULL, session_id TEXT,
            requested_by TEXT NOT NULL, state TEXT NOT NULL, position INTEGER NOT NULL,
            error TEXT, added_at TEXT NOT NULL DEFAULT (datetime('now')), played_at TEXT);
        INSERT INTO songs (video_id, title) VALUES ('a', 'A');
        INSERT INTO queue (video_id, requested_by, state, position) VALUES ('a', 'Ana', 'queued', 1);
    """)
    old.close()
    c = db.connect(path)
    item = Queue(c).pending()[0]
    assert item["attempts"] == 0 and item["retry_at"] is None
    c.close()


# --- recently played ----------------------------------------------------------

def test_recent_is_the_last_five_played_newest_first(conn, q, ana, tmp_path):
    for n in range(7):
        vid = f"s{n}"
        song(conn, vid)
        f = tmp_path / f"{vid}.mp4"
        f.write_bytes(b"x")
        db.set_song_file(conn, vid, str(f))
        q.add(vid, ana)
    q.start_if_idle()
    for _ in range(6):
        q.advance()
    # s0..s5 played, s6 is playing: it isn't "played" yet.
    assert [i["video_id"] for i in q.recent()] == ["s5", "s4", "s3", "s2", "s1"]
    assert [i["video_id"] for i in q.snapshot()["recent"]] == ["s5", "s4", "s3", "s2", "s1"]
    assert q.recent()[0]["requested_by"] == "Ana"


def test_recent_leaves_out_removed_and_failed(conn, q, ana, tmp_path):
    for vid in ("a", "b", "c"):
        song(conn, vid)
        f = tmp_path / f"{vid}.mp4"
        f.write_bytes(b"x")
        db.set_song_file(conn, vid, str(f))
        q.add(vid, ana)
    q.start_if_idle()
    q.advance()                      # a played, b playing
    q.fail_current("broken")         # b failed, c playing
    q.remove(q.current()["id"], ana)  # c removed
    assert [i["video_id"] for i in q.recent()] == ["a"]
