import subprocess

import pytest

from src.downloader import hls as hls_module
from src.downloader.hls import HLSDownloadCancelled, HLSDownloader, parse_attributes, select_variant


def test_hls_selects_highest_bandwidth_variant_and_audio_group() -> None:
    master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-128000",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=640x360,AUDIO="audio-128000"
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1500000,RESOLUTION=1280x720,AUDIO="audio-128000"
high.m3u8
"""
    selected = select_variant(master, "https://video.twimg.com/amplify_video/1/pl/master.m3u8")
    assert selected[0].endswith("high.m3u8")
    assert selected[1:4] == (1280, 720, 1500000)
    assert selected[4].endswith("audio.m3u8")


def test_hls_prefers_highest_resolution_before_bandwidth() -> None:
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1280x720
720.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1920x1080
1080.m3u8
"""

    selected = select_variant(master, "https://video.twimg.com/amplify_video/1/pl/master.m3u8")

    assert selected[:4] == ("https://video.twimg.com/amplify_video/1/pl/1080.m3u8", 1920, 1080, 1000000)


def test_hls_prefers_bandwidth_for_same_or_missing_resolution() -> None:
    same_resolution = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
high.m3u8
"""
    missing_resolution = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=1000000
low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000
high.m3u8
"""

    assert select_variant(same_resolution, "https://video.twimg.com/master.m3u8")[0].endswith("high.m3u8")
    assert select_variant(missing_resolution, "https://video.twimg.com/master.m3u8")[0].endswith("high.m3u8")


def test_hls_attributes_strip_quotes() -> None:
    assert parse_attributes('BANDWIDTH=1234,RESOLUTION="640x360",AUDIO="a"') == {
        "BANDWIDTH": "1234",
        "RESOLUTION": "640x360",
        "AUDIO": "a",
    }


def test_hls_terminates_ffmpeg_when_stop_is_requested(monkeypatch, tmp_path) -> None:
    state = {"stop": False}
    processes = []

    class FakeProcess:
        def __init__(self, *_args, **_kwargs) -> None:
            self.returncode = None
            self.terminated = False
            processes.append(self)

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            state["stop"] = True
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr(hls_module, "find_ffmpeg", lambda: "ffmpeg.exe")
    monkeypatch.setattr(hls_module.subprocess, "Popen", FakeProcess)
    downloader = HLSDownloader(
        client=None,
        headers={},
        retry_count=1,
        should_stop=lambda: state["stop"],
        should_pause=lambda: None,
    )
    monkeypatch.setattr(downloader, "_get_text", lambda _url: "#EXTM3U\n")
    monkeypatch.setattr(
        downloader,
        "_materialize_playlist",
        lambda _url, _directory, _filename: (tmp_path / "video.m3u8", 1.0),
    )

    with pytest.raises(HLSDownloadCancelled):
        downloader.download("https://video.twimg.com/master.m3u8", tmp_path / "out.mp4")

    assert len(processes) == 1
    assert processes[0].terminated is True
