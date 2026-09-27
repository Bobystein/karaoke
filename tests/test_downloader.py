"""Downloader tests. They never call YouTube: YoutubeDL is faked."""

import pytest
import yt_dlp

from app import downloader
from app.downloader import DownloadError


class FakeYDL:
    """Mimics the part of YoutubeDL we use. Records the calls."""

    calls: list = []
    search_result: dict = {}
    video_info: dict = {}
    raise_on_extract: Exception | None = None
    write_file = True

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
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


def test_search_without_karaoke(fake_ydl):
    downloader.search("cielito lindo", add_karaoke=False)
    assert fake_ydl.calls[0][1] == "ytsearch12:cielito lindo"


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
    downloader.search("a" * 5000, add_karaoke=False)
    assert fake_ydl.calls[0][1] == "ytsearch12:" + "a" * downloader.MAX_QUERY_LEN


def test_search_error_becomes_download_error(fake_ydl):
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError("ERROR: no network")
    with pytest.raises(DownloadError, match="no network"):
        downloader.search("x")


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
    with pytest.raises(DownloadError, match="not playable"):
        downloader.download(VID, tmp_path)
    assert not any(c[0] == "process_ie_result" for c in fake_ydl.calls)
    assert list(tmp_path.iterdir()) == []


def test_download_failure_cleans_partials_keeps_nothing_else(fake_ydl, tmp_path):
    (tmp_path / f"{VID}.f137.mp4.part").write_bytes(b"half")
    (tmp_path / "otherVideo1.mp4").write_bytes(b"other cache")
    fake_ydl.raise_on_extract = yt_dlp.utils.DownloadError("ERROR: \x1b[0;31mVideo unavailable\x1b[0m")
    with pytest.raises(DownloadError) as exc:
        downloader.download(VID, tmp_path)
    assert str(exc.value) == "Video unavailable"
    assert [p.name for p in tmp_path.iterdir()] == ["otherVideo1.mp4"]


def test_download_missing_output_is_failure(fake_ydl, tmp_path):
    fake_ydl.video_info = H264
    fake_ydl.write_file = False
    with pytest.raises(DownloadError, match="left no"):
        downloader.download(VID, tmp_path)


@pytest.mark.parametrize("bad", ["../../etc/pa", "abc", "a" * 12, "abc def ghi", ""])
def test_invalid_video_id_rejected(fake_ydl, tmp_path, bad):
    with pytest.raises(DownloadError, match="invalid"):
        downloader.download(bad, tmp_path)
    assert fake_ydl.calls == []
