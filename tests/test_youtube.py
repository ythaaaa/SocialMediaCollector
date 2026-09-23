import json
from pathlib import Path

import pytest

from src.adapters.youtube import YouTubeAdapter, YouTubeDownload
from src.adapters.youtube import YouTubeCancelled, YouTubeError
from src.browser.collector import ImageCandidate
from src.browser.youtube_shorts import YouTubeShortsBrowserCollector, _ShortsSearchState
from src.downloader.engine import DownloadEngine
from src.models import TaskConfig
from src.storage import CollectorDatabase
from src.utils.youtube import normalize_youtube_search_keywords, youtube_url_kind, youtube_video_id


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


def test_youtube_fragment_concurrency_is_only_enabled_for_fragment_protocols() -> None:
    https_info = {"requested_formats": [{"format_id": "401", "protocol": "https"}]}
    dash_info = {"requested_formats": [{"format_id": "137", "protocol": "http_dash_segments"}]}
    assert YouTubeAdapter._fragment_concurrency(https_info) == 1
    assert YouTubeAdapter._fragment_concurrency(dash_info) == 4


def test_youtube_download_logs_safe_plan_and_enables_fragment_concurrency(tmp_path: Path, monkeypatch) -> None:
    options_seen = []

    class Ydl:
        def __init__(self, options):
            self.options = options
            options_seen.append(options)
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=True):
            if not download:
                return {
                    "title": "A title", "duration": 65, "media_type": "short",
                    "requested_formats": [
                        {"format_id": "137", "height": 1080, "protocol": "http_dash_segments", "filesize_approx": 100 * 1024 * 1024},
                        {"format_id": "140", "protocol": "http_dash_segments", "filesize": 10 * 1024 * 1024},
                    ],
                }
            path = tmp_path / "0001_title [abcDEF_1234].mp4"
            path.write_bytes(b"video")
            return {"filepath": str(path)}

    monkeypatch.setattr("src.adapters.youtube.find_ffmpeg", lambda: str(tmp_path / "ffmpeg.exe"))
    logs = []
    candidate = ImageCandidate("", "", platform="youtube", media_url="https://youtu.be/abcDEF_1234", post_id="abcDEF_1234")
    YouTubeAdapter(Ydl).download(candidate, tmp_path, 1, media_type="video", quality="1080", audio_format="best", should_stop=lambda: False, should_pause=lambda: None, on_log=logs.append)

    assert options_seen[0]["skip_download"] is True
    assert options_seen[1]["concurrent_fragment_downloads"] == 4
    assert "标题=A title" in logs[0]
    assert "时长=00:01:05" in logs[0]
    assert "媒体类型=short" in logs[0]
    assert "分辨率=1080p" in logs[0]
    assert "协议=http_dash_segments" in logs[0]
    assert "分片并发=4" in logs[0]


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


def test_youtube_search_keywords_normalize_blank_and_duplicate_lines() -> None:
    assert normalize_youtube_search_keywords(" car restoration \n\nCAR restoration\nhouse renovation\n") == (
        "car restoration", "house renovation",
    )


def test_youtube_search_uses_ytdlp_ytsearch_relevance_extractor() -> None:
    captured = {}

    class Ydl:
        def __init__(self, options): captured.update(options)
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, url, download=False):
            captured["url"] = url
            assert not download
            return {"entries": [{"id": "abcDEF_1234", "url": "abcDEF_1234", "title": "result", "duration": 120}]}

    candidates = list(YouTubeAdapter(Ydl).search("car restoration", 25))

    assert captured["url"] == "ytsearch25:car restoration"
    assert captured["extract_flat"] == "in_playlist"
    assert candidates[0].media_identity == "abcDEF_1234"
    assert candidates[0].source == "car restoration"


@pytest.mark.parametrize(("content_type", "expected"), [
    ("all", "ytsearch20:car restoration"),
    ("shorts", "https://www.youtube.com/results?search_query=car+restoration&sp=EgIQCQ%3D%3D"),
    ("video", "https://www.youtube.com/results?search_query=car+restoration&sp=EgIQAQ%3D%3D"),
])
def test_youtube_search_uses_type_specific_source(content_type: str, expected: str) -> None:
    assert YouTubeAdapter.build_search_source("car restoration", content_type, 20) == expected


def test_youtube_search_passes_filtered_source_to_ytdlp_search_url_extractor() -> None:
    captured = {}

    class Ydl:
        def __init__(self, _options): pass
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, url, download=False):
            captured["url"] = url
            return {"entries": []}

    assert list(YouTubeAdapter(Ydl).search("car restoration", 20, content_type="shorts")) == []
    assert captured["url"].startswith("https://www.youtube.com/results?")
    assert "sp=EgIQCQ%3D%3D" in captured["url"]


def test_youtube_search_url_falsey_iterable_entries_are_not_dropped() -> None:
    captured = {}

    class LazyEntries:
        def __bool__(self): return False
        def __iter__(self):
            return iter([{
                "id": "shortID001", "url": "https://www.youtube.com/shorts/shortID001",
                "title": "Short result", "ie_key": "YoutubeSearchURL",
            }])

    class Ydl:
        def __init__(self, options): captured.update(options)
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=False):
            assert not download
            return {"_type": "playlist", "id": "car restoration", "entries": LazyEntries()}

    candidates = list(YouTubeAdapter(Ydl).search("car restoration", 20, content_type="shorts"))
    assert captured["playlistend"] == 20
    assert [candidate.post_id for candidate in candidates] == ["shortID001"]
    assert candidates[0].post_url == "https://www.youtube.com/watch?v=shortID001"


def test_youtube_filtered_search_retries_public_discovery_after_empty_cookie_result() -> None:
    options_seen = []

    class Ydl:
        def __init__(self, options):
            self.options = options
            options_seen.append(options)
        def __enter__(self): return self
        def __exit__(self, *_args): return None
        def extract_info(self, _url, download=False):
            assert not download
            if self.options.get("cookiesfrombrowser"):
                return {"_type": "playlist", "entries": []}
            return {"_type": "playlist", "entries": [{
                "id": "shortID002", "url": "https://www.youtube.com/shorts/shortID002", "title": "public short",
            }]}

    candidates = list(YouTubeAdapter(Ydl).search("car restoration", 20, content_type="shorts", cookie_source="firefox"))
    assert len(options_seen) == 2
    assert options_seen[0]["cookiesfrombrowser"] == ("firefox",)
    assert "cookiesfrombrowser" not in options_seen[1]
    assert [candidate.post_id for candidate in candidates] == ["shortID002"]


def test_youtube_search_flat_entries_keep_distinct_canonical_video_identities() -> None:
    entries = [
        {"id": f"video{i:06d}", "url": f"video{i:06d}", "title": f"result {i}"}
        for i in range(20)
    ]
    candidates = [
        YouTubeAdapter._candidate_from_entry(entry, "ytsearch:cars", "youtube_search", "cars")
        for entry in entries
    ]

    assert all(candidates)
    assert {candidate.post_id for candidate in candidates} == {f"video{i:06d}" for i in range(20)}
    assert {candidate.media_url for candidate in candidates} == {
        f"https://www.youtube.com/watch?v=video{i:06d}" for i in range(20)
    }


@pytest.mark.parametrize("entry", [
    {"id": "sameVID01", "webpage_url": "https://www.youtube.com/watch?v=sameVID01"},
    {"id": "sameVID01", "url": "https://youtu.be/sameVID01"},
    {"id": "sameVID01", "url": "https://www.youtube.com/shorts/sameVID01"},
])
def test_youtube_search_url_variants_share_only_the_same_video_identity(entry: dict) -> None:
    candidate = YouTubeAdapter._candidate_from_entry(entry, "ytsearch:cars", "youtube_search")
    assert candidate is not None
    assert candidate.post_id == "sameVID01"
    assert candidate.media_url == "https://www.youtube.com/watch?v=sameVID01"


def test_youtube_search_falls_back_to_url_id_and_rejects_unidentifiable_entry() -> None:
    from_url = YouTubeAdapter._candidate_from_entry(
        {"url": "https://www.youtube.com/shorts/fromURL01"}, "ytsearch:cars", "youtube_search",
    )
    missing = YouTubeAdapter._candidate_from_entry(
        {"title": "no usable id", "url": "https://www.youtube.com/results?search_query=cars"},
        "ytsearch:cars", "youtube_search",
    )
    assert from_url is not None and from_url.post_id == "fromURL01"
    assert missing is None


def test_youtube_task_dedupe_uses_video_id_not_query_free_watch_path(tmp_path: Path) -> None:
    engine = DownloadEngine(_search_config(tmp_path, ("cars",)), tmp_path / "profile")
    candidates = [
        _search_candidate(video_id, "cars")
        for video_id in ("videoA01", "videoB02", "videoC03")
    ]
    keys = [engine._current_candidate_keys(candidate) for candidate in candidates]

    assert keys == [
        {("identity", "youtube|videoA01")},
        {("identity", "youtube|videoB02")},
        {("identity", "youtube|videoC03")},
    ]
    assert engine._current_candidate_keys(ImageCandidate("", "", platform="youtube", media_url="https://youtube.com/watch")) == set()


@pytest.mark.parametrize(("target", "expected"), [(1, 20), (20, 60), (50, 100), (100, 150)])
def test_youtube_search_candidate_budget_is_bounded(target: int, expected: int) -> None:
    assert DownloadEngine._youtube_search_budget(target) == expected


def test_youtube_search_task_config_roundtrip_and_legacy_compatibility(tmp_path: Path) -> None:
    config = TaskConfig(
        "", 5, tmp_path, platform="youtube", collection_mode="youtube_search",
        youtube_search_keywords=("car restoration", "house renovation"),
        youtube_search_content_type="shorts", youtube_search_max_duration=300, youtube_search_date_range="30d", media_type="video",
    )
    config.validate()
    restored = TaskConfig.from_dict({**config.__dict__, "output_root": str(tmp_path)})
    assert restored.youtube_search_keywords == ("car restoration", "house renovation")
    assert restored.youtube_search_max_duration == 300
    assert restored.youtube_search_date_range == "30d"
    assert restored.youtube_search_content_type == "shorts"
    old = TaskConfig.from_dict({"target_count": 1, "output_root": str(tmp_path), "platform": "youtube", "collection_mode": "single_video", "source_identifier": "https://youtu.be/abcDEF_1234", "media_type": "video"})
    assert old.youtube_search_keywords == ()
    assert old.youtube_search_content_type == "all"


def _search_candidate(
    video_id: str, keyword: str, *, duration: float | None = None, upload_date: str = "", content_type: str = "",
) -> ImageCandidate:
    return ImageCandidate(
        "", "", platform="youtube", media_url=f"https://youtu.be/{video_id}", post_url=f"https://youtu.be/{video_id}",
        post_id=video_id, media_identity=video_id, post_text=video_id, source=keyword,
        duration=duration, posted_at=upload_date, source_type="youtube_search", content_type=content_type,
    )


def _search_config(tmp_path: Path, keywords: tuple[str, ...], target: int = 2, **kwargs) -> TaskConfig:
    return TaskConfig("", target, tmp_path, platform="youtube", collection_mode="youtube_search", youtube_search_keywords=keywords, media_type="video", **kwargs)


def test_youtube_search_round_robin_and_target_success_driven(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []
    pools = {
        "cars": [_search_candidate("car001", "cars"), _search_candidate("car002", "cars")],
        "houses": [_search_candidate("house001", "houses"), _search_candidate("house002", "houses")],
    }

    class Adapter:
        def search(self, keyword, _limit, **_kwargs): return iter(pools[keyword])
        def download(self, candidate, output_dir, index, **_kwargs):
            downloads.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(candidate.post_id.encode())
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(path): return path.read_text()

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    config = _search_config(tmp_path, ("cars", "houses"), target=2)
    task_dir = DownloadEngine(config, tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite")).run()

    assert downloads == ["car001", "house001"]
    assert snapshots[-1].downloaded == 2
    assert snapshots[-1].current_status == "任务完成"
    assert json.loads((task_dir / "task.json").read_text(encoding="utf-8"))["youtube_search_keywords"] == ["cars", "houses"]


def test_youtube_search_processes_three_unique_flat_candidates_to_target(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **_kwargs):
            return iter([_search_candidate(f"unique{i:02d}", "cars") for i in range(20)])
        def download(self, candidate, output_dir, index, **_kwargs):
            downloads.append(candidate.post_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"
            path.write_bytes(candidate.post_id.encode())
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(path): return path.read_text()

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=3), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert downloads == ["unique00", "unique01", "unique02"]
    assert snapshots[-1].downloaded == 3
    assert snapshots[-1].skipped == 0
    assert snapshots[-1].current_status == "任务完成"


def test_youtube_search_expands_after_first_twenty_are_filtered(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    downloaded: list[str] = []

    class Adapter:
        def search(self, _keyword, limit, **_kwargs):
            calls.append(limit)
            candidates = [_search_candidate(f"long{i:03d}", "cars", duration=301) for i in range(20)]
            if limit >= 40:
                candidates += [_search_candidate("good021", "cars", duration=120), _search_candidate("good022", "cars", duration=120)]
            return iter(candidates)
        def metadata(self, candidate, **_kwargs): return {"duration": candidate.duration}
        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(_path): return "hash-" + str(len(downloaded))

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=2, youtube_search_max_duration=300), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert calls == [20, 40]
    assert downloaded == ["good021", "good022"]
    assert snapshots[-1].downloaded == 2
    assert snapshots[-1].skipped == 20
    assert snapshots[-1].current_status == "任务完成"


def test_youtube_search_reaches_candidate_cap_with_distinct_status(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []

    class Adapter:
        def search(self, _keyword, limit, **_kwargs):
            calls.append(limit)
            return iter([_search_candidate(f"item{i:03d}", "cars", duration=301) for i in range(limit)])
        def metadata(self, candidate, **_kwargs): return {"duration": candidate.duration}

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=2, youtube_search_max_duration=300), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert calls == list(range(20, 151, 20)) + [150]
    assert snapshots[-1].downloaded == 0
    assert "安全上限" in snapshots[-1].current_status


def test_youtube_search_content_type_uses_metadata_not_duration(tmp_path: Path) -> None:
    shorts = _search_candidate("short001", "cars", duration=600, content_type="short")
    short_video = _search_candidate("video001", "cars", duration=30, content_type="video")
    unknown = _search_candidate("unknown01", "cars", duration=30)

    shorts_engine = DownloadEngine(_search_config(tmp_path, ("cars",), youtube_search_content_type="shorts"), tmp_path / "profile")
    assert shorts_engine._youtube_search_filter_reason(shorts) == ""
    assert "普通视频" in shorts_engine._youtube_search_filter_reason(short_video)
    assert "无法确认" in shorts_engine._youtube_search_filter_reason(unknown)

    video_engine = DownloadEngine(_search_config(tmp_path, ("cars",), youtube_search_content_type="video"), tmp_path / "profile")
    assert "Shorts" in video_engine._youtube_search_filter_reason(shorts)
    assert video_engine._youtube_search_filter_reason(short_video) == ""

    all_engine = DownloadEngine(_search_config(tmp_path, ("cars",), youtube_search_content_type="all"), tmp_path / "profile")
    assert all_engine._youtube_search_filter_reason(shorts) == ""
    assert all_engine._youtube_search_filter_reason(short_video) == ""


def test_youtube_search_shorts_filter_expands_until_target(tmp_path: Path, monkeypatch) -> None:
    calls: list[int] = []
    content_types: list[str] = []
    downloaded: list[str] = []

    class Adapter:
        def search(self, _keyword, limit, **_kwargs):
            calls.append(limit)
            content_types.append(_kwargs["content_type"])
            candidates = [_search_candidate(f"video{i:03d}", "cars") for i in range(20)]
            if limit >= 40:
                candidates += [_search_candidate("short021", "cars"), _search_candidate("short022", "cars")]
            return iter(candidates)
        def metadata(self, candidate, **_kwargs):
            return {"media_type": "short" if candidate.post_id.startswith("short") else "video"}
        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=2, youtube_search_content_type="shorts"), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()
    assert calls == [20, 40]
    assert content_types == ["shorts", "shorts"]
    assert downloaded == ["short021", "short022"]
    assert snapshots[-1].downloaded == 2


def test_youtube_search_shorts_filtered_source_falls_back_to_generic_search(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, int]] = []
    downloaded: list[str] = []

    class Adapter:
        def search(self, _keyword, limit, **kwargs):
            calls.append((kwargs["content_type"], limit))
            if kwargs["content_type"] == "shorts":
                return iter([_search_candidate("short001", "cars")])
            return iter([
                _search_candidate("short001", "cars"),  # cross-source duplicate
                _search_candidate("video001", "cars"),
                _search_candidate("short002", "cars"),
                _search_candidate("short003", "cars"),
            ])

        @staticmethod
        def metadata(candidate, **_kwargs):
            return {"media_type": "short" if candidate.post_id.startswith("short") else "video"}

        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"
            path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})

        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=3, youtube_search_content_type="shorts"), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert calls == [("shorts", 20), ("all", 20)]
    assert downloaded == ["short001", "short002", "short003"]
    assert snapshots[-1].downloaded == 3
    assert snapshots[-1].current_status == "任务完成"


def test_youtube_search_strict_shorts_never_uses_generic_fallback_and_reports_shortage(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    downloaded: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            calls.append(kwargs["content_type"])
            if kwargs["content_type"] == "shorts":
                return iter([_search_candidate("short001", "cars")])
            return iter([_search_candidate("short002", "cars"), _search_candidate("short003", "cars")])

        @staticmethod
        def metadata(_candidate, **_kwargs): return {"media_type": "short"}

        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})

        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=3, youtube_search_content_type="shorts", youtube_search_strict_shorts=True),
        tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert calls == ["shorts"]
    assert downloaded == ["short001"]
    assert snapshots[-1].downloaded == 1
    assert snapshots[-1].current_status == "采集结束（YouTube Shorts 精确搜索候选不足）"


def test_youtube_search_strict_shorts_with_zero_candidates_never_uses_generic_fallback(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            calls.append(kwargs["content_type"])
            return iter(())

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    logs, snapshots = [], []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=3, youtube_search_content_type="shorts", youtube_search_strict_shorts=True),
        tmp_path / "profile", on_log=logs.append, on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert calls == ["shorts"]
    assert any("内容类型=Shorts" in line and "严格 Shorts=开启" in line and "搜索源=Shorts 精确搜索 URL" in line for line in logs)
    assert snapshots[-1].current_status == "采集结束（YouTube Shorts 精确搜索候选不足）"


def test_youtube_browser_shorts_collector_keeps_ids_after_virtual_dom_changes(tmp_path: Path) -> None:
    class Locator:
        def __init__(self, page): self.page = page
        def evaluate_all(self, _script): return self.page.links

    class Page:
        def __init__(self): self.links = []
        def locator(self, _selector): return Locator(self)

    page = Page()
    collector = YouTubeShortsBrowserCollector(tmp_path / "profile", lambda _message: None)
    state = _ShortsSearchState(page=page, source_url="https://www.youtube.com/results?search_query=cars")
    page.links = [
        {"href": "https://www.youtube.com/shorts/short001", "title": "first"},
        {"href": "https://www.youtube.com/shorts/short002", "title": "second"},
    ]
    assert collector._collect_visible(state) == 2
    # Simulate YouTube virtualizing the first cards out of the DOM before new
    # cards appear. State-level video-id dedupe must retain old discoveries.
    page.links = [
        {"href": "https://www.youtube.com/shorts/short002", "title": "second"},
        {"href": "https://www.youtube.com/shorts/short003", "title": "third"},
    ]
    assert collector._collect_visible(state) == 1
    assert list(state.candidates) == ["short001", "short002", "short003"]


def test_youtube_browser_strict_shorts_does_not_call_generic_ytdlp_search(tmp_path: Path, monkeypatch) -> None:
    adapter_calls, browser_calls = [], []

    class BrowserCollector:
        def __init__(self, *_args): pass
        def open(self): pass
        def close(self): pass
        def search(self, keyword, limit, **_kwargs):
            browser_calls.append((keyword, limit))
            return [_search_candidate("short001", keyword)]

    class Adapter:
        def search(self, *_args, **_kwargs):
            adapter_calls.append("search")
            return iter(())
        @staticmethod
        def metadata(_candidate, **_kwargs): return {"media_type": "short"}
        @staticmethod
        def download(candidate, output_dir, index, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeShortsBrowserCollector", BrowserCollector)
    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=3, youtube_search_content_type="shorts", youtube_search_source="browser_shorts", youtube_search_strict_shorts=True),
        tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()
    assert browser_calls == [("cars", 20)]
    assert adapter_calls == []
    assert snapshots[-1].current_status == "采集结束（YouTube Shorts 精确搜索候选不足）"


def test_youtube_browser_shorts_candidates_use_existing_download_pipeline_to_target(tmp_path: Path, monkeypatch) -> None:
    browser_calls, downloaded = [], []

    class BrowserCollector:
        def __init__(self, *_args): pass
        def open(self): pass
        def close(self): pass
        def search(self, keyword, limit, **_kwargs):
            browser_calls.append((keyword, limit))
            return [_search_candidate(f"short00{i}", keyword) for i in range(1, 4)]

    class Adapter:
        @staticmethod
        def metadata(_candidate, **_kwargs): return {"media_type": "short"}
        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeShortsBrowserCollector", BrowserCollector)
    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=3, youtube_search_content_type="shorts", youtube_search_source="browser_shorts"),
        tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()
    assert browser_calls == [("cars", 20)]
    assert downloaded == ["short001", "short002", "short003"]
    assert snapshots[-1].current_status == "任务完成"


def test_youtube_search_video_filtered_source_falls_back_to_generic_search(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    downloaded: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            calls.append(kwargs["content_type"])
            if kwargs["content_type"] == "video":
                return iter([_search_candidate("video001", "cars")])
            return iter([_search_candidate("short001", "cars"), _search_candidate("video002", "cars")])

        @staticmethod
        def metadata(candidate, **_kwargs):
            return {"media_type": "short" if candidate.post_id.startswith("short") else "video"}

        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})

        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    DownloadEngine(
        _search_config(tmp_path, ("cars",), youtube_search_content_type="video"), tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()

    assert calls == ["video", "all"]
    assert downloaded == ["video001", "video002"]


def test_youtube_search_all_does_not_use_filtered_fallback(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            calls.append(kwargs["content_type"])
            return iter([_search_candidate("item001", "cars")])

        def download(self, candidate, output_dir, index, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})

        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    DownloadEngine(_search_config(tmp_path, ("cars",), target=2), tmp_path / "profile", database=CollectorDatabase(tmp_path / "db.sqlite")).run()
    assert calls == ["all"]


def test_youtube_search_reports_exhausted_only_after_filtered_and_generic_sources(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            calls.append(kwargs["content_type"])
            return iter(())

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=2, youtube_search_content_type="shorts"), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()
    assert calls == ["shorts", "all"]
    assert snapshots[-1].current_status == "采集结束（YouTube 搜索结果已耗尽）"


def test_youtube_search_fallback_continues_after_filtered_candidate_download_failure(tmp_path: Path, monkeypatch) -> None:
    downloaded: list[str] = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            if kwargs["content_type"] == "shorts":
                return iter([_search_candidate("badshort", "cars")])
            return iter([_search_candidate("good001", "cars"), _search_candidate("good002", "cars")])

        @staticmethod
        def metadata(_candidate, **_kwargs): return {"media_type": "short"}

        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id)
            if candidate.post_id == "badshort":
                raise YouTubeError("unavailable")
            output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})

        @staticmethod
        def file_hash(path): return path.name

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(
        _search_config(tmp_path, ("cars",), target=2, youtube_search_content_type="shorts"), tmp_path / "profile",
        on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite"),
    ).run()
    assert downloaded == ["badshort", "good001", "good002"]
    assert snapshots[-1].downloaded == 2
    assert snapshots[-1].failed == 1


def test_youtube_search_stop_during_generic_fallback_prevents_more_searches(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    engine_ref = []

    class Adapter:
        def search(self, _keyword, _limit, **kwargs):
            calls.append(kwargs["content_type"])
            if kwargs["content_type"] == "all":
                engine_ref[0].stop()
            return iter(())

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    engine = DownloadEngine(
        _search_config(tmp_path, ("cars",), target=2, youtube_search_content_type="shorts"), tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "db.sqlite"),
    )
    engine_ref.append(engine)
    engine.run()
    assert calls == ["shorts", "all"]


def test_youtube_search_continues_after_failure_and_deduplicates_video_ids(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []
    pools = {
        "first": [_search_candidate("same001", "first"), _search_candidate("bad001", "first"), _search_candidate("good001", "first")],
        "second": [_search_candidate("same001", "second"), _search_candidate("good002", "second")],
    }

    class Adapter:
        def search(self, keyword, _limit, **_kwargs): return iter(pools[keyword])
        def download(self, candidate, output_dir, index, **_kwargs):
            downloads.append(candidate.post_id)
            if candidate.post_id == "bad001": raise YouTubeError("unavailable")
            output_dir.mkdir(parents=True, exist_ok=True); path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(candidate.post_id.encode())
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(path): return path.read_text()

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    DownloadEngine(_search_config(tmp_path, ("first", "second"), target=3), tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite")).run()

    assert downloads == ["same001", "bad001", "good002", "good001"]
    assert snapshots[-1].downloaded == 3
    assert snapshots[-1].failed == 1
    assert snapshots[-1].skipped >= 1


def test_youtube_search_filters_duration_and_upload_date_before_download(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y%m%d")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y%m%d")
    pools = {"query": [_search_candidate("long001", "query"), _search_candidate("old001", "query"), _search_candidate("good001", "query")]}
    downloaded: list[str] = []

    class Adapter:
        def search(self, keyword, _limit, **_kwargs): return iter(pools[keyword])
        def metadata(self, candidate, **_kwargs):
            return {
                "long001": {"duration": 400, "upload_date": recent},
                "old001": {"duration": 120, "upload_date": old},
                "good001": {"duration": 120, "upload_date": recent},
            }[candidate.post_id]
        def download(self, candidate, output_dir, index, **_kwargs):
            downloaded.append(candidate.post_id); output_dir.mkdir(parents=True, exist_ok=True)
            path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(_path): return "good"

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    snapshots = []
    config = _search_config(tmp_path, ("query",), target=1, youtube_search_max_duration=300, youtube_search_date_range="30d")
    DownloadEngine(config, tmp_path / "profile", on_stats=snapshots.append, database=CollectorDatabase(tmp_path / "db.sqlite")).run()

    assert downloaded == ["good001"]
    assert snapshots[-1].skipped == 2


def test_youtube_search_keyword_failure_continues_and_stop_prevents_next_search(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class Adapter:
        def search(self, keyword, _limit, **_kwargs):
            calls.append(keyword)
            if keyword == "broken": raise YouTubeError("search failed")
            return iter([_search_candidate("good001", keyword)])
        def download(self, candidate, output_dir, index, **_kwargs):
            output_dir.mkdir(parents=True, exist_ok=True); path = output_dir / f"{index:04d}_{candidate.post_id}.mp4"; path.write_bytes(b"video")
            return YouTubeDownload(path, {"id": candidate.post_id})
        @staticmethod
        def file_hash(_path): return "good"

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    engine = DownloadEngine(_search_config(tmp_path, ("broken", "working"), target=1), tmp_path / "profile", database=CollectorDatabase(tmp_path / "db.sqlite"))
    pauses = []
    monkeypatch.setattr(engine, "_wait_if_paused", lambda: pauses.append(True))
    engine.run()
    assert calls == ["broken", "working"]
    assert pauses


def test_youtube_search_stop_prevents_following_keyword_search(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    engine_ref = []

    class Adapter:
        def search(self, keyword, _limit, **_kwargs):
            calls.append(keyword)
            engine_ref[0].stop()
            return iter(())

    monkeypatch.setattr("src.downloader.engine.YouTubeAdapter", Adapter)
    engine = DownloadEngine(_search_config(tmp_path, ("first", "second"), target=1), tmp_path / "profile", database=CollectorDatabase(tmp_path / "db.sqlite"))
    engine_ref.append(engine)
    engine.run()

    assert calls == ["first"]
    assert engine.stats.current_status == "已停止"
