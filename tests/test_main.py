"""HTTP and WebSocket tests. Permissions are tested by calling the endpoints
directly, not through the UI. Nothing calls YouTube."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, downloader, main
from app.main import COOKIE, Config, create_app, owner_tag, wifi_payload

VID_A = "aaaaaaaaaaa"
VID_B = "bbbbbbbbbbb"
VID_NEW = "nnnnnnnnnnn"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        db_path=tmp_path / "k.db",
        media_dir=tmp_path / "media",
        admin_password="secret",
        wifi_ssid="Home",
        wifi_password="key",
        screen_hosts=frozenset({"testclient"}),
    )


@pytest.fixture
def client(cfg):
    with TestClient(create_app(cfg)) as c:
        yield c


@pytest.fixture
def k(client):
    return client.app.state.k


def as_(session):
    return {"cookie": f"{COOKIE}={session['id']}"}


@pytest.fixture
def ana(k):
    return db.create_session(k.conn, "Ana")


@pytest.fixture
def beto(k):
    return db.create_session(k.conn, "Beto")


@pytest.fixture
def admin(k):
    s = db.create_session(k.conn, "Admin")
    db.set_admin(k.conn, s["id"])
    return db.get_session(k.conn, s["id"])


def cached(k, vid):
    db.upsert_song(k.conn, vid, f"Song {vid}", "Channel", 180, None)
    f = Path(k.cfg.media_dir) / f"{vid}.mp4"
    f.write_bytes(b"0123456789" * 100)
    db.set_song_file(k.conn, vid, str(f))


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# --- session ------------------------------------------------------------------

def test_root_without_session_asks_name(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'action="/session"' in r.text


def test_create_session_sets_long_cookie(client, k):
    r = client.post("/session", data={"name": "  Rober   V "}, follow_redirects=False)
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert COOKIE in cookie and "HttpOnly" in cookie and "Max-Age=" in cookie
    sid = r.cookies[COOKIE]
    assert db.get_session(k.conn, sid)["name"] == "Rober V"


def test_empty_name_rejected(client):
    assert client.post("/session", data={"name": "   "}).status_code == 400


def test_rename_updates_session_and_active_requests(client, k, ana, beto):
    cached(k, VID_A)
    cached(k, VID_B)
    playing = k.queue.add(VID_A, ana)
    k.queue.start_if_idle()
    waiting = k.queue.add(VID_B, ana)
    others = k.queue.add(VID_B, beto)
    k.queue.advance(expected_id=playing["id"])  # the first one is now `played`
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        r = client.post("/session/name", data={"name": "  Ana   María "}, headers=as_(ana))
        assert r.status_code == 200 and r.json()["name"] == "Ana María"
        msg = ws.receive_json()  # everyone sees the new name right away
    assert db.get_session(k.conn, ana["id"])["name"] == "Ana María"
    assert msg["current"]["requested_by"] == "Ana María"
    assert k.queue.get(waiting["id"])["requested_by"] == "Ana María"
    assert k.queue.get(playing["id"])["requested_by"] == "Ana"  # history keeps the old name
    assert k.queue.get(others["id"])["requested_by"] == "Beto"
    assert db.get_session(k.conn, beto["id"])["name"] == "Beto"
    # Still her songs: renaming doesn't change who can remove them.
    assert client.delete(f"/queue/{waiting['id']}", headers=as_(ana)).status_code == 200


def test_rename_validation(client, k, ana):
    assert client.post("/session/name", data={"name": "   "}, headers=as_(ana)).status_code == 400
    assert client.post("/session/name", data={"name": "x"}).status_code == 401
    r = client.post("/session/name", data={"name": "<b>" + "y" * 100}, headers=as_(ana))
    assert r.json()["name"] == ("<b>" + "y" * 100)[:main.MAX_NAME_LEN]
    html = client.get("/", headers=as_(ana)).text
    assert 'id="rename"' in html and "&lt;b&gt;" in html and "<b>yyy" not in html


def test_endpoints_require_session(client):
    assert client.get("/search?q=x").status_code == 401
    assert client.post("/queue", data={"video_id": VID_A}).status_code == 401
    assert client.delete("/queue/1").status_code == 401
    assert client.post("/admin/skip").status_code == 401


# --- search -------------------------------------------------------------------

def test_search_weird_query_and_escaped_results(client, ana, monkeypatch):
    seen = {}

    def fake_search(q, mode):
        seen["q"], seen["mode"] = q, mode
        return [{"video_id": VID_A, "title": "<script>alert(1)</script> ñandú",
                 "channel": "C", "duration_s": 100, "thumb_url": "t"}]

    monkeypatch.setattr(downloader, "search", fake_search)
    q = 'canción "ñoña"; DROP TABLE queue; --'
    r = client.get("/search", params={"q": q, "mode": "youtube"}, headers=as_(ana))
    assert r.status_code == 200
    assert seen == {"q": q, "mode": "youtube"}
    assert "<script>alert" not in r.text
    assert "&lt;script&gt;" in r.text and "ñandú" in r.text


def test_search_error_is_shown_not_500(client, ana, monkeypatch):
    def boom(q, mode):
        raise downloader.DownloadError("no network")

    monkeypatch.setattr(downloader, "search", boom)
    r = client.get("/search?q=x", headers=as_(ana))
    assert r.status_code == 200
    assert "no network" in r.text


# --- links --------------------------------------------------------------------

LINK_RESULT = {"video_id": VID_NEW, "title": "From a link", "channel": "C",
               "duration_s": 200, "thumb_url": "t"}


@pytest.mark.parametrize("q", [f"https://youtu.be/{VID_NEW}", f"https://www.youtube.com/watch?v={VID_NEW}&t=3"])
def test_link_is_resolved_not_searched(client, k, ana, monkeypatch, q):
    monkeypatch.setattr(downloader, "search", lambda *a: pytest.fail("must not search"))
    seen = []
    monkeypatch.setattr(downloader, "resolve", lambda vid: seen.append(vid) or LINK_RESULT)
    r = client.get("/search", params={"q": q, "mode": "lyrics"}, headers=as_(ana))
    assert seen == [VID_NEW]
    assert r.text.count("<article") == 1 and "From a link" in r.text
    # Ready to queue, without keeping the link as the query to retry with.
    r = client.post("/queue", data={"video_id": VID_NEW, "q": q, "mode": "lyrics"}, headers=as_(ana))
    assert r.status_code == 201
    item = k.queue.get(int(r.headers["x-queue-id"]))
    assert item["query"] is None and item["search_mode"] == "lyrics"


def test_fake_youtube_link_is_just_searched(client, ana, monkeypatch):
    monkeypatch.setattr(downloader, "resolve", lambda vid: pytest.fail("must not resolve"))
    seen = []
    monkeypatch.setattr(downloader, "search", lambda q, mode: seen.append(q) or [])
    q = f"https://www.youtube.com.evil.net/watch?v={VID_NEW}"
    client.get("/search", params={"q": q}, headers=as_(ana))
    assert seen == [q]


def test_bare_word_that_is_not_a_video_falls_back_to_search(client, ana, monkeypatch):
    def not_found(vid):
        raise downloader.DownloadError(downloader.MESSAGES["unavailable"])

    monkeypatch.setattr(downloader, "resolve", not_found)
    seen = []
    monkeypatch.setattr(downloader, "search", lambda q, mode: seen.append((q, mode)) or [])
    r = client.get("/search", params={"q": "Bittersweet", "mode": "karaoke"}, headers=as_(ana))
    assert seen == [("Bittersweet", "karaoke")]
    assert downloader.MESSAGES["unavailable"] not in r.text


def test_broken_link_shows_readable_error(client, ana, monkeypatch):
    def not_found(vid):
        raise downloader.DownloadError(downloader.MESSAGES["unavailable"])

    monkeypatch.setattr(downloader, "resolve", not_found)
    monkeypatch.setattr(downloader, "search", lambda *a: pytest.fail("must not search"))
    r = client.get("/search", params={"q": f"https://youtu.be/{VID_NEW}"}, headers=as_(ana))
    assert r.status_code == 200
    assert "no longer available" in r.text


def test_long_link_video_is_rejected_like_search(client, ana, monkeypatch):
    def too_long(vid):
        raise downloader.TooLong("That video is longer than 15 minutes; it doesn't look like a song.")

    monkeypatch.setattr(downloader, "resolve", too_long)
    monkeypatch.setattr(downloader, "search", lambda *a: pytest.fail("must not search"))
    r = client.get("/search", params={"q": VID_NEW}, headers=as_(ana))
    assert "15 minutes" in r.text and "<article" not in r.text


# --- queue --------------------------------------------------------------------

def test_add_requires_known_song(client, ana):
    r = client.post("/queue", data={"video_id": VID_NEW}, headers=as_(ana))
    assert r.status_code == 404
    r = client.post("/queue", data={"video_id": "../../etc"}, headers=as_(ana))
    assert r.status_code == 400


def test_add_cached_song_starts_playing_without_download(client, k, ana, monkeypatch):
    monkeypatch.setattr(downloader, "download", lambda *a, **kw: pytest.fail("must not download"))
    cached(k, VID_A)
    r = client.post("/queue", data={"video_id": VID_A}, headers=as_(ana))
    assert r.status_code == 201
    assert k.queue.current()["video_id"] == VID_A


def test_add_from_search_downloads_then_plays(client, k, ana, monkeypatch):
    k.remember_results([{"video_id": VID_NEW, "title": "New", "channel": "C",
                         "duration_s": 100, "thumb_url": "t"}])

    def fake_download(video_id, media_dir, on_progress):
        on_progress(50.0)
        p = Path(media_dir) / f"{video_id}.mp4"
        p.write_bytes(b"x")
        return {"file_path": str(p), "cached": False}

    monkeypatch.setattr(downloader, "download", fake_download)
    r = client.post("/queue", data={"video_id": VID_NEW}, headers=as_(ana))
    assert r.status_code == 201
    assert wait_for(lambda: (k.queue.current() or {}).get("video_id") == VID_NEW)
    assert db.get_song(k.conn, VID_NEW)["title"] == "New"


def test_download_failure_marks_failed_and_keeps_going(client, k, ana, monkeypatch):
    k.remember_results([{"video_id": VID_NEW, "title": "Bad", "channel": None,
                         "duration_s": None, "thumb_url": None}])

    def bad(*a):
        raise downloader.DownloadError(downloader.MESSAGES["format"])

    monkeypatch.setattr(downloader, "download", bad)
    r = client.post("/queue", data={"video_id": VID_NEW, "q": "  cielito   lindo ", "mode": "lyrics"},
                    headers=as_(ana))
    item_id = int(r.headers["x-queue-id"])
    assert wait_for(lambda: k.queue.get(item_id)["state"] == "failed")
    assert k.queue.get(item_id)["error"] == downloader.MESSAGES["format"]
    failed = k.state_message()["failed"][0]
    # What the phone needs for "try another version".
    assert failed["id"] == item_id and failed["video_id"] == VID_NEW
    assert failed["query"] == "cielito lindo" and failed["search_mode"] == "lyrics"


def test_unexpected_download_crash_is_readable_and_logged(client, k, ana, monkeypatch, caplog):
    k.remember_results([{"video_id": VID_NEW, "title": "Bad", "channel": None,
                         "duration_s": None, "thumb_url": None}])

    def crash(*a):
        raise KeyError("formats")

    monkeypatch.setattr(downloader, "download", crash)
    with caplog.at_level("ERROR", logger="app.main"):
        r = client.post("/queue", data={"video_id": VID_NEW}, headers=as_(ana))
        item_id = int(r.headers["x-queue-id"])
        assert wait_for(lambda: k.queue.get(item_id)["state"] == "failed")
    assert k.queue.get(item_id)["error"] == downloader.MESSAGES["other"]
    assert "KeyError" in caplog.text


def test_search_marks_the_result_that_already_failed(client, ana, monkeypatch):
    monkeypatch.setattr(downloader, "search", lambda q, mode: [
        {"video_id": VID_A, "title": "Version A", "channel": "C", "duration_s": 100, "thumb_url": "t"},
        {"video_id": VID_B, "title": "Version B", "channel": "C", "duration_s": 100, "thumb_url": "t"},
    ])
    r = client.get("/search", params={"q": "x", "failed": VID_B}, headers=as_(ana))
    a, b = r.text.split("<article")[1:]
    assert "data-failed" in b and "already failed" in b
    assert "data-failed" not in a
    r = client.get("/search", params={"q": "x", "failed": '"><script>'}, headers=as_(ana))
    assert "data-failed" not in r.text and "<script>" not in r.text


def test_add_records_query_and_mode(client, k, ana):
    k.remember_results([{"video_id": VID_NEW, "title": "N", "channel": None,
                         "duration_s": None, "thumb_url": None}])
    r = client.post("/queue", data={"video_id": VID_NEW, "q": "x" * 999, "mode": "nope"}, headers=as_(ana))
    item = k.queue.get(int(r.headers["x-queue-id"]))
    assert item["query"] == "x" * downloader.MAX_QUERY_LEN and item["search_mode"] == "karaoke"


def test_old_database_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(Path(db.SCHEMA_PATH).read_text()
                      .replace("    query         TEXT,", "").replace("    search_mode   TEXT,", ""))
    old.execute("INSERT INTO songs (video_id, title) VALUES (?, ?)", (VID_A, "A"))
    old.execute("INSERT INTO queue (video_id, requested_by, state, position) VALUES (?, 'x', 'failed', 1)", (VID_A,))
    old.commit()
    old.close()
    conn = db.connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(queue)")}
    assert {"query", "search_mode"} <= cols
    assert main.Queue(conn).failed()[0]["query"] is None
    db.connect(path).close()  # idempotent


def test_download_over_the_limit_evicts_least_recently_used(client, k, ana, monkeypatch):
    k.cfg.media_max_bytes = 2500  # cached() writes 1000 bytes
    cached(k, VID_A)
    cached(k, VID_B)
    k.conn.execute("UPDATE songs SET last_used_at = '2026-01-01' WHERE video_id = ?", (VID_A,))
    k.conn.execute("UPDATE songs SET last_used_at = '2026-02-01' WHERE video_id = ?", (VID_B,))
    k.remember_results([{"video_id": VID_NEW, "title": "New", "channel": None,
                         "duration_s": None, "thumb_url": None}])

    def fake_download(video_id, media_dir, on_progress):
        p = Path(media_dir) / f"{video_id}.mp4"
        p.write_bytes(b"x" * 1000)
        return {"file_path": str(p), "cached": False}

    monkeypatch.setattr(downloader, "download", fake_download)
    client.post("/queue", data={"video_id": VID_NEW}, headers=as_(ana))
    assert wait_for(lambda: db.get_song(k.conn, VID_A)["file_path"] is None)
    assert not (Path(k.cfg.media_dir) / f"{VID_A}.mp4").exists()
    assert db.cached_file(k.conn, VID_B) and db.cached_file(k.conn, VID_NEW)


def test_user_cannot_remove_others_song(client, k, ana, beto):
    db.upsert_song(k.conn, VID_A, "A")
    item = k.queue.add(VID_A, ana)
    r = client.delete(f"/queue/{item['id']}", headers=as_(beto))
    assert r.status_code == 403
    assert k.queue.get(item["id"])["state"] != "removed"


def test_user_removes_own_and_admin_removes_any(client, k, ana, beto, admin):
    db.upsert_song(k.conn, VID_A, "A")
    mine = k.queue.add(VID_A, ana)
    theirs = k.queue.add(VID_A, beto)
    assert client.delete(f"/queue/{mine['id']}", headers=as_(ana)).status_code == 200
    assert client.delete(f"/queue/{theirs['id']}", headers=as_(admin)).status_code == 200
    assert k.queue.get(mine["id"])["state"] == "removed"
    assert k.queue.get(theirs["id"])["state"] == "removed"
    assert client.delete("/queue/9999", headers=as_(admin)).status_code == 404


# --- admin --------------------------------------------------------------------

ADMIN_CALLS = [
    ("/admin/skip", {}),
    ("/admin/pause", {}),
    ("/admin/reorder", {"order": "1,2"}),
    ("/admin/clear", {}),
    ("/admin/qr", {}),
    ("/admin/volume", {"level": "50"}),
]


@pytest.mark.parametrize("path,data", ADMIN_CALLS)
def test_admin_endpoints_forbidden_for_normal_user(client, k, ana, path, data, monkeypatch):
    monkeypatch.setattr(main, "run_pactl", lambda args: pytest.fail("must not call pactl"))
    cached(k, VID_A)
    cached(k, VID_B)
    k.queue.add(VID_A, ana)
    k.queue.add(VID_B, ana)
    before = k.state_message()
    r = client.post(path, data=data, headers=as_(ana))
    assert r.status_code == 403
    assert k.state_message() == before


def test_admin_login(client, k, ana):
    assert client.post("/admin/login", data={"password": "nop"}, headers=as_(ana)).status_code == 403
    assert not db.get_session(k.conn, ana["id"])["is_admin"]
    r = client.post("/admin/login", data={"password": "secret"}, headers=as_(ana))
    assert r.status_code == 200
    assert db.get_session(k.conn, ana["id"])["is_admin"] == 1
    assert client.post("/admin/clear", headers=as_(ana)).status_code == 200


@pytest.mark.parametrize(
    "first,second,accepted,rejected",
    [
        ("one", "two", ["one", "two"], ["", "three", "onetwo", "ONE"]),
        ("one", "", ["one"], ["", "two"]),
        ("", "two", ["two"], ["", "one"]),  # the second works on its own too
    ],
)
def test_admin_login_with_either_password(tmp_path, monkeypatch, first, second, accepted, rejected):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(main.asyncio, "sleep", lambda s: real_sleep(0))  # skip the 1 s delay
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m",
                 admin_password=first, admin_password_2=second)
    with TestClient(create_app(cfg)) as c:
        conn = c.app.state.k.conn
        assert 'id="admin-login"' in c.get("/", headers=as_(db.create_session(conn, "X"))).text
        for pw in rejected:
            s = db.create_session(conn, "Guest")
            assert c.post("/admin/login", data={"password": pw}, headers=as_(s)).status_code == 403
            assert not db.get_session(conn, s["id"])["is_admin"]
        for pw in accepted:
            s = db.create_session(conn, "Admin")
            assert c.post("/admin/login", data={"password": pw}, headers=as_(s)).status_code == 200
            assert db.get_session(conn, s["id"])["is_admin"] == 1


def test_second_admin_password_from_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_ADMIN_PASSWORD", "a")
    monkeypatch.setenv("KARAOKE_ADMIN_PASSWORD_2", "b")
    assert Config.from_env().admin_passwords == ["a", "b"]
    monkeypatch.delenv("KARAOKE_ADMIN_PASSWORD_2")
    assert Config.from_env().admin_passwords == ["a"]


def test_admin_login_disabled_without_password(tmp_path, monkeypatch):
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m", admin_password="")
    with TestClient(create_app(cfg)) as c:
        s = db.create_session(c.app.state.k.conn, "X")
        r = c.post("/admin/login", data={"password": ""}, headers=as_(s))
        assert r.status_code == 503


def test_admin_skip_pause_reorder_clear(client, k, ana, admin):
    for v in (VID_A, VID_B, "ccccccccccc"):
        cached(k, v)
        k.queue.add(v, ana)
    k.queue.start_if_idle()

    r = client.post("/admin/pause", headers=as_(admin))
    assert r.json()["paused"] is True
    assert client.post("/admin/pause", data={"paused": "0"}, headers=as_(admin)).json()["paused"] is False
    client.post("/admin/pause", headers=as_(admin))

    assert client.post("/admin/skip", headers=as_(admin)).status_code == 200
    assert k.queue.current()["video_id"] == VID_B
    assert k.paused is False  # the new song starts playing

    client.post("/admin/pause", headers=as_(admin))  # pause b
    pending = [i["id"] for i in k.queue.pending()]
    assert len(pending) == 1
    db.upsert_song(k.conn, VID_A, "A")
    extra = k.queue.add(VID_A, ana)
    client.post("/admin/reorder", data={"id": extra["id"], "delta": -1}, headers=as_(admin))
    assert [i["id"] for i in k.queue.pending()] == [extra["id"], pending[0]]
    assert k.paused is True  # reordering doesn't touch the pause

    assert client.post("/admin/reorder", data={"order": "x,y"}, headers=as_(admin)).status_code == 400
    assert client.post("/admin/reorder", headers=as_(admin)).status_code == 400

    client.post("/admin/clear", headers=as_(admin))
    assert k.queue.pending() == []
    assert k.queue.current()["video_id"] == VID_B


def test_admin_volume(client, admin, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_pactl", calls.append)
    assert client.post("/admin/volume", data={"level": "80"}, headers=as_(admin)).status_code == 200
    assert client.post("/admin/volume", data={"delta": "-5"}, headers=as_(admin)).status_code == 200
    assert calls == [
        ["set-sink-volume", "@DEFAULT_SINK@", "80%"],
        ["set-sink-volume", "@DEFAULT_SINK@", "-5%"],
    ]
    for bad in ({"level": "500"}, {"delta": "99"}, {"level": "80; reboot"}, {}):
        assert client.post("/admin/volume", data=bad, headers=as_(admin)).status_code in (400, 422)
    assert len(calls) == 2


# --- media --------------------------------------------------------------------

def test_media_supports_range(client, k):
    cached(k, VID_A)
    r = client.get(f"/media/{VID_A}.mp4", headers={"range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == b"0123456789"
    assert r.headers["content-type"] == "video/mp4"
    assert client.get(f"/media/{VID_A}.mp4").headers["accept-ranges"] == "bytes"


def test_media_rejects_bad_ids(client):
    assert client.get("/media/zzzzzzzzzzz.mp4").status_code == 404
    assert client.get("/media/..%2F..%2Fschema.mp4").status_code == 404
    assert client.get("/media/abc.mp4").status_code == 404


# --- QR and screen ----------------------------------------------------------

@pytest.mark.parametrize(
    "ssid,password,expected",
    [
        ("Casa", "clave123", "WIFI:T:WPA;S:Casa;P:clave123;;"),
        ('Mi;Red,"Coatl"', r"p:a\ss;", r'WIFI:T:WPA;S:Mi\;Red\,\"Coatl\";P:p\:a\\ss\;;;'),
        ("Café Ñandú", "contraseña", "WIFI:T:WPA;S:Café Ñandú;P:contraseña;;"),
        ("Abierta", "", "WIFI:T:nopass;S:Abierta;;"),
    ],
)
def test_wifi_payload_escaping(ssid, password, expected):
    assert wifi_payload(ssid, password) == expected


def test_wifi_qr_with_special_chars_renders(tmp_path):
    svg = main.qr_svg(wifi_payload('Mi;Red,"Coatl"', r"p:a\ss;ñ"))
    assert svg.startswith("<svg") and "</svg>" in svg


def test_screen_has_two_qrs(client, k):
    r = client.get("/screen")
    assert r.status_code == 200
    assert k.app_url.endswith(":8004")
    assert k.wifi_qr is not None
    assert k.wifi_qr in r.text and k.app_qr in r.text


def test_screen_shows_qrs_while_paused_and_in_bar(client, k):
    html = client.get("/screen").text
    paused = html.split('id="paused"', 1)[1].split("</section>", 1)[0]
    assert k.app_qr in paused and k.wifi_qr in paused
    bar = html.split('id="bar"', 1)[1].split("</header>", 1)[0]
    assert k.app_qr in bar


# --- new party ----------------------------------------------------------------

def test_new_party_empties_everything_but_names_and_library(client, k, ana, beto, admin):
    cached(k, VID_A)
    cached(k, VID_B)
    k.queue.add(VID_A, ana)  # plays
    k.queue.add(VID_B, beto)  # ready
    db.upsert_song(k.conn, VID_NEW, "New")
    k.queue.add(VID_NEW, ana)  # waits for its download
    k.queue.start_if_idle()
    client.post("/admin/pause", headers=as_(admin))
    k.queue.conn.execute("UPDATE queue SET state = 'failed', error = 'x' WHERE video_id = ?", (VID_NEW,))
    assert k.paused and k.state_message()["failed"]

    with client.websocket_connect("/ws") as screen:
        screen.receive_json()
        assert client.post("/party/new").status_code == 200
        msg = screen.receive_json()
    assert msg["mode"] == "waiting"  # the screen goes to the QR codes
    assert msg["current"] is None and msg["queue"] == [] and msg["failed"] == []
    assert msg["paused"] is False
    # Guests keep their names, and the library stays cached.
    assert db.get_session(k.conn, ana["id"])["name"] == "Ana"
    assert db.cached_file(k.conn, VID_A) and db.cached_file(k.conn, VID_B)
    # A new request after the reset works normally.
    client.post("/queue", data={"video_id": VID_A}, headers=as_(ana))
    assert k.queue.current()["video_id"] == VID_A


def test_new_party_only_from_the_karaoke_machine(tmp_path, monkeypatch):
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m", admin_password="x")  # localhost only
    with TestClient(create_app(cfg)) as c:
        k = c.app.state.k
        s = db.create_session(k.conn, "Ana")
        db.set_admin(k.conn, s["id"])
        cached(k, VID_A)
        k.queue.add(VID_A, s)
        k.queue.start_if_idle()
        # Not even an admin's phone.
        assert c.post("/party/new", headers=as_(s)).status_code == 403
        assert k.queue.current()["video_id"] == VID_A


# --- pinned QR ------------------------------------------------------------------

def test_admin_pins_and_unpins_qr(client, k, admin):
    assert k.state_message()["qr_pinned"] is False
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        r = client.post("/admin/qr", data={"pinned": "1"}, headers=as_(admin))
        assert r.json()["qr_pinned"] is True
        assert ws.receive_json()["qr_pinned"] is True  # the screen hears about it
    assert client.post("/admin/qr", headers=as_(admin)).json()["qr_pinned"] is False  # toggle
    assert client.post("/admin/qr", data={"pinned": "0"}, headers=as_(admin)).json()["qr_pinned"] is False


def test_pinned_qr_survives_restart_and_new_party(cfg):
    with TestClient(create_app(cfg)) as c:
        c.app.state.k.set_qr_pinned(True)
        assert c.post("/party/new").status_code == 200
        assert c.app.state.k.state_message()["qr_pinned"] is True
    with TestClient(create_app(cfg)) as c:
        assert c.app.state.k.state_message()["qr_pinned"] is True


def test_screen_q_key_toggles_qr(client, k):
    with client.websocket_connect("/ws") as screen:
        screen.receive_json()
        screen.send_text(json.dumps({"type": "toggle-qr"}))
        assert screen.receive_json()["qr_pinned"] is True
        screen.send_text(json.dumps({"type": "toggle-qr"}))
        assert screen.receive_json()["qr_pinned"] is False


def test_qr_toggle_ignored_from_phones(tmp_path):
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m")  # localhost only
    with TestClient(create_app(cfg)) as c:
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({"type": "toggle-qr"}))
            time.sleep(0.2)
        assert c.app.state.k.qr_pinned is False


def test_screen_has_pinned_qr_and_admin_has_button(client, k, admin, ana):
    html = client.get("/screen").text
    pinned = html.split('id="pinned-qr"', 1)[1].split("</div>\n  </div>", 1)[0]
    assert k.app_qr in pinned and k.app_url in pinned
    assert 'data-admin-action="qr"' in client.get("/", headers=as_(admin)).text
    assert 'data-admin-action="qr"' not in client.get("/", headers=as_(ana)).text


# --- WebSocket ----------------------------------------------------------------

def test_ws_sends_state_without_session_ids(client, k, ana):
    cached(k, VID_A)
    k.queue.add(VID_A, ana)
    k.queue.start_if_idle()
    with client.websocket_connect("/ws") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "state"
    assert msg["mode"] == "playing"
    assert msg["current"]["owner"] == owner_tag(ana["id"])
    assert ana["id"] not in json.dumps(msg)


def test_ws_ended_advances_and_broadcasts(client, k, ana):
    cached(k, VID_A)
    cached(k, VID_B)
    a = k.queue.add(VID_A, ana)
    k.queue.add(VID_B, ana)
    k.queue.start_if_idle()
    with client.websocket_connect("/ws") as screen, client.websocket_connect("/ws") as phone:
        screen.receive_json()
        phone.receive_json()
        screen.send_text(json.dumps({"type": "ended", "id": a["id"]}))
        s1 = screen.receive_json()
        p1 = phone.receive_json()
        assert s1["current"]["video_id"] == VID_B == p1["current"]["video_id"]
        # a repeated `ended` for the previous song doesn't skip the current one
        screen.send_text(json.dumps({"type": "ended", "id": a["id"]}))
        screen.send_text("this is not json")
        screen.send_text(json.dumps({"type": "ended", "id": s1["current"]["id"]}))
        s2 = screen.receive_json()
        assert s2["mode"] == "waiting"


def test_ws_error_marks_failed(client, k, ana):
    cached(k, VID_A)
    a = k.queue.add(VID_A, ana)
    k.queue.start_if_idle()
    with client.websocket_connect("/ws") as screen:
        screen.receive_json()
        screen.send_text(json.dumps({"type": "error", "id": a["id"], "error": "MEDIA_ERR_DECODE"}))
        msg = screen.receive_json()
    assert msg["mode"] == "waiting"
    assert msg["failed"][0]["error"] == "MEDIA_ERR_DECODE"


def test_ws_ended_ignored_from_non_screen_host(tmp_path):
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m")  # localhost only
    with TestClient(create_app(cfg)) as c:
        k = c.app.state.k
        ana = db.create_session(k.conn, "Ana")
        cached(k, VID_A)
        a = k.queue.add(VID_A, ana)
        k.queue.start_if_idle()
        with c.websocket_connect("/ws") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({"type": "ended", "id": a["id"]}))
            time.sleep(0.2)
        assert k.queue.current()["id"] == a["id"]


# --- mobile view ----------------------------------------------------------------

def test_mobile_view_for_guest(client, ana):
    r = client.get("/", headers=as_(ana))
    assert r.status_code == 200
    assert 'hx-get="/search"' in r.text
    assert f'data-owner="{owner_tag(ana["id"])}"' in r.text
    assert 'data-admin="0"' in r.text
    assert 'id="admin-login"' in r.text  # discreet link
    assert 'id="admin"' not in r.text  # no controls
    assert ana["id"] not in r.text


def test_mobile_view_for_admin(client, admin):
    r = client.get("/", headers=as_(admin))
    assert 'data-admin="1"' in r.text
    assert 'data-admin-action="skip"' in r.text
    assert 'id="admin-login"' not in r.text


def test_mobile_hides_admin_link_when_disabled(tmp_path):
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m", admin_password="")
    with TestClient(create_app(cfg)) as c:
        s = db.create_session(c.app.state.k.conn, "X")
        assert 'id="admin-login"' not in c.get("/", headers=as_(s)).text


def test_results_have_add_buttons(client, ana, monkeypatch):
    monkeypatch.setattr(downloader, "search", lambda q, k: [
        {"video_id": VID_A, "title": "Cielito", "channel": "C", "duration_s": 185, "thumb_url": "t"}])
    r = client.get("/search?q=cielito&mode=karaoke", headers=as_(ana))
    assert 'hx-post="/queue"' in r.text
    assert "3:05" in r.text
    assert "&#34;video_id&#34;: &#34;aaaaaaaaaaa&#34;" in r.text or '"video_id": "aaaaaaaaaaa"' in r.text
    r = client.post("/queue", data={"video_id": VID_A}, headers=as_(ana))
    assert "Queued" in r.text


def test_search_mode_defaults_to_karaoke(client, ana, monkeypatch):
    seen = []
    monkeypatch.setattr(downloader, "search", lambda q, mode: seen.append(mode) or [])
    for mode in ("", "karaoke", "lyrics", "youtube", "<script>"):
        client.get("/search", params={"q": "x", "mode": mode}, headers=as_(ana))
    client.get("/search?q=x", headers=as_(ana))
    assert seen == ["karaoke", "karaoke", "lyrics", "youtube", "karaoke", "karaoke"]


def test_mode_chips_always_visible_karaoke_first(client, ana):
    html = client.get("/", headers=as_(ana)).text
    form = html.split('id="search"', 1)[1].split("</form>", 1)[0]
    assert "<details" not in form
    radios = [part.split(">", 1)[0] for part in form.split('name="mode"')[1:]]
    assert [r.split('value="', 1)[1].split('"', 1)[0] for r in radios] == ["karaoke", "lyrics", "youtube"]
    assert "checked" in radios[0] and not any("checked" in r for r in radios[1:])
    assert "Lyrics" in form


# --- screen ---------------------------------------------------------------------

def test_screen_works_offline_and_has_no_controls(client):
    html = client.get("/screen").text
    assert "cdn." not in html and "https://" not in html  # nothing external
    assert "<video" in html and "controls" not in html.split("<video", 1)[1].split(">", 1)[0]
    assert '/static/screen.js' in html and '/static/ws.js' in html
    assert client.get("/static/screen.js").status_code == 200


def test_screen_without_wifi_config(tmp_path):
    cfg = Config(db_path=tmp_path / "k.db", media_dir=tmp_path / "m")
    with TestClient(create_app(cfg)) as c:
        html = c.get("/screen").text
        assert "KARAOKE_WIFI_SSID is not set" in html
        # Only the app QR: waiting screen, up-next bar, pinned corner and pause overlay.
        assert html.count("<svg") == 4
        assert html.count(c.app.state.k.app_qr) == 4


def test_media_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_MEDIA_DIR", str(tmp_path / "songs"))
    assert Config.from_env().media_dir == tmp_path / "songs"
    monkeypatch.delenv("KARAOKE_MEDIA_DIR")
    assert Config.from_env().media_dir == downloader.DEFAULT_MEDIA_DIR
