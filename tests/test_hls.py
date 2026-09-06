import json
import subprocess
from pathlib import Path

import pytest

from src.downloader import hls as hls_module
from src.downloader.hls import HLSDownloadCancelled, HLSDownloadError, HLSDownloader, HLSResult, parse_attributes, select_variant


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


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakePlaylistClient:
    def __init__(self, playlists: dict[str, str], resource: bytes) -> None:
        self.playlists = playlists
        self.resource = resource
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, headers: dict[str, str]):
        self.requests.append((url, headers))
        if url in self.playlists:
            return _FakeResponse(self.playlists[url].encode("utf-8"))
        return _FakeResponse(self.resource)


def test_hls_materializes_fmp4_map_and_byte_ranges_as_exact_local_fragments(tmp_path: Path) -> None:
    resource = b"INITSEGONESEGTWO"
    playlist_url = "https://cdn.test/video.m3u8"
    playlist = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MAP:URI="video.cmfv",BYTERANGE="4@0"
#EXTINF:1,
#EXT-X-BYTERANGE:6@4
video.cmfv
#EXTINF:1,
#EXT-X-BYTERANGE:6@10
video.cmfv
#EXT-X-ENDLIST
"""
    client = _FakePlaylistClient({playlist_url: playlist}, resource)
    downloader = HLSDownloader(client, {}, 1, lambda: False, lambda: None)

    manifest, duration = downloader._materialize_playlist(playlist_url, tmp_path, "video.m3u8")

    assert duration == 2.0
    text = manifest.read_text(encoding="utf-8")
    assert "#EXT-X-BYTERANGE" not in text
    assert '#EXT-X-MAP:URI="' in text
    local_files = sorted(tmp_path.glob("part-*"))
    assert [path.read_bytes() for path in local_files] == [b"INIT", b"SEGONE", b"SEGTWO"]
    assert any(headers.get("Range") == "bytes=0-3" for _url, headers in client.requests)


def test_hls_materializes_mpeg_ts_playlist_without_range_metadata(tmp_path: Path) -> None:
    playlist_url = "https://cdn.test/video.ts.m3u8"
    playlist = """#EXTM3U
#EXTINF:2,
part-000.ts
#EXTINF:2,
part-001.ts
#EXT-X-ENDLIST
"""
    client = _FakePlaylistClient({playlist_url: playlist}, b"MPEG-TS")
    downloader = HLSDownloader(client, {}, 1, lambda: False, lambda: None)

    manifest, duration = downloader._materialize_playlist(playlist_url, tmp_path, "video.m3u8")

    assert duration == 4.0
    assert "#EXT-X-BYTERANGE" not in manifest.read_text(encoding="utf-8")
    assert [path.read_bytes() for path in sorted(tmp_path.glob("part-*"))] == [b"MPEG-TS", b"MPEG-TS"]


class _ValidationProcess:
    def __init__(self, command: list[str], *, probe: dict | None, decode_returncode: int, decode_stderr: str) -> None:
        self.command = command
        self.probe = probe
        self.decode_returncode = decode_returncode
        self.decode_stderr = decode_stderr
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        if self.command[0] == "ffprobe.exe":
            self.returncode = 0
            return json.dumps(self.probe or {}), ""
        self.returncode = self.decode_returncode
        return "", self.decode_stderr

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _patch_validation_processes(monkeypatch, *, decode_returncode: int = 0, decode_stderr: str = ""):
    processes: list[_ValidationProcess] = []
    probe = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 720, "height": 1278, "duration": "25.12"},
            {"codec_type": "audio", "codec_name": "aac", "duration": "25.12"},
        ],
        "format": {"duration": "25.12"},
    }

    def factory(command, **_kwargs):
        process = _ValidationProcess(
            command,
            probe=probe,
            decode_returncode=decode_returncode,
            decode_stderr=decode_stderr,
        )
        processes.append(process)
        return process

    monkeypatch.setattr(hls_module, "find_ffmpeg", lambda: "ffmpeg.exe")
    monkeypatch.setattr(hls_module, "find_ffprobe", lambda: "ffprobe.exe")
    monkeypatch.setattr(hls_module.subprocess, "Popen", factory)
    return processes


def test_hls_valid_output_passes_probe_and_decode_gate(monkeypatch, tmp_path: Path) -> None:
    processes = _patch_validation_processes(monkeypatch)
    output = tmp_path / "valid.mp4"
    output.write_bytes(b"valid")
    downloader = HLSDownloader(None, {}, 1, lambda: False, lambda: None)

    result = downloader.validate_output(output, HLSResult(duration=2.0, width=1, height=1))

    assert result.duration == 25.12
    assert result.width == 720
    assert result.height == 1278
    assert [process.command[0] for process in processes] == ["ffprobe.exe", "ffmpeg.exe"]


def test_hls_corrupt_h264_or_aac_fails_decode_gate(monkeypatch, tmp_path: Path) -> None:
    processes = _patch_validation_processes(monkeypatch, decode_returncode=1, decode_stderr="Invalid NAL unit size")
    output = tmp_path / "corrupt.mp4"
    output.write_bytes(b"corrupt")
    downloader = HLSDownloader(None, {}, 1, lambda: False, lambda: None)

    with pytest.raises(HLSDownloadError, match="decode 失败"):
        downloader.validate_output(output, HLSResult(duration=2.0))

    assert output.exists()
    assert processes[-1].command[0] == "ffmpeg.exe"


def test_hls_missing_video_stream_fails_probe_gate(monkeypatch, tmp_path: Path) -> None:
    _patch_validation_processes(monkeypatch)
    output = tmp_path / "audio-only.mp4"
    output.write_bytes(b"audio")
    downloader = HLSDownloader(None, {}, 1, lambda: False, lambda: None)
    monkeypatch.setattr(
        hls_module.subprocess,
        "Popen",
        lambda command, **_kwargs: _ValidationProcess(
            command,
            probe={"streams": [{"codec_type": "audio"}], "format": {"duration": "4.0"}},
            decode_returncode=0,
            decode_stderr="",
        ),
    )

    with pytest.raises(HLSDownloadError, match="video stream"):
        downloader.validate_output(output, HLSResult(duration=2.0))


def test_hls_stop_during_integrity_decode_terminates_process(monkeypatch, tmp_path: Path) -> None:
    state = {"stop": False}
    processes: list[_ValidationProcess] = []

    class StopProcess(_ValidationProcess):
        def communicate(self, timeout=None):
            if self.command[0] == "ffprobe.exe":
                self.returncode = 0
                return json.dumps({"streams": [{"codec_type": "video", "duration": "2"}], "format": {"duration": "2"}}), ""
            state["stop"] = True
            raise subprocess.TimeoutExpired(self.command[0], timeout)

    def factory(command, **_kwargs):
        process = StopProcess(command, probe={}, decode_returncode=0, decode_stderr="")
        processes.append(process)
        return process

    monkeypatch.setattr(hls_module, "find_ffmpeg", lambda: "ffmpeg.exe")
    monkeypatch.setattr(hls_module, "find_ffprobe", lambda: "ffprobe.exe")
    monkeypatch.setattr(hls_module.subprocess, "Popen", factory)
    output = tmp_path / "stopped.mp4"
    output.write_bytes(b"partial")
    downloader = HLSDownloader(None, {}, 1, lambda: state["stop"], lambda: None)

    with pytest.raises(HLSDownloadCancelled):
        downloader.validate_output(output, HLSResult(duration=2.0))

    assert len(processes) == 2
    assert processes[-1].terminated is True
