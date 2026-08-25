from pathlib import Path

from PIL import Image

from src.browser import ImageCandidate
from src.downloader import engine as engine_module
from src.downloader.engine import DownloadEngine
from src.models import TaskConfig


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

    task_dir = DownloadEngine(config, tmp_path / "profile").run()

    assert len(list((task_dir / "images").iterdir())) == 2
    assert (task_dir / "task.json").read_text(encoding="utf-8").find('"downloaded": 2') >= 0
