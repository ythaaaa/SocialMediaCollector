from __future__ import annotations

import io
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from src.adapters import gallerydl_worker
from src.adapters.instagram_gallerydl import XGalleryDlAdapter, candidate_from_gallerydl
from src.adapters.x_cookie_bridge import XAuthRequired
from src.utils.x import normalize_x_author, normalize_x_search_query, x_author_media_url, x_search_url


def x_event(**extra):
    return {
        "type": "candidate", "platform": "x", "media_type": "image",
        "tweet_id": "123", "post_id": "123", "media_index": 0,
        "media_identity": "123:0", "tweet_url": "https://x.com/alice/status/123",
        "post_url": "https://x.com/alice/status/123", "username": "alice",
        "content": "Prompt:\n测试 😂\n如下", "description": "Prompt:\n测试 😂\n如下",
        "display_url": "https://pbs.twimg.com/media/abc?format=jpg&name=orig",
        "media_url": "https://pbs.twimg.com/media/abc?format=jpg&name=orig",
        "width": 1600, "height": 1200, **extra,
    }


@pytest.mark.parametrize("value", ["alice", "@alice", "https://x.com/alice", "https://twitter.com/alice/", "https://x.com/alice/media"])
def test_x_author_inputs_normalize_to_media(value):
    assert normalize_x_author(value)[0] == "alice"
    assert x_author_media_url(value) == ("alice", "https://x.com/alice/media")


@pytest.mark.parametrize("media_type,operator", [("image", "filter:images"), ("video", "filter:native_video"), ("mixed", "filter:media")])
def test_x_search_adds_exact_media_operator_and_encodes_url(media_type, operator):
    query, url = x_search_url("  AI   art  ", media_type)
    assert query == f"AI art {operator}"
    assert f"filter%3A{operator.split(':')[1]}" in url


@pytest.mark.parametrize("operator", ["filter:images", "filter:native_video", "filter:media"])
def test_x_search_does_not_duplicate_existing_operator(operator):
    query = normalize_x_search_query(f"AI art {operator}", "mixed")
    assert query.count("filter:") == 1


def test_x_worker_maps_orig_image_metadata_identity_and_prompt_text():
    post = {"tweet_id": "123", "content": "Prompt:\ncinematic city", "author": {"id": "9", "name": "alice", "nick": "Alice Display"}, "date": "2026-01-01"}
    event = gallerydl_worker._candidate_payload(
        post, {**post, "num": 2, "type": "photo", "width": 2048, "height": 1365, "description": "alt"},
        platform="x", item_url="https://pbs.twimg.com/media/abc?format=jpg&name=orig",
        source_url="https://x.com/alice/media", source_type="author", source_username="alice",
    )
    candidate = candidate_from_gallerydl(event)
    assert candidate.platform == "x" and candidate.media_type == "image"
    assert candidate.media_identity == "123:1" and "name=orig" in candidate.image_url
    assert candidate.prompt_detected is True and candidate.post_text.startswith("Prompt:")
    assert event["metadata"]["user_id"] == "9" and event["metadata"]["alt_text"] == "alt"


@pytest.mark.parametrize(
    ("target", "handle", "display_name", "expected"),
    [
        ("jackzhang123vip", "jackzhang123vip", "Jack Zhang", True),
        ("JackZhang123VIP", "jackzhang123vip", "Jack Zhang", True),
        ("@jackzhang123vip", "@jackzhang123vip", "Jack Zhang", True),
        ("jackzhang123vip", "jackzhang123vip", "jackzhang123vip", True),
        ("jackzhang123vip", "other_handle", "Jack Zhang", False),
    ],
)
def test_x_canonical_owner_uses_handle_not_display_name(target, handle, display_name, expected):
    post = {
        "tweet_id": "99",
        "author": {"name": handle, "nick": display_name, "id": 7},
        # These fields simulate an unrelated quoted/retweet source. They
        # must never override the current Tweet's author.
        "user": {"name": "quoted_user", "nick": "Quoted Display", "id": 8},
    }
    event = gallerydl_worker._candidate_payload(
        post, {"num": 1, "type": "photo"}, platform="x",
        item_url="https://pbs.twimg.com/media/owner.jpg?format=jpg&name=orig",
        source_url="https://x.com/jackzhang123vip/media",
        source_type="author", source_username=target,
    )
    candidate = candidate_from_gallerydl(event)
    assert (candidate.author_username.casefold() == target.lstrip("@").casefold()) is expected
    assert event["owner_username"] == handle.lstrip("@")
    assert event["display_name"] == display_name


def test_x_owner_prefers_current_author_over_quoted_and_media_source_fields():
    post = {
        "tweet_id": "100", "author": {"name": "jackzhang123vip", "nick": "Jack Zhang"},
        "user": {"name": "quoted_user", "nick": "Quoted User"},
    }
    file_data = {
        "num": 1, "type": "photo", "source_user": {"name": "media_source"},
        "owner_username": "retweet_source",
    }
    event = gallerydl_worker._candidate_payload(
        post, file_data, platform="x",
        item_url="https://pbs.twimg.com/media/owner2.jpg?format=jpg&name=orig",
        source_username="jackzhang123vip",
    )
    assert event["owner_username"] == "jackzhang123vip"


@pytest.mark.parametrize("kind,url", [("video", "https://video.twimg.com/a.mp4"), ("animated_gif", "https://video.twimg.com/a.mp4"), ("video", "https://video.twimg.com/a.m3u8")])
def test_x_worker_maps_video_gif_and_hls(kind, url):
    event = gallerydl_worker._candidate_payload(
        {"tweet_id": "44", "user": {"nick": "alice"}},
        {"num": 1, "type": kind, "extension": "m3u8" if url.endswith("m3u8") else "mp4", "duration": 3.5, "bitrate": 900000},
        platform="x", item_url=url, source_url="https://x.com/alice/media", source_username="alice",
    )
    candidate = candidate_from_gallerydl(event)
    assert candidate.media_type == "video" and candidate.media_identity == "44:0"
    assert candidate.video_sources[0].kind == ("hls" if url.endswith("m3u8") else "mp4")


class FakeClient:
    is_packaged_helper = True
    def __init__(self, events): self.events, self.commands, self.stopped = list(events), [], False
    def start(self): self.commands.append("start")
    def send(self, command): self.commands.append(command)
    def read_event(self, _stop): return self.events.pop(0)
    def stop(self): self.stopped = True


def test_x_adapter_filters_ownership_quote_retweet_and_media_in_worker_configuration():
    fake = FakeClient([{"type": "ready", "cookie_loaded": True, "gallery_dl_version": "1.32.10"}, x_event(username="other"), x_event(), {"type": "exhausted"}])
    accepted, discovered, skipped, logs = [], [], [], []
    adapter = XGalleryDlAdapter(cookies={"auth_token": "secret", "ct0": "secret2"}, client_factory=lambda *_a, **_k: fake)
    adapter.collect("https://x.com/alice/media", source_type="author", source_username="alice", media_type="image",
                    on_candidate=lambda c: accepted.append(c) or True, on_discovered=discovered.append,
                    on_skipped=skipped.append, on_log=logs.append)
    assert len(discovered) == 2 and len(skipped) == 1 and len(accepted) == 1
    assert any("helper=packaged; cookie loaded=yes" in line for line in logs)
    assert all("secret" not in line for line in logs)


def test_x_adapter_prompt_filter_and_auth_lifecycle():
    fake = FakeClient([{"type": "ready", "cookie_loaded": True}, x_event(content="ordinary", description="ordinary"), {"type": "exhausted"}])
    skipped = []
    XGalleryDlAdapter(cookies={"auth_token": "a", "ct0": "c"}, client_factory=lambda *_a, **_k: fake).collect(
        "https://x.com/search?q=AI+art", source_type="search", only_prompt_posts=True,
        on_candidate=lambda _c: True, on_skipped=skipped.append,
    )
    assert len(skipped) == 1 and fake.stopped
    with pytest.raises(XAuthRequired):
        XGalleryDlAdapter(cookies={"auth_token": "a"}).collect(
            "https://x.com/search?q=x", source_type="search", on_candidate=lambda _c: True
        )


def test_x_adapter_pause_resume_and_stop_cleanup():
    import time

    pause = threading.Event()
    fake = FakeClient([{"type": "ready", "cookie_loaded": True}, {"type": "exhausted"}])
    timer = threading.Timer(0.03, pause.set)
    timer.start()
    try:
        XGalleryDlAdapter(cookies={"auth_token": "a", "ct0": "c"}, client_factory=lambda *_a, **_k: fake).collect(
            "https://x.com/search?q=x", source_type="search", pause_event=pause,
            on_candidate=lambda _c: True,
        )
    finally:
        timer.cancel()
    assert "pause" in fake.commands and "resume" in fake.commands and "next" in fake.commands
    assert fake.stopped

    stopped = threading.Event(); stopped.set()
    fake_stop = FakeClient([{"type": "ready", "cookie_loaded": True}])
    delivered = XGalleryDlAdapter(cookies={"auth_token": "a", "ct0": "c"}, client_factory=lambda *_a, **_k: fake_stop).collect(
        "https://x.com/search?q=x", source_type="search", stop_event=stopped,
        on_candidate=lambda _c: True,
    )
    assert delivered == 0 and fake_stop.stopped


def test_x_adapter_helper_error_and_auth_error_are_terminal():
    for event, error in [
        ({"type": "error", "message": "boom"}, Exception),
        ({"type": "error", "code": "AUTH_REQUIRED"}, XAuthRequired),
    ]:
        fake = FakeClient([{"type": "ready", "cookie_loaded": True}, event])
        with pytest.raises(error):
            XGalleryDlAdapter(cookies={"auth_token": "a", "ct0": "c"}, client_factory=lambda *_a, **_k: fake).collect(
                "https://x.com/search?q=x", source_type="search", on_candidate=lambda _c: True
            )
        assert fake.stopped


def test_x_worker_sets_twitter_configuration_and_uses_search_extractor(monkeypatch, tmp_path: Path):
    settings = []
    class Instance:
        category, subcategory, cookies_names, cookies_domain = "twitter", "search", ("auth_token",), ".x.com"
        def initialize(self): return None
        def cookies_check(self, *_args): return True
        def items(self): return iter(())
    fake_extractor = SimpleNamespace(find=lambda _url: Instance())
    fake_config = SimpleNamespace(set=lambda *args: settings.append(args))
    fake_gallery = ModuleType("gallery_dl"); fake_gallery.__version__ = "1.32.10"
    fake_gallery.config, fake_gallery.extractor = fake_config, fake_extractor
    monkeypatch.setitem(sys.modules, "gallery_dl", fake_gallery)
    monkeypatch.setitem(sys.modules, "gallery_dl.config", fake_config)
    monkeypatch.setitem(sys.modules, "gallery_dl.extractor", fake_extractor)
    monkeypatch.setattr(gallerydl_worker.sys, "stdin", io.StringIO('{"type":"next"}\n'))
    output = []; monkeypatch.setattr(gallerydl_worker, "_write", output.append)
    assert gallerydl_worker.run({"platform": "x", "url": "https://x.com/search?q=AI+art", "profile_path": str(tmp_path), "cookies": {"auth_token": "a", "ct0": "c"}, "search_scope": "top"}) == 0
    assert output[-1] == {"type": "exhausted"}
    assert (("extractor", "twitter"), "retweets", False) in settings
    assert (("extractor", "twitter"), "quoted", False) in settings
    assert (("extractor", "twitter"), "search-results", "top") in settings


def test_x_home_is_not_gallerydl_url():
    assert not gallerydl_worker.X_MEDIA_URL_RE.match("https://x.com/home")
    assert not gallerydl_worker.X_SEARCH_URL_RE.match("https://x.com/home")
