from pathlib import Path
from unittest.mock import Mock
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from src.browser import ImageCandidate
from src.downloader import engine as engine_module
from src.downloader.dash import DASHDownloadCancelled
from src.downloader.engine import DownloadEngine
from src.downloader.hls import HLSDownloadError
from src.adapters.instagram_gallerydl import GalleryDlError
from src.models import TaskConfig
from src.storage import CollectorDatabase, TaskStore


def _image_bytes(size: tuple[int, int], color: str) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_download_count_means_successful_images_not_discovered_candidates(tmp_path: Path, monkeypatch) -> None:
    payloads = {
        "small": _image_bytes((1, 1), "red"),
        "large-1": _image_bytes((2, 2), "green"),
        "large-2": _image_bytes((3, 3), "blue"),
    }

    class Response:
        def __init__(self, content: bytes) -> None:
            self.content = content

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def get(self, url: str, **kwargs) -> Response:
            return Response(payloads[url])

    class Collector:
        def __init__(self, profile_dir, log) -> None:
            return None

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def collect(self, keyword, wanted, max_scrolls, should_stop, should_pause, on_found, on_candidate, should_finish):
            candidates = [
                ImageCandidate("small", "https://www.pinterest.com/pin/1", "https://www.pinterest.com/search/pins/?q=test"),
                ImageCandidate("large-1", "https://www.pinterest.com/pin/2", "https://www.pinterest.com/search/pins/?q=test"),
                ImageCandidate("large-2", "https://www.pinterest.com/pin/3", "https://www.pinterest.com/search/pins/?q=test"),
            ]
            found = []
            for candidate in candidates:
                if should_stop():
                    break
                found.append(candidate)
                on_candidate(candidate)
                on_found(len(found))
                if should_finish():
                    break
            return found

    monkeypatch.setattr(engine_module, "httpx", type("Httpx", (), {"Client": Client}))
    monkeypatch.setattr(engine_module, "PinterestCollector", Collector)
    config = TaskConfig("test", 2, tmp_path, min_width=2, min_height=2, interval_seconds=0)

    task_dir = DownloadEngine(config, tmp_path / "profile", database=CollectorDatabase(tmp_path / "collector.db")).run()

    assert len(list((task_dir / "images").iterdir())) == 2
    assert (task_dir / "task.json").read_text(encoding="utf-8").find('"downloaded": 2') >= 0


def test_image_download_falls_back_when_highest_quality_url_fails(tmp_path: Path) -> None:
    config = TaskConfig("test", 1, tmp_path / "output", min_width=2, min_height=2, interval_seconds=0)
    engine = DownloadEngine(config, tmp_path / "profile", database=CollectorDatabase(tmp_path / "collector.db"))
    engine.task_dir = tmp_path / "task"
    engine.task_dir.mkdir()
    (engine.task_dir / "images").mkdir()
    store = TaskStore(engine.task_dir, config)
    payload = _image_bytes((4, 4), "purple")
    highest = "https://i.pinimg.com/originals/a/b/image.jpg"
    fallback = "https://i.pinimg.com/736x/a/b/image.jpg"
    calls: list[str] = []

    class Client:
        def get(self, url: str, **_kwargs):
            calls.append(url)
            if url == highest:
                request = httpx.Request("GET", url)
                response = httpx.Response(404, request=request)
                response.raise_for_status()
            return type("Response", (), {"content": payload, "raise_for_status": lambda self: None})()

    candidate = ImageCandidate(
        highest,
        "https://www.pinterest.com/pin/123/",
        media_url=highest,
        fallback_urls=(fallback,),
    )

    engine._download_one(Client(), candidate, store, set())

    assert calls == [highest, highest, highest, fallback]
    assert engine.stats.downloaded == 1
    assert (engine.task_dir / "images" / "0001.png").exists()
    metadata = (engine.task_dir / "metadata.csv").read_text(encoding="utf-8-sig")
    assert fallback in metadata


def test_current_task_identity_duplicate_is_always_skipped(tmp_path: Path, monkeypatch) -> None:
    config = TaskConfig("test", 2, tmp_path / "output", interval_seconds=0)
    database = Mock()
    database.find_duplicate.return_value = None
    engine = DownloadEngine(config, tmp_path / "profile", database=database)
    engine.task_dir = tmp_path / "task"
    engine.task_dir.mkdir()
    store = TaskStore(engine.task_dir, config)
    downloaded: list[str] = []

    def fake_download(_client, candidate, _store, _known) -> None:
        downloaded.append(candidate.media_url)
        engine.stats.downloaded += 1

    monkeypatch.setattr(engine, "_download_one", fake_download)
    first = ImageCandidate("", "", media_url="https://cdn.test/a.jpg", media_identity="pin:1")
    second = ImageCandidate("", "", media_url="https://cdn.test/other-signed.jpg", media_identity="pin:1")

    engine._process_candidate(object(), first, store, set())
    engine._process_candidate(object(), second, store, set())

    assert downloaded == ["https://cdn.test/a.jpg"]
    assert engine.stats.duplicate_count == 1
    assert engine.stats.skipped == 0
    database.find_duplicate.assert_not_called()


@pytest.mark.parametrize("skip_history", [False, True])
def test_history_identity_switch_controls_author_candidate(tmp_path: Path, monkeypatch, skip_history: bool) -> None:
    config = TaskConfig(
        "",
        1,
        tmp_path / "output",
        platform="instagram",
        collection_mode="author",
        source_identifier="alice",
        media_type="mixed",
        interval_seconds=0,
        skip_history_duplicates=skip_history,
    )
    database = Mock()
    database.find_duplicate.return_value = {"id": 1} if skip_history else None
    engine = DownloadEngine(config, tmp_path / "profile", database=database)
    engine.task_dir = tmp_path / "task"
    engine.task_dir.mkdir()
    store = TaskStore(engine.task_dir, config)
    downloaded: list[str] = []

    def fake_download(_client, candidate, _store, _known) -> None:
        downloaded.append(candidate.media_identity)
        engine.stats.downloaded += 1

    monkeypatch.setattr(engine, "_download_one", fake_download)
    candidate = ImageCandidate("", "", platform="instagram", media_url="media", media_identity="POST:0")
    engine._process_candidate(object(), candidate, store, set())

    assert downloaded == ([] if skip_history else ["POST:0"])
    assert engine.stats.duplicate_count == (1 if skip_history else 0)
    assert engine.stats.skipped == 0
    if skip_history:
        database.find_duplicate.assert_called_once_with(candidate, "alice", require_local_file=False)
    else:
        database.find_duplicate.assert_not_called()


def test_historical_sha256_check_is_opt_in_and_current_hash_is_always_checked(tmp_path: Path) -> None:
    payload = _image_bytes((4, 4), "orange")

    class Client:
        def get(self, _url, **_kwargs):
            return type("Response", (), {"content": payload, "raise_for_status": lambda self: None})()

    candidate = ImageCandidate("image", "https://example.test/post/1", media_url="image")
    for skip_history, expected_downloads, expected_hash_calls in ((False, 1, 0), (True, 0, 1)):
        config = TaskConfig("test", 1, tmp_path / f"output-{skip_history}", min_width=2, min_height=2, interval_seconds=0, skip_history_duplicates=skip_history)
        database = Mock()
        database.find_hash_duplicate.return_value = {"id": 1}
        engine = DownloadEngine(config, tmp_path / "profile", database=database)
        engine.task_dir = tmp_path / f"task-{skip_history}"
        engine.task_dir.mkdir()
        (engine.task_dir / "images").mkdir()
        store = TaskStore(engine.task_dir, config)
        engine._download_one(Client(), candidate, store, set())
        assert engine.stats.downloaded == expected_downloads
        assert database.find_hash_duplicate.call_count == expected_hash_calls
        if skip_history:
            assert database.find_hash_duplicate.call_args.args == (hashlib.sha256(payload).hexdigest(),)
            assert database.find_hash_duplicate.call_args.kwargs == {"require_local_file": False}


def test_current_task_sha256_duplicate_is_skipped_when_history_dedup_is_off(tmp_path: Path) -> None:
    payload = _image_bytes((4, 4), "orange")

    class Client:
        def get(self, _url, **_kwargs):
            return type("Response", (), {"content": payload, "raise_for_status": lambda self: None})()

    config = TaskConfig("test", 2, tmp_path / "output", min_width=2, min_height=2, interval_seconds=0)
    database = Mock()
    engine = DownloadEngine(config, tmp_path / "profile", database=database)
    engine.task_dir = tmp_path / "task-current-hash"
    engine.task_dir.mkdir()
    (engine.task_dir / "images").mkdir()
    store = TaskStore(engine.task_dir, config)
    first = ImageCandidate("first", "https://example.test/post/1", media_url="first", media_identity="first")
    second = ImageCandidate("second", "https://example.test/post/2", media_url="second", media_identity="second")

    engine._download_one(Client(), first, store, set())
    engine._download_one(Client(), second, store, set())

    assert engine.stats.downloaded == 1
    assert engine.stats.duplicate_count == 1
    database.find_hash_duplicate.assert_not_called()


def test_engine_reports_task_directory_as_soon_as_it_is_created(tmp_path: Path, monkeypatch) -> None:
    class Collector:
        def __init__(self, profile_dir, log) -> None:
            return None

        def open(self) -> None:
            return None

        def close(self) -> None:
            return None

        def collect(self, keyword, wanted, max_scrolls, should_stop, should_pause, on_found, on_candidate, should_finish):
            return []

    monkeypatch.setattr(engine_module, "PinterestCollector", Collector)
    config = TaskConfig("test", 1, tmp_path, interval_seconds=0)
    started: list[Path] = []

    try:
        DownloadEngine(config, tmp_path / "profile", on_task_dir=started.append, database=CollectorDatabase(tmp_path / "collector.db")).run()
    except RuntimeError:
        pass

    assert len(started) == 1
    assert started[0].name.startswith("test_")
    assert started[0].parent == tmp_path
    assert config.output_root == tmp_path


def test_engine_reuses_managed_browser_and_records_current_page_url(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, bool] = {}

    class Page:
        url = "https://www.pinterest.com/pin/123/"

    class Collector:
        page = Page()
        opened = False
        connected = False

        def __init__(self, profile_dir, log) -> None:
            return None

        def open(self) -> None:
            observed["opened"] = True

        def connect_over_cdp(self, endpoint: str) -> None:
            observed["connected"] = endpoint == "http://127.0.0.1:9222"

        def prepare_current_page(self):
            return self.page

        def collect_current_page(self, *args, **kwargs):
            return []

        def close(self) -> None:
            return None

    class Manager:
        endpoint = "http://127.0.0.1:9222"

        def is_running(self) -> bool:
            return True

    monkeypatch.setattr(engine_module, "PinterestCollector", Collector)
    config = TaskConfig("", 1, tmp_path, collection_mode="current_page", interval_seconds=0)

    task_dir = DownloadEngine(config, tmp_path / "profile", database=CollectorDatabase(tmp_path / "collector.db"), browser_manager=Manager()).run()

    assert observed == {"connected": True}
    task_data = (task_dir / "task.json").read_text(encoding="utf-8")
    assert '"source_page_url": "https://www.pinterest.com/pin/123/"' in task_data


def test_engine_routes_instagram_to_shared_collector_and_normalizes_config(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    class Collector:
        def __init__(self, profile_dir, log) -> None:
            observed["profile_dir"] = profile_dir

        def open(self) -> None:
            observed["opened"] = True

        def connect_over_cdp(self, endpoint: str) -> None:
            observed["connected"] = endpoint == "http://127.0.0.1:9222"

        def close(self) -> None:
            observed["closed"] = True

        def collect_author(self, *args):
            observed["author_source"] = args[0]
            return []

    class Manager:
        endpoint = "http://127.0.0.1:9222"

        def is_login_running(self) -> bool:
            return False

        def is_running(self) -> bool:
            return True

    monkeypatch.setattr(engine_module, "InstagramCollector", Collector)
    config = TaskConfig(
        "",
        1,
        tmp_path,
        platform="instagram",
        collection_mode="author",
        source_identifier="@alice",
        media_type="mixed",
        interval_seconds=0,
    )

    task_dir = DownloadEngine(
        config,
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
        browser_manager=Manager(),
    ).run()

    assert observed["connected"] is True
    assert observed["closed"] is True
    assert observed["author_source"] == "alice"
    task_data = (task_dir / "task.json").read_text(encoding="utf-8")
    assert '"source_page_url": "https://www.instagram.com/alice/"' in task_data


def test_gallerydl_author_keeps_requesting_after_duplicate_and_failed_until_target(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_INSTAGRAM_GALLERYDL_POC", "1")
    seen: list[str] = []
    candidates = [
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="DUP", media_url="dup", media_identity="DUP:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="FAIL", media_url="fail", media_identity="FAIL:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="NEW1", media_url="new1", media_identity="NEW1:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="NEW2", media_url="new2", media_identity="NEW2:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="NEW3", media_url="new3", media_identity="NEW3:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="EXTRA", media_url="extra", media_identity="EXTRA:0"),
    ]

    class GalleryAdapter:
        def collect(self, _url, *, on_candidate, **_kwargs):
            delivered = 0
            for candidate in candidates:
                delivered += 1
                if not on_candidate(candidate):
                    break
            return delivered

    monkeypatch.setattr(engine_module, "InstagramGalleryDlAdapter", GalleryAdapter)
    config = TaskConfig(
        "",
        3,
        tmp_path / "output",
        platform="instagram",
        collection_mode="author",
        source_identifier="@alice",
        media_type="mixed",
        interval_seconds=0,
    )
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
    )

    def fake_process(_client, candidate, _store, _known):
        seen.append(candidate.shortcode)
        if candidate.shortcode == "DUP":
            engine.stats.duplicate_count += 1
        elif candidate.shortcode == "FAIL":
            engine.stats.failed += 1
        else:
            engine.stats.downloaded += 1

    monkeypatch.setattr(engine, "_process_candidate", fake_process)
    task_dir = engine.run()

    assert seen == ["DUP", "FAIL", "NEW1", "NEW2", "NEW3"]
    assert engine.stats.downloaded == 3
    assert engine.stats.duplicate_count == 1
    assert engine.stats.failed == 1
    assert '"downloaded": 3' in (task_dir / "task.json").read_text(encoding="utf-8")


def test_gallerydl_discovered_counts_duplicates_failures_and_successes_before_processing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_INSTAGRAM_GALLERYDL_POC", "1")
    candidates = [
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="CURRENT_DUP", media_url="current", media_identity="CURRENT_DUP:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="HISTORY_DUP", media_url="history", media_identity="HISTORY_DUP:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="FAILED", media_url="failed", media_identity="FAILED:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="SUCCESS1", media_url="success1", media_identity="SUCCESS1:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="SUCCESS2", media_url="success2", media_identity="SUCCESS2:0"),
        ImageCandidate("", "", platform="instagram", author_username="alice", shortcode="SUCCESS3", media_url="success3", media_identity="SUCCESS3:0"),
    ]

    class GalleryAdapter:
        def collect(self, _url, *, on_candidate, on_discovered, **_kwargs):
            delivered = 0
            for candidate in candidates:
                on_discovered(candidate)
                delivered += 1
                if not on_candidate(candidate):
                    break
            return delivered

    monkeypatch.setattr(engine_module, "InstagramGalleryDlAdapter", GalleryAdapter)
    logs: list[str] = []
    config = TaskConfig(
        "",
        3,
        tmp_path / "output",
        platform="instagram",
        collection_mode="author",
        source_identifier="@alice",
        media_type="mixed",
        interval_seconds=0,
    )
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        on_log=logs.append,
        database=CollectorDatabase(tmp_path / "collector.db"),
    )

    def fake_process(_client, candidate, _store, _known):
        if candidate.shortcode in {"CURRENT_DUP", "HISTORY_DUP"}:
            engine.stats.duplicate_count += 1
        elif candidate.shortcode == "FAILED":
            engine.stats.failed += 1
        else:
            engine.stats.downloaded += 1

    monkeypatch.setattr(engine, "_process_candidate", fake_process)
    engine.run()

    assert engine.stats.discovered == 6
    assert engine.stats.duplicate_count == 2
    assert engine.stats.failed == 1
    assert engine.stats.downloaded == 3
    assert "收集完成，发现 6 个候选资源。" in logs


def test_gallerydl_video_filter_counts_twenty_images_as_skipped(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_INSTAGRAM_GALLERYDL_POC", "1")
    candidates = [
        ImageCandidate("", "", platform="instagram", media_type="image", media_url=f"image-{index}", media_identity=f"IMAGE:{index}")
        for index in range(20)
    ]
    candidates.append(ImageCandidate("", "", platform="instagram", media_type="video", media_url="video", media_identity="VIDEO:0"))

    class GalleryAdapter:
        def collect(self, _url, *, on_candidate, on_discovered, on_skipped, **_kwargs):
            delivered = 0
            for candidate in candidates:
                on_discovered(candidate)
                if candidate.media_type != "video":
                    on_skipped(candidate)
                    continue
                delivered += 1
                if not on_candidate(candidate):
                    break
            return delivered

    monkeypatch.setattr(engine_module, "InstagramGalleryDlAdapter", GalleryAdapter)
    logs: list[str] = []
    config = TaskConfig(
        "",
        1,
        tmp_path / "output",
        platform="instagram",
        collection_mode="hashtag",
        source_identifier="reels",
        media_type="video",
        interval_seconds=0,
    )
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        on_log=logs.append,
        database=CollectorDatabase(tmp_path / "collector.db"),
    )
    monkeypatch.setattr(engine, "_process_candidate", lambda *_args: setattr(engine.stats, "downloaded", 1))

    engine.run()

    assert engine.stats.discovered == 21
    assert engine.stats.skipped == 20
    assert engine.stats.downloaded == 1
    assert engine.stats.failed == 0
    assert "收集完成，发现 21 个候选资源。" in logs


def test_gallerydl_author_exhausted_before_target_is_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_INSTAGRAM_GALLERYDL_POC", "1")

    class GalleryAdapter:
        def collect(self, _url, *, on_candidate, **_kwargs):
            on_candidate(ImageCandidate(
                "", "", platform="instagram", author_username="alice",
                shortcode="ONLY", media_url="only", media_identity="ONLY:0",
            ))
            return 1

    monkeypatch.setattr(engine_module, "InstagramGalleryDlAdapter", GalleryAdapter)
    config = TaskConfig(
        "",
        2,
        tmp_path / "output",
        platform="instagram",
        collection_mode="author",
        source_identifier="alice",
        media_type="image",
        interval_seconds=0,
    )
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
    )
    monkeypatch.setattr(engine, "_process_candidate", lambda *_args: None)

    with pytest.raises(GalleryDlError, match="source exhausted before target reached"):
        engine.run()

    history = engine.database.get_history(config.task_id)
    assert history["status"] == "failed"
    assert history["error_message"] == "source exhausted before target reached"


def test_gallerydl_hashtag_uses_tag_url_without_collection_chrome(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_INSTAGRAM_GALLERYDL_POC", "1")
    observed: dict[str, object] = {}

    class GalleryAdapter:
        def collect(self, url, *, source_hashtag, on_candidate, **_kwargs):
            observed["url"] = url
            observed["source_hashtag"] = source_hashtag
            on_candidate(ImageCandidate(
                "",
                "https://www.instagram.com/p/TAGPOST1/",
                platform="instagram",
                author_username="creator",
                media_url="tag-media",
                media_identity="TAGPOST1:0",
                hashtag="aiart",
                source_hashtag="aiart",
            ))
            return 1

    monkeypatch.setattr(engine_module, "InstagramGalleryDlAdapter", GalleryAdapter)
    config = TaskConfig(
        "",
        1,
        tmp_path / "output",
        platform="instagram",
        collection_mode="hashtag",
        source_identifier="#AIART",
        media_type="image",
        interval_seconds=0,
    )
    engine = DownloadEngine(config, tmp_path / "profile", database=CollectorDatabase(tmp_path / "collector.db"))
    monkeypatch.setattr(engine, "_process_candidate", lambda *_args: setattr(engine.stats, "downloaded", 1))

    task_dir = engine.run()

    assert observed == {
        "url": "https://www.instagram.com/explore/tags/aiart/",
        "source_hashtag": "aiart",
    }
    task_data = (task_dir / "task.json").read_text(encoding="utf-8")
    assert '"collection_mode": "hashtag"' in task_data
    assert '"hashtag": "aiart"' in task_data
    assert '"source_hashtag": "aiart"' in task_data


def test_gallerydl_hashtag_exhausted_before_target_is_failed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SMC_INSTAGRAM_GALLERYDL_POC", "1")

    class GalleryAdapter:
        def collect(self, _url, *, on_candidate, **_kwargs):
            on_candidate(ImageCandidate("", "", platform="instagram", media_url="only", media_identity="ONLY:0"))
            return 1

    monkeypatch.setattr(engine_module, "InstagramGalleryDlAdapter", GalleryAdapter)
    config = TaskConfig(
        "",
        2,
        tmp_path / "output",
        platform="instagram",
        collection_mode="hashtag",
        source_identifier="travel",
        media_type="image",
        interval_seconds=0,
    )
    engine = DownloadEngine(config, tmp_path / "profile", database=CollectorDatabase(tmp_path / "collector.db"))
    monkeypatch.setattr(engine, "_process_candidate", lambda *_args: setattr(engine.stats, "downloaded", 1))

    with pytest.raises(GalleryDlError, match="source exhausted before target reached"):
        engine.run()


def test_engine_rejects_instagram_without_manual_login_browser(tmp_path: Path) -> None:
    config = TaskConfig(
        "",
        1,
        tmp_path,
        platform="instagram",
        collection_mode="single_post",
        source_identifier="https://www.instagram.com/p/ABC123/",
        media_type="mixed",
        interval_seconds=0,
    )

    with pytest.raises(RuntimeError, match="Instagram 尚未登录"):
        DownloadEngine(config, tmp_path / "profile", database=CollectorDatabase(tmp_path / "collector.db")).run()


def test_engine_rejects_instagram_while_manual_login_browser_is_running(tmp_path: Path) -> None:
    class Manager:
        endpoint = None

        def is_login_running(self) -> bool:
            return True

        def is_running(self) -> bool:
            return False

    config = TaskConfig(
        "",
        1,
        tmp_path,
        platform="instagram",
        collection_mode="single_post",
        source_identifier="https://www.instagram.com/p/ABC123/",
        media_type="mixed",
        interval_seconds=0,
    )

    with pytest.raises(RuntimeError, match="登录浏览器仍在运行"):
        DownloadEngine(
            config,
            tmp_path / "profile",
            database=CollectorDatabase(tmp_path / "collector.db"),
            browser_manager=Manager(),
        ).run()


def test_video_download_error_only_fails_current_candidate(tmp_path: Path, monkeypatch) -> None:
    config = TaskConfig(
        "",
        2,
        tmp_path / "output",
        platform="x",
        source_identifier="example",
        collection_mode="home",
        media_type="video",
        interval_seconds=0,
    )
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
    )
    engine.task_dir = tmp_path / "task"
    engine.task_dir.mkdir()
    store = TaskStore(engine.task_dir, config)
    calls = 0

    def fake_download_video(_client, _candidate, _record, _store, _known) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise HLSDownloadError("timeout")
        engine.stats.downloaded += 1

    monkeypatch.setattr(engine, "_download_video", fake_download_video)
    candidates = [
        ImageCandidate("", "https://x.com/example/status/1", media_type="video", media_url="video-1"),
        ImageCandidate("", "https://x.com/example/status/2", media_type="video", media_url="video-2"),
    ]

    for candidate in candidates:
        engine._download_one(object(), candidate, store, set())

    assert calls == 2
    assert engine.stats.failed == 1
    assert engine.stats.downloaded == 1
    assert engine.stats.skipped == 0


def test_instagram_video_success_log_uses_instagram_stage_label(tmp_path: Path, monkeypatch) -> None:
    config = TaskConfig(
        "",
        1,
        tmp_path / "output",
        platform="instagram",
        collection_mode="single_post",
        source_identifier="https://www.instagram.com/reel/ABC123/",
        media_type="video",
        interval_seconds=0,
    )
    logs: list[str] = []
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        on_log=logs.append,
        database=CollectorDatabase(tmp_path / "collector.db"),
    )
    engine.task_dir = tmp_path / "task"
    engine.task_dir.mkdir()
    store = TaskStore(engine.task_dir, config)

    def fake_download_direct(_client, _url, output_path, _source, _referer):
        output_path.write_bytes(b"fixture-video")
        return SimpleNamespace(duration=1.0, width=720, height=1280, bitrate=1000)

    monkeypatch.setattr(engine, "_download_direct_video", fake_download_direct)
    candidate = ImageCandidate(
        "",
        "https://www.instagram.com/reel/ABC123/",
        platform="instagram",
        media_type="video",
        media_url="https://cdn.test/video.mp4",
    )

    engine._download_one(object(), candidate, store, set())

    assert "Instagram视频阶段=mp4_saved" in logs
    assert "Pinterest视频阶段=mp4_saved" not in logs


def test_dash_cancellation_does_not_record_a_failed_candidate(tmp_path: Path, monkeypatch) -> None:
    config = TaskConfig(
        "",
        1,
        tmp_path / "output",
        platform="instagram",
        source_identifier="https://www.instagram.com/p/ABC123/",
        collection_mode="single_post",
        media_type="video",
        interval_seconds=0,
    )
    engine = DownloadEngine(
        config,
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
    )
    engine.task_dir = tmp_path / "task"
    engine.task_dir.mkdir()
    store = TaskStore(engine.task_dir, config)

    def cancel_download(_client, _candidate, _record, _store, _known) -> None:
        raise DASHDownloadCancelled()

    monkeypatch.setattr(engine, "_download_video", cancel_download)
    candidate = ImageCandidate(
        "",
        "https://www.instagram.com/reel/ABC123/",
        platform="instagram",
        media_type="video",
        media_url="https://cdn.test/video.mp4",
    )

    engine._download_one(object(), candidate, store, set())

    assert engine.stats.failed == 0
    assert store.metadata_path.read_text(encoding="utf-8-sig").count("\n") == 1
    assert store.failed_path.read_text(encoding="utf-8-sig").count("\n") == 1


def test_direct_video_request_preserves_signed_query_but_removes_range_controls() -> None:
    url = "https://cdninstagram.com/clip.mp4?efg=signed%3D%3D&_nc_sig=opaque&bytestart=0&byteend=817"
    request_url = DownloadEngine._direct_video_request_url(url)
    assert request_url == "https://cdninstagram.com/clip.mp4?efg=signed%3D%3D&_nc_sig=opaque"
    assert "efg=signed%3D%3D" in request_url
    assert "_nc_sig=opaque" in request_url
    assert "bytestart" not in request_url
    assert "byteend" not in request_url
