from pathlib import Path

import pytest

from src.adapters.youtube import YouTubeAdapter, YouTubeDownload
from src.adapters.youtube import YouTubeCancelled, YouTubeError
from src.browser.collector import ImageCandidate
from src.downloader.engine import DownloadEngine
from src.models import TaskConfig
from src.storage import CollectorDatabase
from src.utils.youtube import youtube_url_kind, youtube_video_id


@pytest.mark.parametrize(("url", "kind", "video_id"), [
    ("https://www.youtube.com/watch?v=abcDEF_1234", "single_video", "abcDEF_1234"),
    ("https://youtu.be/abcDEF_1234", "single_video", "abcDEF_1234"),
    ("https://www.youtube.com/shorts/abcDEF_1234", "single_video", "abcDEF_1234"),
    ("https://www.youtube.com/playlist?list=PL123", "playlist", ""),
    ("https://www.youtube.com/@creator/videos", "channel", ""),
])
def test_youtube_url_recognition(url: str, kind: str, video_id: str) -> None:
    assert youtube_url_kind(url) == kind
    assert youtube_video_id(url) == video_id


def test_youtube_playlist_watch_url_is_playlist() -> None:
    assert youtube_url_kind("https://www.youtube.com/watch?v=abcDEF_1234&list=PL123") == "playlist"


def test_youtube_adapter_entries_use_video_id_as_stable_identity() -> None:
    class Ydl:
        def __init__(self, _options): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=False):
            assert not download
            return {"entries": [
                {"id": "sameID123", "url": "sameID123", "title": "first"},
                {"id": "nextID123", "webpage_url": "https://youtu.be/nextID123", "title": "next"},
            ]}

    candidates = list(YouTubeAdapter(Ydl).entries("https://www.youtube.com/playlist?list=PL123"))
    assert [candidate.media_identity for candidate in candidates] == ["sameID123", "nextID123"]
    assert candidates[0].media_url == "https://www.youtube.com/watch?v=sameID123"


@pytest.mark.parametrize(("quality", "expected"), [("best", "bestvideo+bestaudio/best/best"), ("1080", "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best")])
def test_youtube_quality_format_selection(quality: str, expected: str) -> None:
    assert YouTubeAdapter._format(quality, "video") == expected


def test_youtube_config_accepts_public_channel_and_persists_quality(tmp_path: Path) -> None:
    config = TaskConfig("", 2, tmp_path, platform="youtube", collection_mode="channel", source_identifier="https://www.youtube.com/@creator/shorts", media_type="video", video_quality="720")
    config.validate()
    assert TaskConfig.from_dict({**config.__dict__, "output_root": str(tmp_path)}).video_quality == "720"


def test_youtube_config_rejects_search(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        TaskConfig("", 1, tmp_path, platform="youtube", collection_mode="auto", source_identifier="cats").validate()


def test_youtube_old_task_config_defaults_cookie_settings(tmp_path: Path) -> None:
    config = TaskConfig.from_dict({"target_count": 1, "output_root": str(tmp_path), "platform": "youtube", "collection_mode": "auto", "source_identifier": "https://youtu.be/abcDEF_1234", "media_type": "video"})
    assert not config.youtube_use_browser_cookies
    assert config.youtube_cookie_source == "none"
    assert config.youtube_browser == "chrome"
    assert config.youtube_browser_profile == "Default"


def test_youtube_cookie_and_timeout_options_are_sent_to_yt_dlp(tmp_path: Path, monkeypatch) -> None:
    captured = {}
    class Ydl:
        def __init__(self, options): captured.update(options)
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=True):
            path = tmp_path / "0001_title [abcDEF_1234].mp4"
            path.write_bytes(b"video")
            return {"id": "abcDEF_1234"}

    monkeypatch.setattr("src.adapters.youtube.find_ffmpeg", lambda: str(tmp_path / "ffmpeg.exe"))
    candidate = ImageCandidate("", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234")
    result = YouTubeAdapter(Ydl).download(candidate, tmp_path, 1, media_type="video", quality="720", audio_format="best", should_stop=lambda: False, should_pause=lambda: None, on_log=lambda _text: None, cookie_source="chrome")

    assert result.path.exists()
    assert captured["cookiesfrombrowser"] == ("chrome", "Default")
    assert captured["socket_timeout"] == 20
    assert captured["extractor_retries"] == 2


def test_youtube_download_uses_merged_file_when_intermediate_streams_are_gone(tmp_path: Path, monkeypatch) -> None:
    class Ydl:
        def __init__(self, _options): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=True):
            merged = tmp_path / "0001_title [abcDEF_1234].mp4"
            merged.write_bytes(b"merged-video")
            return {
                "filepath": str(tmp_path / "0001_title [abcDEF_1234].f137.mp4"),
                "requested_downloads": [{"filepath": str(tmp_path / "0001_title [abcDEF_1234].f140.m4a")}],
            }

    monkeypatch.setattr("src.adapters.youtube.find_ffmpeg", lambda: str(tmp_path / "ffmpeg.exe"))
    candidate = ImageCandidate("", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234")
    result = YouTubeAdapter(Ydl).download(candidate, tmp_path, 1, media_type="video", quality="best", audio_format="best", should_stop=lambda: False, should_pause=lambda: None, on_log=lambda _text: None)

    assert result.path.name.endswith(".mp4")
    assert result.path.read_bytes() == b"merged-video"


def test_youtube_download_does_not_report_success_without_a_real_output(tmp_path: Path, monkeypatch) -> None:
    class Ydl:
        def __init__(self, _options): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=True):
            return {"filepath": str(tmp_path / "deleted [abcDEF_1234].mp4")}

    monkeypatch.setattr("src.adapters.youtube.find_ffmpeg", lambda: str(tmp_path / "ffmpeg.exe"))
    candidate = ImageCandidate("", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234")
    with pytest.raises(YouTubeError, match="未生成输出文件"):
        YouTubeAdapter(Ydl).download(candidate, tmp_path, 1, media_type="video", quality="best", audio_format="best", should_stop=lambda: False, should_pause=lambda: None, on_log=lambda _text: None)


def test_youtube_single_success_updates_counts_metadata_and_database(tmp_path: Path, monkeypatch) -> None:
    candidate = ImageCandidate(
        "", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234",
        post_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234", media_identity="abcDEF_1234",
    )

    class Adapter:
        def entries(self, *_args, **_kwargs):
            yield candidate

        def download(self, _candidate, output_dir, _index, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            merged = output_dir / "0001_title [abcDEF_1234].mp4"
            merged.write_bytes(b"merged-video")
            return YouTubeDownload(merged, {"id": "abcDEF_1234", "width": 1920, "height": 1080})

        @staticmethod
        def file_hash(_path): return "youtube-final-hash"

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    config = TaskConfig("", 1, tmp_path, platform="youtube", collection_mode="single_video", source_identifier="https://youtu.be/abcDEF_1234", media_type="video")
    database = CollectorDatabase(tmp_path / "collector.db")
    snapshots, logs = [], []
    task_dir = DownloadEngine(config, tmp_path / "profile", on_log=logs.append, on_stats=snapshots.append, database=database).run()

    assert snapshots[-1].downloaded == 1
    assert snapshots[-1].current_status == "任务完成"
    assert any("成功保存 1/1" in line for line in logs)
    assert not any("YouTube 已无更多条目" in line for line in logs)
    assert ",downloaded," in (task_dir / "metadata.csv").read_text(encoding="utf-8-sig")
    assert database.get_history(config.task_id)["success_count"] == 1
    assert database.find_duplicate(candidate, config.canonical_source_value, require_local_file=True) is not None


def test_youtube_empty_entries_reports_no_more_items(tmp_path: Path, monkeypatch) -> None:
    class Adapter:
        def entries(self, *_args, **_kwargs):
            return iter(())

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    config = TaskConfig("", 1, tmp_path, platform="youtube", collection_mode="single_video", source_identifier="https://youtu.be/abcDEF_1234", media_type="video")
    snapshots = []
    DownloadEngine(config, tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "collector.db")).run()

    assert snapshots[-1].downloaded == 0
    assert snapshots[-1].current_status == "采集结束（YouTube 已无更多条目）"


def test_youtube_stop_hooks_cancel_without_recording_normal_failure(tmp_path: Path, monkeypatch) -> None:
    captured = {}
    stopped = False
    class Ydl:
        def __init__(self, options): captured.update(options)
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=True):
            path = tmp_path / "0001_title [abcDEF_1234].mp4"
            path.write_bytes(b"video")
            return {}

    monkeypatch.setattr("src.adapters.youtube.find_ffmpeg", lambda: str(tmp_path / "ffmpeg.exe"))
    candidate = ImageCandidate("", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234")
    YouTubeAdapter(Ydl).download(candidate, tmp_path, 1, media_type="video", quality="best", audio_format="best", should_stop=lambda: stopped, should_pause=lambda: None, on_log=lambda _text: None)
    stopped = True
    with pytest.raises(YouTubeCancelled):
        captured["progress_hooks"][0]({"status": "downloading"})
    with pytest.raises(YouTubeCancelled):
        captured["postprocessor_hooks"][0]({"status": "started"})
    # Postprocessor hooks use the same cancellation policy and are retained in options.
    assert captured["postprocessor_hooks"]


def test_youtube_cookie_read_failure_has_clear_error(tmp_path: Path, monkeypatch) -> None:
    class Ydl:
        def __init__(self, _options): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=True): raise RuntimeError("chrome cookie database locked")

    monkeypatch.setattr("src.adapters.youtube.find_ffmpeg", lambda: str(tmp_path / "ffmpeg.exe"))
    candidate = ImageCandidate("", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234")
    with pytest.raises(YouTubeError, match="Chrome Cookies"):
        YouTubeAdapter(Ydl).download(candidate, tmp_path, 1, media_type="video", quality="best", audio_format="best", should_stop=lambda: False, should_pause=lambda: None, on_log=lambda _text: None, cookie_source="chrome")


def test_youtube_firefox_and_cookiefile_options(tmp_path: Path) -> None:
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert YouTubeAdapter._cookie_options("firefox") == {"cookiesfrombrowser": ("firefox",)}
    assert YouTubeAdapter._cookie_options("file", cookie_file=str(cookie_file)) == {"cookiefile": str(cookie_file)}
    with pytest.raises(YouTubeError, match="不存在"):
        YouTubeAdapter._cookie_options("file", cookie_file=str(tmp_path / "missing.txt"))


def test_youtube_chrome_dpapi_error_is_friendly() -> None:
    error = YouTubeAdapter._cookie_error("chrome", "Failed to decrypt with DPAPI")
    assert error is not None and "Firefox Cookies" in str(error)


def test_youtube_ejs_options_use_deno_runtime(monkeypatch) -> None:
    monkeypatch.setattr("src.adapters.youtube.find_deno", lambda: "C:/tools/deno.exe")
    assert YouTubeAdapter._ejs_options() == {"js_runtimes": {"deno": {"path": "C:/tools/deno.exe"}}}


def test_youtube_ejs_options_explain_missing_deno(monkeypatch) -> None:
    monkeypatch.setattr("src.adapters.youtube.find_deno", lambda: None)
    with pytest.raises(YouTubeError, match="Deno"):
        YouTubeAdapter._ejs_options()


def test_requirements_pin_ytdlp_and_ejs() -> None:
    requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "yt-dlp==2026.8.19" in requirements
    assert "yt-dlp-ejs==0.8.0" in requirements


def test_youtube_legacy_chrome_cookie_config_maps_to_source(tmp_path: Path) -> None:
    config = TaskConfig.from_dict({"target_count": 1, "output_root": str(tmp_path), "platform": "youtube", "collection_mode": "auto", "source_identifier": "https://youtu.be/abcDEF_1234", "media_type": "video", "youtube_use_browser_cookies": True, "youtube_browser": "chrome"})
    assert config.youtube_cookie_source == "chrome"
