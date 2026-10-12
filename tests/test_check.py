"""Tests of the YouTube health check. Nothing calls YouTube or uptime-kuma."""

import os

import pytest

from app import check, downloader


@pytest.fixture
def routes(tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("KARAOKE_COOKIES_FILE", str(cookies))
    monkeypatch.setenv("KARAOKE_PROXY_URL", "http://127.0.0.1:8888")
    return cookies


@pytest.fixture
def asked(monkeypatch):
    """Fakes check_route: records which routes were asked; `fail` maps a route to its failure."""
    seen: list[str] = []
    fail: dict[str, str] = {}

    def fake(route_opts, video_id):
        route = "cookies" if "cookiefile" in route_opts else "proxy" if "proxy" in route_opts else "direct"
        seen.append(route)
        return fail.get(route)

    monkeypatch.setattr(check, "check_route", fake)
    return seen, fail


def test_first_run_asks_every_route(routes, asked):
    seen, _ = asked
    results, state = check.run("v", {}, now=1000.0)
    assert seen == ["direct", "cookies", "proxy"]
    assert results == {"direct": None, "cookies": None, "proxy": None}
    assert state["cookies"]["at"] == 1000.0


def test_cookies_are_only_asked_once_a_day(routes, asked):
    seen, fail = asked
    fail["cookies"] = "bot"
    _, state = check.run("v", {}, now=1000.0)
    seen.clear()
    results, state = check.run("v", state, now=1000.0 + 3600)
    assert seen == ["direct", "proxy"]
    assert results["cookies"] == "bot"  # the last result is reported again
    seen.clear()
    check.run("v", state, now=1000.0 + check.COOKIES_EVERY_S)
    assert seen == ["direct", "cookies", "proxy"]


def test_replacing_the_cookie_file_asks_again(routes, asked):
    seen, _ = asked
    _, state = check.run("v", {}, now=1000.0)
    mtime = routes.stat().st_mtime
    os.utime(routes, (mtime + 60, mtime + 60))
    seen.clear()
    check.run("v", state, now=1060.0)
    assert "cookies" in seen


def test_only_configured_routes_are_checked(asked, monkeypatch):
    monkeypatch.delenv("KARAOKE_COOKIES_FILE", raising=False)
    monkeypatch.delenv("KARAOKE_PROXY_URL", raising=False)
    results, state = check.run("v", {}, now=1000.0)
    assert results == {"direct": None} and state == {}


@pytest.mark.parametrize(
    "results,ok,msg",
    [
        ({"direct": None, "cookies": None, "proxy": None}, True, "OK: direct, cookies, proxy"),
        (
            {"direct": "bot", "cookies": None, "proxy": None},
            False,
            "direct FAILED (bot); still working: cookies, proxy",
        ),
        (
            {"direct": "bot", "proxy": "offline"},
            False,
            "direct FAILED (bot); proxy FAILED (offline); no route works",
        ),
    ],
)
def test_summary(results, ok, msg):
    assert check.summary(results) == (ok, msg)


def test_check_route_asks_like_a_download_without_downloading(monkeypatch):
    calls = []

    class FakeYDL:
        def __init__(self, opts):
            calls.append(opts)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls.append((url, download))
            return {"ext": "mp4", "requested_formats": [
                {"vcodec": "avc1.640028", "acodec": "none"}, {"vcodec": "none", "acodec": "mp4a.40.2"}]}

    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    assert check.check_route({"proxy": "http://p"}, "dQw4w9WgXcQ") is None
    assert calls[0]["proxy"] == "http://p" and calls[0]["format"] == downloader.FORMAT
    assert calls[1] == ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False)


def test_check_route_reports_the_kind_of_failure(monkeypatch):
    class Blocked:
        def __init__(self, opts): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, url, download=False):
            raise RuntimeError("ERROR: [youtube] x: Sign in to confirm you're not a bot")

    monkeypatch.setattr(downloader, "YoutubeDL", Blocked)
    assert check.check_route({}, "dQw4w9WgXcQ") == "bot"


def test_push_replaces_the_query_of_the_configured_url(monkeypatch):
    opened = []

    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    monkeypatch.setattr(check.urllib.request, "urlopen", lambda url, timeout: opened.append(url) or Response())
    check.push("http://k:3001/api/push/tok?status=up&msg=OK&ping=", False, "direct FAILED (bot)", 1234)
    assert opened == ["http://k:3001/api/push/tok?status=down&msg=direct+FAILED+%28bot%29&ping=1234"]
