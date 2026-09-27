"""HTTP and WebSocket tests. Permissions are tested by calling the endpoints
directly, not through the UI. Nothing calls YouTube."""

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


def test_endpoints_require_session(client):
    assert client.get("/search?q=x").status_code == 401
    assert client.post("/queue", data={"video_id": VID_A}).status_code == 401
    assert client.delete("/queue/1").status_code == 401
    assert client.post("/admin/skip").status_code == 401


# --- search -------------------------------------------------------------------

def test_search_weird_query_and_escaped_results(client, ana, monkeypatch):
    seen = {}

    def fake_search(q, add_karaoke):
        seen["q"], seen["k"] = q, add_karaoke
        return [{"video_id": VID_A, "title": "<script>alert(1)</script> ñandú",
                 "channel": "C", "duration_s": 100, "thumb_url": "t"}]

    monkeypatch.setattr(downloader, "search", fake_search)
    q = 'canción "ñoña"; DROP TABLE queue; --'
    r = client.get("/search", params={"q": q, "karaoke": 0}, headers=as_(ana))
    assert r.status_code == 200
    assert seen == {"q": q, "k": False}
    assert "<script>alert" not in r.text
    assert "&lt;script&gt;" in r.text and "ñandú" in r.text


def test_search_error_is_shown_not_500(client, ana, monkeypatch):
    def boom(q, add_karaoke):
        raise downloader.DownloadError("no network")

    monkeypatch.setattr(downloader, "search", boom)
    r = client.get("/search?q=x", headers=as_(ana))
    assert r.status_code == 200
    assert "no network" in r.text


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
        raise downloader.DownloadError("format not playable in the browser: video=vp9")

    monkeypatch.setattr(downloader, "download", bad)
    r = client.post("/queue", data={"video_id": VID_NEW}, headers=as_(ana))
    item_id = int(r.headers["x-queue-id"])
    assert wait_for(lambda: k.queue.get(item_id)["state"] == "failed")
    assert "vp9" in k.queue.get(item_id)["error"]
    assert k.state_message()["failed"][0]["id"] == item_id


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
    r = client.get("/search?q=cielito&karaoke=1", headers=as_(ana))
    assert 'hx-post="/queue"' in r.text
    assert "3:05" in r.text
    assert "&#34;video_id&#34;: &#34;aaaaaaaaaaa&#34;" in r.text or '"video_id": "aaaaaaaaaaa"' in r.text
    r = client.post("/queue", data={"video_id": VID_A}, headers=as_(ana))
    assert "Queued" in r.text


def test_search_checkbox_off_means_no_karaoke(client, ana, monkeypatch):
    seen = []
    monkeypatch.setattr(downloader, "search", lambda q, k: seen.append(k) or [])
    client.get("/search?q=x", headers=as_(ana))  # unchecked box: the field isn't sent
    client.get("/search?q=x&karaoke=1", headers=as_(ana))
    assert seen == [False, True]


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
        # Only the app QR: waiting screen, up-next bar and pause overlay.
        assert html.count("<svg") == 3
        assert html.count(c.app.state.k.app_qr) == 3


def test_media_dir_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KARAOKE_MEDIA_DIR", str(tmp_path / "songs"))
    assert Config.from_env().media_dir == tmp_path / "songs"
    monkeypatch.delenv("KARAOKE_MEDIA_DIR")
    assert Config.from_env().media_dir == downloader.DEFAULT_MEDIA_DIR
