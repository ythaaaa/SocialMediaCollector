from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from src.adapters import gallerydl_worker
from src.adapters.instagram_gallerydl import (
    GalleryDlError,
    PinterestGalleryDlAdapter,
    candidate_from_gallerydl,
)
from src.browser import ImageCandidate
from src.models import TaskConfig
from src.utils.pinterest import (
    normalize_pinterest_board_url,
    normalize_pinterest_pin_url,
    normalize_pinterest_username,
    pinterest_pin_id,
    pinterest_search_url,
    pinterest_user_url,
)


def pinterest_post(**extra):
    post = {
        "id": "123456789",
        "description": "测试描述 😂 📷 한국어 日本語",
        "closeup_description": "closeup",
        "pinner": {"username": "creator"},
        "board": {"name": "AI art"},
        "images": {
            "orig": {"url": "https://i.pinimg.com/originals/aa/bb/cc.jpg", "width": 1600, "height": 1200},
            "736x": {"url": "https://i.pinimg.com/736x/aa/bb/cc.jpg", "width": 736, "height": 552},
        },
        **extra,
    }
    return post


def pinterest_image_event(**extra):
    return {
        "type": "candidate",
        "platform": "pinterest",
        "media_type": "image",
        "pin_id": "123456789",
        "post_id": "123456789",
        "media_index": 0,
        "media_identity": "123456789:0",
        "pin_url": "https://www.pinterest.com/pin/123456789/",
        "post_url": "https://www.pinterest.com/pin/123456789/",
        "source_url": "https://www.pinterest.com/search/pins/?q=AI+art",
        "username": "creator",
        "description": "测试描述 😂",
        "display_url": "https://i.pinimg.com/originals/aa/bb/cc.jpg",
        "media_url": "https://i.pinimg.com/originals/aa/bb/cc.jpg",
        "width": 1600,
        "height": 1200,
        **extra,
    }


def test_pinterest_search_query_is_trimmed_and_url_encoded():
    assert pinterest_search_url("  AI art / 测试  ") == (
        "https://www.pinterest.com/search/pins/?q=AI+art+%2F+%E6%B5%8B%E8%AF%95"
    )


@pytest.mark.parametrize(
    "value, expected_id",
    [
        ("https://www.pinterest.com/pin/123456789/", "123456789"),
        ("www.pinterest.com/pin/abc/", "abc"),
        ("https://pin.it/AbCdEf", ""),
    ],
)
def test_pinterest_pin_url_normalization_and_id(value: str, expected_id: str):
    normalized = normalize_pinterest_pin_url(value)
    assert normalized.startswith("https://")
    assert pinterest_pin_id(normalized) == expected_id


def test_pinterest_pin_validation_rejects_non_pin_urls():
    with pytest.raises(ValueError, match="Pinterest Pin"):
        normalize_pinterest_pin_url("https://www.pinterest.com/search/pins/?q=cat")


@pytest.mark.parametrize(
    "value",
    [
        "alice",
        "https://www.pinterest.com/alice/",
        "https://www.pinterest.com/alice/pins/",
        "https://www.pinterest.com/alice/_created/",
        "https://www.pinterest.com/alice/_saved/",
    ],
)
def test_pinterest_user_urls_normalize_to_all_pins_username(value: str):
    assert normalize_pinterest_username(value) == "alice"


def test_pinterest_user_and_board_urls_are_normalized_without_cross_mode_confusion():
    assert normalize_pinterest_username("@alice") == "alice"
    assert pinterest_user_url("alice") == "https://www.pinterest.com/alice/pins/"
    assert normalize_pinterest_board_url("https://www.pinterest.com/alice/ai-art/") == (
        "https://www.pinterest.com/alice/ai-art/"
    )
    for value in (
        "https://www.pinterest.com/pin/123/",
        "https://www.pinterest.com/search/pins/?q=cat",
        "https://www.pinterest.com/alice/pins/",
        "https://www.pinterest.com/alice/_created/",
    ):
        with pytest.raises(ValueError):
            normalize_pinterest_board_url(value)
    with pytest.raises(ValueError):
        normalize_pinterest_username("https://www.pinterest.com/alice/boards/")


@pytest.mark.parametrize(
    "value",
    [
        "https://www.pinterest.com/alice/ai-art/",
        "https://www.pinterest.com/pin/123/",
        "https://www.pinterest.com/search/pins/?q=cat",
        "https://example.com/alice/",
    ],
)
def test_pinterest_user_rejects_board_pin_search_and_non_pinterest_urls(value: str):
    with pytest.raises(ValueError):
        normalize_pinterest_username(value)


def test_pinterest_task_config_supports_search_and_single_pin():
    search = TaskConfig("AI art", 3, Path("out"), platform="pinterest", collection_mode="search")
    pin = TaskConfig(
        "",
        1,
        Path("out"),
        platform="pinterest",
        collection_mode="single_post",
        source_identifier="https://www.pinterest.com/pin/123456789/",
        media_type="mixed",
    )
    search.validate()
    pin.validate()


def test_pinterest_task_config_supports_user_board_and_round_trip_fields():
    user = TaskConfig(
        "",
        3,
        Path("out"),
        platform="pinterest",
        collection_mode="user",
        source_identifier="https://www.pinterest.com/alice/",
        username="https://www.pinterest.com/alice/",
        source_type="user",
    )
    board = TaskConfig(
        "",
        3,
        Path("out"),
        platform="pinterest",
        collection_mode="board",
        source_identifier="https://www.pinterest.com/alice/ai-art/",
        board_url="https://www.pinterest.com/alice/ai-art/",
        source_type="board",
    )
    user.validate()
    board.validate()
    assert user.canonical_source_value == "alice"
    assert board.canonical_source_value.endswith("/alice/ai-art/")
    loaded = TaskConfig.from_dict({
        "platform": "pinterest",
        "collection_mode": "board",
        "source_identifier": "https://www.pinterest.com/alice/ai-art/",
        "board_url": "https://www.pinterest.com/alice/ai-art/",
        "target_count": 1,
        "output_root": "out",
    })
    assert loaded.canonical_source_value.endswith("/alice/ai-art/")


def test_pinterest_registry_keeps_home_and_current_page_modes():
    from src.platforms.registry import get_platform_spec

    assert get_platform_spec("pinterest").collection_modes == (
        "search", "single_post", "user", "board", "home", "current_page"
    )


def test_pinterest_image_mapping_preserves_unicode_identity_and_metadata():
    event = gallerydl_worker._candidate_payload(
        pinterest_post(),
        {"num": 1, "extension": "jpg", "width": 1600, "height": 1200},
        platform="pinterest",
        item_url="https://i.pinimg.com/originals/aa/bb/cc.jpg",
        source_url="https://www.pinterest.com/search/pins/?q=AI+art",
    )
    assert event is not None
    candidate = candidate_from_gallerydl(event)
    assert isinstance(candidate, ImageCandidate)
    assert candidate.platform == "pinterest"
    assert candidate.media_identity == "123456789:0"
    assert candidate.author_username == "creator"
    assert candidate.caption == "测试描述 😂 📷 한국어 日本語"
    assert event["metadata"]["board"] == "AI art"
    assert event["metadata"]["source_url"].endswith("AI+art")


def test_pinterest_user_and_board_candidate_metadata_preserves_source_scope():
    user_event = gallerydl_worker._candidate_payload(
        pinterest_post(),
        {"num": 1, "extension": "jpg"},
        platform="pinterest",
        item_url="https://i.pinimg.com/originals/aa/bb/cc.jpg",
        source_url="https://www.pinterest.com/alice/pins/",
        source_type="user",
        source_username="alice",
    )
    board_event = gallerydl_worker._candidate_payload(
        pinterest_post(board={"name": "AI art", "owner": {"username": "alice"}}),
        {"num": 1, "extension": "jpg"},
        platform="pinterest",
        item_url="https://i.pinimg.com/originals/aa/bb/cc.jpg",
        source_url="https://www.pinterest.com/alice/ai-art/",
        source_type="board",
        source_board_url="https://www.pinterest.com/alice/ai-art/",
    )
    assert user_event is not None and board_event is not None
    assert user_event["metadata"]["source_type"] == "user"
    assert user_event["metadata"]["source_username"] == "alice"
    assert board_event["metadata"]["source_type"] == "board"
    assert board_event["metadata"]["source_board"] == "AI art"
    assert board_event["metadata"]["source_owner"] == "alice"
    assert candidate_from_gallerydl(board_event).source_board_url.endswith("/alice/ai-art/")


def test_pinterest_carousel_uses_zero_based_media_identity():
    post = pinterest_post(id="pin-carousel")
    items = [
        (2, "", post),
        (3, "https://i.pinimg.com/originals/a.jpg", {"num": 1, "extension": "jpg"}),
        (3, "https://i.pinimg.com/originals/b.jpg", {"num": 2, "extension": "jpg"}),
        (3, "https://i.pinimg.com/originals/c.jpg", {"num": 3, "extension": "jpg"}),
    ]
    events = list(gallerydl_worker._matching_items(iter(items), "mixed", platform="pinterest", source_url="pin"))
    assert [event["media_identity"] for event in events] == ["pin-carousel:0", "pin-carousel:1", "pin-carousel:2"]


def test_pinterest_originals_has_variant_fallback():
    post = pinterest_post()
    event = gallerydl_worker._candidate_payload(
        post,
        {"num": 1, "extension": "jpg"},
        platform="pinterest",
        item_url="https://i.pinimg.com/originals/aa/bb/cc.jpg",
    )
    assert event is not None
    assert "https://i.pinimg.com/736x/aa/bb/cc.jpg" in event["fallback_urls"]


def test_pinterest_hls_mapping_uses_existing_hls_downloader_source():
    event = gallerydl_worker._candidate_payload(
        pinterest_post(videos={"V_HLSV4": {"url": "https://cdn.example/pin.m3u8"}}),
        {"num": 1, "extension": "m3u8", "_ytdl_manifest": "hls", "width": 1280, "height": 720},
        platform="pinterest",
        item_url="ytdl:https://cdn.example/pin.m3u8",
    )
    assert event is not None
    candidate = candidate_from_gallerydl(event)
    assert candidate.media_type == "video"
    assert candidate.video_sources[0].kind == "hls"
    assert candidate.video_sources[0].url.endswith("pin.m3u8")


def test_pinterest_direct_mp4_mapping_uses_existing_direct_downloader():
    event = gallerydl_worker._candidate_payload(
        pinterest_post(videos={"V_720P": {"url": "https://cdn.example/pin.mp4"}}),
        {"num": 1, "extension": "mp4", "width": 1280, "height": 720},
        platform="pinterest",
        item_url="https://cdn.example/pin.mp4",
    )
    assert event is not None
    candidate = candidate_from_gallerydl(event)
    assert candidate.video_sources[0].kind == "mp4"
    assert candidate.video_sources[0].url.endswith("pin.mp4")


def test_pinterest_audio_and_text_blocks_are_not_candidates():
    post = pinterest_post()
    assert gallerydl_worker._candidate_payload(
        post, {"num": 1, "extension": "m4a"}, platform="pinterest", item_url="https://cdn.example/audio.m4a"
    ) is None
    assert gallerydl_worker._candidate_payload(
        post, {"num": 1, "extension": "txt"}, platform="pinterest", item_url="https://cdn.example/text.txt"
    ) is None


def test_pinterest_matching_items_filters_requested_media_type_but_keeps_pagination_order():
    post = pinterest_post()
    items = [
        (2, "", post),
        (3, "https://cdn.example/image.jpg", {"num": 1, "extension": "jpg"}),
        (3, "https://cdn.example/video.mp4", {"num": 2, "extension": "mp4"}),
    ]
    images = list(gallerydl_worker._matching_items(iter(items), "image", platform="pinterest"))
    assert len(images) == 1
    assert images[0]["media_type"] == "image"


class FakePinterestClient:
    is_packaged_helper = True

    def __init__(self, events):
        self.events = list(events)
        self.commands: list[str] = []
        self.stopped = False

    def start(self):
        self.commands.append("start")

    def send(self, command):
        self.commands.append(command)

    def read_event(self, stop_event):
        if stop_event.is_set():
            raise RuntimeError("read after stop")
        return self.events.pop(0)

    def stop(self):
        self.stopped = True


def test_pinterest_adapter_streams_packaged_candidates_with_backpressure():
    fake = FakePinterestClient([
        {"type": "ready", "cookie_loaded": False, "gallery_dl_version": "1.32.14"},
        pinterest_image_event(),
    ])
    logs: list[str] = []
    seen: list[ImageCandidate] = []
    adapter = PinterestGalleryDlAdapter(
        profile_path=Path("profile"),
        client_factory=lambda *args, **kwargs: fake,
    )
    assert adapter.collect(
        "https://www.pinterest.com/pin/123456789/",
        on_candidate=lambda candidate: seen.append(candidate) or False,
        on_log=logs.append,
    ) == 1
    assert fake.commands == ["start", "next"]
    assert fake.stopped
    assert "Pinterest engine=gallery-dl 1.32.14; helper=packaged; cookie loaded=no" in logs
    assert seen[0].media_identity == "123456789:0"


def test_pinterest_adapter_pause_resume_does_not_advance_generator_while_paused():
    fake = FakePinterestClient([
        {"type": "ready", "cookie_loaded": False},
        pinterest_image_event(),
        pinterest_image_event(media_identity="123456789:1", media_index=1),
        {"type": "exhausted"},
    ])
    pause_event = threading.Event()
    pause_event.set()
    adapter = PinterestGalleryDlAdapter(
        profile_path=Path("profile"),
        cookies={"_pinterest_sess": "test-session"},
        client_factory=lambda *a, **k: fake,
    )

    def callback(_candidate):
        pause_event.clear()
        threading.Timer(0.1, pause_event.set).start()
        return True

    assert adapter.collect(
        "https://www.pinterest.com/search/pins/?q=AI+art",
        on_candidate=callback,
        pause_event=pause_event,
    ) == 2
    assert fake.commands.index("pause") < fake.commands.index("resume")


def test_pinterest_adapter_error_finishes_and_cleans_helper():
    fake = FakePinterestClient([
        {"type": "ready", "cookie_loaded": False},
        {"type": "error", "message": "gallery-dl extraction failed"},
    ])
    adapter = PinterestGalleryDlAdapter(profile_path=Path("profile"), client_factory=lambda *a, **k: fake)
    with pytest.raises(GalleryDlError, match="extraction failed"):
        adapter.collect("https://www.pinterest.com/pin/123456789/", on_candidate=lambda _candidate: True)
    assert fake.stopped


def test_pinterest_worker_search_and_pin_extractors_are_selected_lazily(monkeypatch, tmp_path: Path):
    captured: list[dict] = []

    class Instance:
        category = "pinterest"
        subcategory = "search"
        cookies_names = None
        cookies_domain = ""

        def initialize(self):
            captured.append({"initialized": True})

        def items(self):
            captured.append({"items": True})
            yield (2, "", pinterest_post())
            yield (3, "https://i.pinimg.com/originals/aa/bb/cc.jpg", {"num": 1, "extension": "jpg"})

    instance = Instance()
    fake_extractor = SimpleNamespace(find=lambda url: instance)
    fake_config = SimpleNamespace(set=lambda *args, **kwargs: None)
    fake_gallery = ModuleType("gallery_dl")
    fake_gallery.__version__ = "1.32.14"
    fake_gallery.config = fake_config
    fake_gallery.extractor = fake_extractor
    monkeypatch.setitem(sys.modules, "gallery_dl", fake_gallery)
    monkeypatch.setitem(sys.modules, "gallery_dl.config", fake_config)
    monkeypatch.setitem(sys.modules, "gallery_dl.extractor", fake_extractor)
    monkeypatch.setattr(gallerydl_worker.sys, "stdin", io.StringIO('{"type":"next"}\n{"type":"stop"}\n'))
    output: list[dict] = []
    monkeypatch.setattr(gallerydl_worker, "_write", output.append)

    assert gallerydl_worker.run({
        "platform": "pinterest",
        "url": "https://www.pinterest.com/search/pins/?q=AI+art",
        "profile_path": str(tmp_path / "unused"),
        "cookies": {"_pinterest_sess": "test-session"},
        "media_type": "mixed",
    }) == 0
    assert output[0]["type"] == "ready"
    assert output[0]["gallery_dl_version"] == "1.32.14"
    assert output[1]["platform"] == "pinterest"
    assert output[1]["media_identity"] == "123456789:0"
    assert captured == [{"initialized": True}, {"items": True}]


@pytest.mark.parametrize(
    ("url", "subcategory", "source_type"),
    [
        ("https://www.pinterest.com/alice/pins/", "allpins", "user"),
        ("https://www.pinterest.com/alice/ai-art/", "board", "board"),
    ],
)
def test_pinterest_worker_selects_user_or_board_without_queueing_sections(
    monkeypatch, tmp_path: Path, url: str, subcategory: str, source_type: str,
):
    captured: list[dict] = []
    settings: list[tuple] = []

    class Instance:
        category = "pinterest"
        cookies_names = None
        cookies_domain = ""

        def __init__(self):
            self.subcategory = subcategory

        def initialize(self):
            captured.append({"initialized": True})

        def items(self):
            captured.append({"items": True})
            yield (2, "", pinterest_post(board={"name": "AI art", "owner": {"username": "alice"}}))
            yield (3, "https://i.pinimg.com/originals/aa/bb/cc.jpg", {"num": 1, "extension": "jpg"})

    instance = Instance()
    fake_extractor = SimpleNamespace(find=lambda _url: instance)
    fake_config = SimpleNamespace(set=lambda *args, **kwargs: settings.append(args))
    fake_gallery = ModuleType("gallery_dl")
    fake_gallery.__version__ = "1.32.14"
    fake_gallery.config = fake_config
    fake_gallery.extractor = fake_extractor
    monkeypatch.setitem(sys.modules, "gallery_dl", fake_gallery)
    monkeypatch.setitem(sys.modules, "gallery_dl.config", fake_config)
    monkeypatch.setitem(sys.modules, "gallery_dl.extractor", fake_extractor)
    monkeypatch.setattr(gallerydl_worker.sys, "stdin", io.StringIO('{"type":"next"}\n{"type":"stop"}\n'))
    output: list[dict] = []
    monkeypatch.setattr(gallerydl_worker, "_write", output.append)

    assert gallerydl_worker.run({
        "platform": "pinterest",
        "url": url,
        "profile_path": str(tmp_path / "unused"),
        "cookies": {"_pinterest_sess": "test-session"},
        "media_type": "mixed",
        "source_type": source_type,
        "source_username": "alice",
        "source_board_url": "https://www.pinterest.com/alice/ai-art/",
    }) == 0
    assert output[0]["type"] == "ready"
    assert output[1]["source_type"] == source_type
    assert output[1]["source_username"] == "alice"
    assert any(item == (("extractor", "pinterest"), "sections", False) for item in settings)
    assert captured == [{"initialized": True}, {"items": True}]


def test_pinterest_worker_rejects_instagram_only_url_without_loading_browser():
    output: list[dict] = []
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(gallerydl_worker, "_write", output.append)
        assert gallerydl_worker.run({
            "platform": "pinterest",
            "url": "https://www.instagram.com/p/ABC/",
            "profile_path": "missing",
            "cookies": {},
        }) == 2
    finally:
        monkeypatch.undo()
    assert "Pinterest Search, Single Pin, User, or Board" in output[0]["message"]
