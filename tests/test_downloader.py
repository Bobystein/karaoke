"""Downloader tests. They never call YouTube: YoutubeDL is faked."""

import pytest
import yt_dlp

from app import downloader
from app.downloader import DownloadError


class FakeYDL:
    """Mimics the part of YoutubeDL we use. Records the calls."""

    calls: list = []
    opts_seen: list = []
    search_result: dict = {}
    video_info: dict = {}
    raise_on_extract: Exception | None = None
    write_file = True

    def __init__(self, opts):
        self.opts = opts
        FakeYDL.opts_seen.append(opts)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False, process=True):
        FakeYDL.calls.append(("extract_info", url, download))
        if FakeYDL.raise_on_extract:
            raise FakeYDL.raise_on_extract
        if url.startswith("ytsearch"):
            return FakeYDL.search_result
        return dict(FakeYDL.video_info)

    def process_ie_result(self, info, download=True):
        FakeYDL.calls.append(("process_ie_result", info["id"], download))
        for hook in self.opts.get("progress_hooks", []):
            hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
            hook({"status": "finished"})
            hook({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 10})
            hook({"status": "finished"})
        if FakeYDL.write_file:
            out = self.opts["outtmpl"].replace("%(id)s", info["id"]).replace("%(ext)s", "mp4")
            with open(out, "wb") as f:
                f.write(b"fake mp4")
        return info


@pytest.fixture(autouse=True)
def fake_ydl(monkeypatch):
    FakeYDL.calls = []
    FakeYDL.opts_seen = []
    FakeYDL.search_result = {"entries": []}
    FakeYDL.video_info = {}
    FakeYDL.raise_on_extract = None
    FakeYDL.write_file = True
    monkeypatch.setattr(downloader, "YoutubeDL", FakeYDL)
    return FakeYDL


VID = "dQw4w9WgXcQ"

H264 = {
    "id": VID,
    "title": "Cielito lindo (karaoke)",
    "channel": "Karaoke MX",
    "duration": 185,
    "ext": "mp4",
    "requested_formats": [
        {"vcodec": "avc1.640028", "acodec": "none"},
        {"vcodec": "none", "acodec": "mp4a.40.2"},
    ],
}


# --- search -----------------------------------------------------------------

def test_search_adds_karaoke_and_parses(fake_ydl):
    fake_ydl.search_result = {
        "entries": [
            {"id": VID, "title": "Cielito lindo", "channel": "Channel", "duration": 190.0},
            {"id": "aaaaaaaaaaa", "title": "2-hour compilation", "duration": 7200},
            {"id": "UCxxxxxxxxxxxxxxxxxxxxxx", "title": "A channel"},
            None,
        ]
    }
    res = downloader.search("cielito lindo")
    assert fake_ydl.calls == [("extract_info", "ytsearch12:cielito lindo karaoke", False)]
    assert res == [
        {
            "video_id": VID,
            "title": "Cielito lindo",
            "channel": "Channel",
            "duration_s": 190,
            "thumb_url": f"https://i.ytimg.com/vi/{VID}/mqdefault.jpg",
        }
    ]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("karaoke", "cielito lindo karaoke"),
        ("lyrics", "cielito lindo lyrics"),
        ("youtube", "cielito lindo"),
        ("bogus", "cielito lindo karaoke"),  # unknown mode = default
    ],
)
def test_search_modes(fake_ydl, mode, expected):
    downloader.search("cielito lindo", mode)
    assert fake_ydl.calls[0][1] == f"ytsearch12:{expected}"


def test_search_lyrics_mode_does_not_duplicate(fake_ydl):
    downloader.search("Cielito Lindo LYRICS", "lyrics")
    assert fake_ydl.calls[0][1] == "ytsearch12:Cielito Lindo LYRICS"


def test_duration_filter_is_the_same_in_every_mode(fake_ydl):
    fake_ydl.search_result = {"entries": [{"id": VID, "title": "long", "duration": 7200}]}
    for mode in downloader.SEARCH_MODES:
        assert downloader.search("x", mode) == []


def test_search_does_not_duplicate_karaoke(fake_ydl):
    downloader.search("Cielito Lindo KARAOKE")
    assert fake_ydl.calls[0][1] == "ytsearch12:Cielito Lindo KARAOKE"


def test_search_weird_characters_pass_through_intact(fake_ydl):
    q = 'canción "ñoña"; rm -rf / && echo $(whoami) `id` | ¿qué?'
    assert downloader.search(q) == []
    assert fake_ydl.calls[0][1] == f"ytsearch12:{q} karaoke"


def test_search_empty_query_does_not_call_youtube(fake_ydl):
    assert downloader.search("   ") == []
    assert fake_ydl.calls == []


def test_search_query_is_truncated(fake_ydl):
    downloader.search("a" * 5000, "youtube")
    assert fake_ydl.calls[0][1] == "ytsearch12:" + "a" * downloader.MAX_QUERY_LEN


def test_search_error_becomes_readable_download_error(fake_ydl):
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError(
        "ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>"
    )
    with pytest.raises(DownloadError) as exc:
        downloader.search("x")
    assert str(exc.value) == downloader.MESSAGES["offline"]


def test_search_unknown_error_says_search_failed(fake_ydl):
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError("ERROR: something new")
    with pytest.raises(DownloadError) as exc:
        downloader.search("x")
    assert str(exc.value) == downloader.MESSAGES["search"]


# --- download ---------------------------------------------------------------

def test_download_h264(fake_ydl, tmp_path):
    fake_ydl.video_info = H264
    progress = []
    res = downloader.download(VID, tmp_path, on_progress=progress.append)
    assert res["file_path"] == str(tmp_path / f"{VID}.mp4")
    assert res["cached"] is False
    assert res["duration_s"] == 185
    assert (tmp_path / f"{VID}.mp4").is_file()
    assert progress == sorted(progress)
    assert progress[0] == 25.0
    assert progress[-1] == 100.0


def test_download_uses_cache(fake_ydl, tmp_path):
    (tmp_path / f"{VID}.mp4").write_bytes(b"already here")
    res = downloader.download(VID, tmp_path)
    assert res["cached"] is True
    assert fake_ydl.calls == []


def test_download_single_file_fallback_ok(fake_ydl, tmp_path):
    fake_ydl.video_info = {"id": VID, "ext": "mp4", "vcodec": "avc1.42001E", "acodec": "mp4a.40.2"}
    downloader.download(VID, tmp_path)
    assert (tmp_path / f"{VID}.mp4").is_file()


@pytest.mark.parametrize(
    "info",
    [
        {"id": VID, "ext": "webm", "vcodec": "vp9", "acodec": "opus"},
        {"id": VID, "ext": "mp4", "vcodec": "av01.0.08M.08", "acodec": "mp4a.40.2"},
        {"id": VID, "ext": "mp4", "vcodec": "avc1.4d401f", "acodec": "none"},
    ],
)
def test_download_rejects_unplayable_before_downloading(fake_ydl, tmp_path, info):
    fake_ydl.video_info = info
    with pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    assert str(exc.value) == downloader.MESSAGES["format"]
    assert not any(c[0] == "process_ie_result" for c in fake_ydl.calls)
    assert list(tmp_path.iterdir()) == []


def test_download_failure_cleans_partials_keeps_nothing_else(fake_ydl, tmp_path):
    (tmp_path / f"{VID}.f137.mp4.part").write_bytes(b"half")
    (tmp_path / "otherVideo1.mp4").write_bytes(b"other cache")
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError("ERROR: \x1b[0;31mVideo unavailable\x1b[0m")
    with pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    assert str(exc.value) == downloader.MESSAGES["unavailable"]
    assert [p.name for p in tmp_path.iterdir()] == ["otherVideo1.mp4"]


def test_download_missing_output_is_failure(fake_ydl, tmp_path):
    fake_ydl.video_info = H264
    fake_ydl.write_file = False
    with pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    assert str(exc.value) == downloader.MESSAGES["other"]


@pytest.mark.parametrize("bad", ["../../etc/pa", "abc", "a" * 12, "abc def ghi", ""])
def test_invalid_video_id_rejected(fake_ydl, tmp_path, bad):
    with pytest.raises(DownloadError, match="invalid"):
        downloader.download(bad, tmp_path)
    assert fake_ydl.calls == []


# --- readable errors --------------------------------------------------------

@pytest.mark.parametrize(
    "raw,kind",
    [
        ("ERROR: [youtube] dQw4w9WgXcQ: Sign in to confirm you’re not a bot. Use --cookies-from-browser", "bot"),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "bot"),
        ("ERROR: Unable to download webpage: HTTP Error 429: Too Many Requests", "bot"),
        ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", "unavailable"),
        ("ERROR: [youtube] aaaaaaaaaaa: This video is unavailable", "unavailable"),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "unavailable"),
        ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", "unavailable"),
        ("ERROR: [youtube] x: The uploader has not made this video available in your country", "geo"),
        ("ERROR: [youtube] x: Video unavailable. This video is not available in your country", "geo"),
        ("ERROR: Unable to download webpage: <urlopen error [Errno -3] Temporary failure in name resolution>", "offline"),
        ("ERROR: unable to download video data: <urlopen error timed out>", "offline"),
        ("format not playable in the browser: video=vp9 audio=opus container=webm", "format"),
        ("ERROR: [youtube] x: Requested format is not available. Use --list-formats", "format"),
        ("ERROR: something nobody has seen before", "other"),
    ],
)
def test_error_kinds(raw, kind):
    assert downloader.error_kind(raw) == kind


@pytest.mark.parametrize(
    "raw",
    [
        "ERROR: [youtube] x: Sign in to confirm you're not a bot",
        "ERROR: Unable to download webpage: <urlopen error timed out>",
        "Traceback (most recent call last):\n  File \"x.py\", line 1\nKeyError: 'formats'",
    ],
)
def test_ui_never_gets_ytdlp_text_and_log_gets_all_of_it(fake_ydl, tmp_path, caplog, raw):
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError(raw)
    with caplog.at_level("WARNING", logger="app.downloader"), pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    shown = str(exc.value)
    assert shown in downloader.MESSAGES.values()
    assert "ERROR" not in shown and "Traceback" not in shown
    assert raw.removeprefix("ERROR: ") in caplog.text
    assert caplog.records[0].exc_info is not None  # the traceback goes to the log


def test_unexpected_exception_is_also_readable(fake_ydl, tmp_path):
    fake_ydl.raise_on_extract = KeyError("formats")
    with pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    assert str(exc.value) == downloader.MESSAGES["other"]


def test_bot_check_is_matched_across_the_full_text(fake_ydl, tmp_path):
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError("ERROR: " + "x" * 400 + " not a bot")
    with pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    assert str(exc.value) == downloader.MESSAGES["bot"]


# --- cookies ----------------------------------------------------------------

def test_cookies_file_used_when_it_exists(fake_ydl, tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("KARAOKE_COOKIES_FILE", str(cookies))
    assert downloader.ydl_opts()["cookiefile"] == str(cookies)
    fake_ydl.video_info = H264
    downloader.download(VID, tmp_path / "media")
    downloader.search("x")
    downloader.resolve(VID)
    assert len(fake_ydl.opts_seen) == 3
    assert all(o["cookiefile"] == str(cookies) for o in fake_ydl.opts_seen)


@pytest.mark.parametrize("value", ["", "/nonexistent/cookies.txt"])
def test_cookies_file_ignored_when_missing(monkeypatch, value):
    monkeypatch.setenv("KARAOKE_COOKIES_FILE", value)
    assert "cookiefile" not in downloader.ydl_opts()


def test_cookies_file_unset(monkeypatch):
    monkeypatch.delenv("KARAOKE_COOKIES_FILE", raising=False)
    assert "cookiefile" not in downloader.ydl_opts()


# --- links ------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        VID,
        f"  {VID}  ",
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtube.com/watch?v={VID}&list=PL123&t=42s",
        f"http://m.youtube.com/watch?feature=share&v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"www.youtube.com/watch?v={VID}",
        f"youtube.com/watch/?v={VID}",
        f"https://youtu.be/{VID}",
        f"https://youtu.be/{VID}?si=abcdef",
        f"youtu.be/{VID}",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}",
    ],
)
def test_parse_video_ref_accepts_youtube(text):
    assert downloader.parse_video_ref(text) == VID


@pytest.mark.parametrize(
    "text",
    [
        "cielito lindo",
        "",
        "abc",
        VID + "x",
        f"https://www.youtube.com.evil.net/watch?v={VID}",
        f"https://evil.net/watch?v={VID}",
        f"https://evil.net/?u=https://youtu.be/{VID}",
        f"https://notyoutube.com/watch?v={VID}",
        f"https://www.youtube.com/results?search_query={VID}",
        f"https://www.youtube.com/watch?v={VID[:10]}",
        f"https://www.youtube.com/watch?v={VID}x",
        f"https://www.youtube.com/watch?v=../../etc/pa",
        f"https://youtu.be/{VID}/extra",
        f"https://user:pw@www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com:8080/watch?v={VID}",
        f"https://www.youtube.com:99999/watch?v={VID}",
        f"ftp://www.youtube.com/watch?v={VID}",
        f"javascript:alert(1)//youtu.be/{VID}",
        f"file:///youtu.be/{VID}",
        f"https://www.youtube.com/watch?v={VID} rm -rf",
        f"https://[::1/watch?v={VID}",
    ],
)
def test_parse_video_ref_rejects_everything_else(text):
    assert downloader.parse_video_ref(text) is None


def test_resolve_uses_canonical_url_only(fake_ydl):
    fake_ydl.video_info = {"id": VID, "title": "Cielito", "uploader": "Up", "duration": 185.0}
    assert downloader.resolve(VID) == {
        "video_id": VID,
        "title": "Cielito",
        "channel": "Up",
        "duration_s": 185,
        "thumb_url": f"https://i.ytimg.com/vi/{VID}/mqdefault.jpg",
    }
    assert fake_ydl.calls == [("extract_info", f"https://www.youtube.com/watch?v={VID}", False)]


def test_resolve_applies_the_same_duration_limit(fake_ydl):
    fake_ydl.video_info = {"id": VID, "title": "2 hours", "duration": 7200}
    with pytest.raises(downloader.TooLong):
        downloader.resolve(VID)


@pytest.mark.parametrize(
    "info",
    [
        {"id": "otherVideo1", "title": "not what we asked for"},
        {"id": VID, "title": "live", "is_live": True},
        {"id": VID, "title": "soon", "live_status": "is_upcoming"},
    ],
)
def test_resolve_rejects_mismatch_and_live(fake_ydl, info):
    fake_ydl.video_info = info
    with pytest.raises(DownloadError) as exc:
        downloader.resolve(VID)
    assert str(exc.value) == downloader.MESSAGES["unavailable"]


def test_resolve_error_is_readable(fake_ydl):
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError("ERROR: [youtube] x: Video unavailable")
    with pytest.raises(DownloadError) as exc:
        downloader.resolve(VID)
    assert str(exc.value) == downloader.MESSAGES["unavailable"]
