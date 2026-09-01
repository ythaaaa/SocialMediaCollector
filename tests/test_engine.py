from pathlib import Path

from PIL import Image

from src.browser import ImageCandidate
from src.downloader import engine as engine_module
from src.downloader.engine import DownloadEngine
from src.downloader.hls import HLSDownloadError
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
