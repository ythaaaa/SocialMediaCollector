from pathlib import Path

import src.browser.x_collector as x_collector_module
from src.browser import ImageCandidate, XCollector
from src.browser.x_collector import XScanDiagnostics, build_x_search_query, build_x_search_url
from src.models import TaskConfig, TaskStats
from src.storage import CollectorDatabase, QueueStore
from src.tasks import QueueTask
from src.utils.x import normalize_x_author


def test_normalize_x_author_accepts_supported_inputs() -> None:
    expected = ("example", "https://x.com/example")
    assert normalize_x_author("example") == expected
    assert normalize_x_author("@example") == expected
    assert normalize_x_author("https://x.com/example") == expected
    assert normalize_x_author("https://twitter.com/example/") == expected


def test_x_task_config_validates_author_mode_and_legacy_defaults_to_pinterest() -> None:
    config = TaskConfig(
        keyword="",
        target_count=3,
        output_root=Path("out"),
        platform="x",
        source_identifier="@example",
        collection_mode="author",
    )
    config.validate()
    assert config.canonical_author() == ("example", "https://x.com/example")
    legacy = TaskConfig.from_dict({"keyword": "old", "target_count": 1, "output_root": "out"})
    assert legacy.canonical_platform() == "pinterest"


def test_x_collector_uses_original_media_and_filters_non_author_video_content() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [
                {"status": "https://x.com/example/status/1", "authorName": "example", "repost": False, "video": False, "media": [
                    "https://pbs.twimg.com/media/one.jpg?name=small",
                    "https://pbs.twimg.com/media/two.jpg?name=small",
                ]},
                {"status": "https://x.com/example/status/2", "authorName": "other", "repost": False, "video": False, "media": ["https://pbs.twimg.com/media/other.jpg?name=small"]},
                {"status": "https://x.com/example/status/3", "authorName": "example", "repost": True, "video": False, "media": ["https://pbs.twimg.com/media/repost.jpg?name=small"]},
                {"status": "https://x.com/example/status/4", "authorName": "example", "repost": False, "video": True, "media": ["https://pbs.twimg.com/media/video.jpg?name=small"]},
            ]

    class Page:
        url = "https://x.com/example"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    XCollector(Path("profile"), lambda _: None)._collect_visible_tweets(Page(), "example", Page.url, True, candidates, None)

    assert len(candidates) == 2
    assert all(candidate.platform == "x" for candidate in candidates.values())
    assert all("name=orig" in candidate.image_url for candidate in candidates.values())
    assert all(candidate.fallback_urls == (candidate.image_url.replace("name=orig", "name=small"),) for candidate in candidates.values())


def test_x_home_and_media_type_configurations_are_valid() -> None:
    for media_type in ("image", "video", "mixed"):
        config = TaskConfig(
            keyword="",
            target_count=3,
            output_root=Path("out"),
            platform="x",
            collection_mode="home",
            media_type=media_type,
        )
        config.validate()
        assert config.canonical_collection_mode() == "home"
        assert config.canonical_media_type() == media_type


def test_x_search_task_config_is_valid() -> None:
    config = TaskConfig(
        keyword="",
        target_count=3,
        output_root=Path("out"),
        platform="x",
        collection_mode="search",
        source_identifier="AI character",
    )
    config.validate()
    assert config.canonical_source_value == "AI character"
    assert config.canonical_search_scope() == "media"


def test_legacy_x_search_task_defaults_to_media_scope() -> None:
    config = TaskConfig.from_dict({
        "platform": "x",
        "collection_mode": "search",
        "source_value": "AI character",
        "target_count": 1,
        "output_root": "out",
    })
    assert config.canonical_search_scope() == "media"


def test_queue_and_history_preserve_search_scope_and_legacy_queue_defaults(tmp_path: Path) -> None:
    config = TaskConfig(
        "", 1, tmp_path, platform="x", collection_mode="search",
        source_identifier="raw keyword", media_type="video", search_scope="top",
    )
    queue_store = QueueStore(tmp_path / "queue.json")
    queue_store.save([QueueTask(config)])
    assert queue_store.load()[0].config.canonical_search_scope() == "top"

    legacy_store = QueueStore(tmp_path / "legacy-queue.json")
    legacy_store.path.write_text(
        '{"version": 1, "tasks": [{"config": {"platform": "x", "collection_mode": "search", '
        '"source_value": "old", "target_count": 1, "output_root": "out"}}]}',
        encoding="utf-8",
    )
    assert legacy_store.load()[0].config.canonical_search_scope() == "media"

    database = CollectorDatabase(tmp_path / "collector.db")
    database.start_task(config, tmp_path / "output")
    database.finish_task(config, TaskStats(), "completed", tmp_path / "output")
    assert database.load_task_config(config.task_id).canonical_search_scope() == "top"


def test_x_search_urls_separate_scope_and_media_operator() -> None:
    expected = {
        ("media", "image"): "https://x.com/search?q=AI+character+filter%3Aimages&src=typed_query&f=media",
        ("media", "video"): "https://x.com/search?q=AI+character+filter%3Anative_video&src=typed_query&f=media",
        ("media", "mixed"): "https://x.com/search?q=AI+character+filter%3Amedia&src=typed_query&f=media",
        ("top", "image"): "https://x.com/search?q=AI+character+filter%3Aimages&src=typed_query&f=top",
        ("top", "video"): "https://x.com/search?q=AI+character+filter%3Anative_video&src=typed_query&f=top",
        ("top", "mixed"): "https://x.com/search?q=AI+character+filter%3Amedia&src=typed_query&f=top",
    }
    for (scope, media_type), url in expected.items():
        assert build_x_search_url("AI character", scope, media_type) == url
    assert build_x_search_query("原始关键词", "video").startswith("原始关键词 ")
    assert "filter:native_video" in build_x_search_query("原始关键词", "video")


def test_x_collector_search_builds_media_search_url(monkeypatch) -> None:
    class Page:
        url = "about:blank"

        def __init__(self) -> None:
            self.goto_calls: list[str] = []

        def on(self, *_args) -> None:
            return None

        def goto(self, url, **_kwargs) -> None:
            self.goto_calls.append(url)
            self.url = url

    page = Page()
    collector = XCollector(Path("profile"), lambda _: None)
    collector._page = page
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        collector,
        "_collect_media_grid",
        lambda _page, source_url, *_args: captured.__setitem__("profile_url", source_url) or [],
    )

    collector.collect_search("AI character", 2, 1, lambda: False, lambda: None, lambda _count: None)

    assert page.goto_calls == [
        "https://x.com/search?q=AI+character+filter%3Aimages&src=typed_query&f=media"
    ]
    assert captured["profile_url"] == page.goto_calls[0]


def test_x_collector_top_search_keeps_article_collector(monkeypatch) -> None:
    class Page:
        url = "about:blank"

        def on(self, *_args) -> None:
            return None

        def goto(self, url, **_kwargs) -> None:
            self.url = url

    page = Page()
    collector = XCollector(Path("profile"), lambda _: None)
    collector._page = page
    calls: list[str] = []
    monkeypatch.setattr(collector, "_collect_timeline", lambda *_args: calls.append("top") or [])
    monkeypatch.setattr(collector, "_collect_media_grid", lambda *_args: calls.append("media") or [])

    collector.collect_search("AI character", 2, 1, lambda: False, lambda: None, lambda _count: None, search_scope="top")

    assert calls == ["top"]
    assert page.url.endswith("f=top")


def test_x_media_grid_extracts_images_and_video_without_thumbnail_image() -> None:
    payload = [
        {
            "href": "https://x.com/one/status/100/photo/1",
            "images": ["https://pbs.twimg.com/media/photo-a?format=jpg&name=small"],
            "video": False,
        },
        {
            "href": "https://x.com/two/status/200/video/1",
            "images": ["https://pbs.twimg.com/amplify_video_thumb/200/img/poster"],
            "video": True,
        },
        {
            "href": "https://x.com/one/status/100/photo/1",
            "images": ["https://pbs.twimg.com/media/photo-a?format=jpg&name=small"],
            "video": False,
        },
    ]

    class Locator:
        def evaluate_all(self, _script):
            return payload

    class Page:
        url = "https://x.com/search"

        def locator(self, selector):
            assert selector == 'a[href*="/status/"]'
            return Locator()

    collector = XCollector(Path("profile"), lambda _: None)
    collector._resolve_media_video_playlist = lambda _page, _status, _poster: "https://video.twimg.com/video.m3u8"
    candidates: dict[str, ImageCandidate] = {}
    diagnostics = collector._collect_visible_media_grid(Page(), Page.url, candidates, None, "mixed")

    assert diagnostics.grid_item_count == 3
    assert diagnostics.status_link_count == 3
    assert diagnostics.image_item_count == 2
    assert diagnostics.video_item_count == 1
    assert diagnostics.media_candidate_count == 2
    assert {candidate.media_type for candidate in candidates.values()} == {"image", "video"}
    assert all("amplify_video_thumb" not in candidate.media_url for candidate in candidates.values())


def test_x_media_grid_strict_image_and_video_modes() -> None:
    payload = [
        {
            "href": "https://x.com/one/status/100/photo/1",
            "images": ["https://pbs.twimg.com/media/photo-a?name=small"],
            "video": False,
        },
        {
            "href": "https://x.com/two/status/200/video/1",
            "images": ["https://pbs.twimg.com/amplify_video_thumb/200/img/poster"],
            "video": True,
        },
    ]

    class Locator:
        def evaluate_all(self, _script):
            return payload

    class Page:
        url = "https://x.com/search"

        def locator(self, _selector):
            return Locator()

    for media_type, expected in (("image", {"image"}), ("video", {"video"})):
        collector = XCollector(Path("profile"), lambda _: None)
        collector._resolve_media_video_playlist = lambda _page, _status, _poster: "video.m3u8"
        candidates: dict[str, ImageCandidate] = {}
        collector._collect_visible_media_grid(Page(), Page.url, candidates, None, media_type)
        assert {candidate.media_type for candidate in candidates.values()} == expected


def test_x_media_video_listener_is_registered_before_navigation_and_is_cleaned_up() -> None:
    events: list[str] = []

    class Response:
        url = "https://video.twimg.com/amplify_video/123/pl/master.m3u8"

    class Locator:
        def evaluate_all(self, _script):
            return None

    class DetailPage:
        url = "https://x.com/example/status/123"

        def on(self, event, callback):
            events.append(f"on:{event}")
            self.callback = callback

        def goto(self, _url, **_kwargs):
            assert events == ["new_page", "on:response"]
            self.callback(Response())
            events.append("goto")

        def locator(self, _selector):
            return Locator()

        def wait_for_timeout(self, _milliseconds):
            return None

        def remove_listener(self, event, _callback):
            events.append(f"remove:{event}")

        def close(self):
            events.append("close")

    class Context:
        def new_page(self):
            events.append("new_page")
            return DetailPage()

        def close(self):
            pass

    collector = XCollector(Path("profile"), lambda _: None)
    collector._context = Context()
    playlist = collector._resolve_media_video_playlist(
        object(), "https://x.com/example/status/123", "https://video.twimg.com/amplify_video_thumb/123/poster.jpg"
    )

    assert playlist.endswith("master.m3u8")
    collector.close()
    assert events == ["new_page", "on:response", "goto", "remove:response", "close"]


def test_x_media_video_reuses_one_worker_page_and_closes_it_at_task_end() -> None:
    events: list[str] = []

    class Response:
        def __init__(self, video_id: str) -> None:
            self.url = f"https://video.twimg.com/amplify_video/{video_id}/pl/master.m3u8"

    class Locator:
        def evaluate_all(self, _script):
            return None

    class DetailPage:
        def on(self, event, callback):
            events.append(f"on:{event}")
            self.callback = callback

        def goto(self, url, **_kwargs):
            events.append(f"goto:{url.rsplit('/', 1)[-1]}")
            self.callback(Response(url.rsplit("/", 2)[-2]))

        def locator(self, _selector):
            return Locator()

        def wait_for_timeout(self, _milliseconds):
            pass

        def remove_listener(self, event, _callback):
            events.append(f"remove:{event}")

        def close(self):
            events.append("close")

    class Context:
        def __init__(self) -> None:
            self.page = DetailPage()

        def new_page(self):
            events.append("new_page")
            return self.page

        def close(self):
            pass

    collector = XCollector(Path("profile"), lambda _: None)
    collector._context = Context()
    first = collector._resolve_media_video_playlist(
        object(), "https://x.com/example/status/123", "https://video.twimg.com/amplify_video_thumb/123/poster.jpg"
    )
    second = collector._resolve_media_video_playlist(
        object(), "https://x.com/example/status/456", "https://video.twimg.com/amplify_video_thumb/456/poster.jpg"
    )

    assert first.endswith("master.m3u8")
    assert second.endswith("master.m3u8")
    assert events.count("new_page") == 1
    collector.close()
    assert events.count("close") == 1


def test_x_media_video_hls_timeout_closes_worker_page_at_task_end(monkeypatch) -> None:
    events: list[str] = []

    class Locator:
        def evaluate_all(self, _script):
            return None

    class DetailPage:
        def on(self, _event, _callback):
            return None

        def goto(self, _url, **_kwargs):
            return None

        def locator(self, _selector):
            return Locator()

        def wait_for_timeout(self, _milliseconds):
            return None

        def remove_listener(self, _event, _callback):
            events.append("remove")

        def close(self):
            events.append("close")

    class Context:
        def new_page(self):
            return DetailPage()

        def close(self):
            pass

    ticks = iter((0.0, 21.0))
    monkeypatch.setattr(x_collector_module.time, "monotonic", lambda: next(ticks))
    collector = XCollector(Path("profile"), lambda _: None)
    collector._context = Context()

    assert collector._resolve_media_video_playlist(
        object(), "https://x.com/example/status/123", "https://video.twimg.com/amplify_video_thumb/123/poster.jpg"
    ) == ""
    collector.close()
    assert events == ["remove", "close"]


def test_x_media_video_duplicate_is_checked_before_detail_page() -> None:
    payload = [{
        "href": "https://x.com/example/status/123/video/1",
        "images": ["https://video.twimg.com/amplify_video_thumb/123/poster.jpg"],
        "video": True,
    }]

    class Locator:
        def evaluate_all(self, _script):
            return payload

    class Page:
        url = "https://x.com/search"

        def locator(self, _selector):
            return Locator()

    collector = XCollector(Path("profile"), lambda _: None)
    collector._resolve_media_video_playlist = lambda *_args: (_ for _ in ()).throw(AssertionError("detail page opened"))
    candidates: dict[str, ImageCandidate] = {}
    diagnostics = collector._collect_visible_media_grid(
        Page(), Page.url, candidates, None, "video", should_skip_candidate=lambda _candidate: True
    )

    assert candidates == {}
    assert diagnostics.duplicate_count == 1


def test_x_media_video_failure_continues_to_next_candidate() -> None:
    payload = [
        {
            "href": "https://x.com/example/status/123/video/1",
            "images": ["https://video.twimg.com/amplify_video_thumb/123/poster.jpg"],
            "video": True,
        },
        {
            "href": "https://x.com/example/status/456/video/1",
            "images": ["https://video.twimg.com/amplify_video_thumb/456/poster.jpg"],
            "video": True,
        },
    ]

    class Locator:
        def evaluate_all(self, _script):
            return payload

    class Page:
        url = "https://x.com/search"

        def locator(self, _selector):
            return Locator()

    collector = XCollector(Path("profile"), lambda _: None)
    collector._resolve_media_video_playlist = lambda _page, status, _poster: (
        "" if status.endswith("/123") else "https://video.twimg.com/456.m3u8"
    )
    candidates: dict[str, ImageCandidate] = {}
    diagnostics = collector._collect_visible_media_grid(Page(), Page.url, candidates, None, "video")

    assert diagnostics.hls_failed_count == 1
    assert len(candidates) == 1


def test_x_top_video_continues_after_failed_candidate_until_success_target() -> None:
    collector = XCollector(Path("profile"), lambda _: None)
    calls = 0
    successful = 0

    class Mouse:
        def wheel(self, *_args):
            return None

    class Page:
        mouse = Mouse()

        def wait_for_timeout(self, _milliseconds):
            return None

    def collect(_page, _username, _profile_url, _author_only, candidates, on_candidate, _media_type, _only_prompt):
        nonlocal calls, successful
        calls += 1
        candidate = ImageCandidate(
            f"video-{calls}", f"https://x.com/example/status/{calls}", media_type="video", media_url=f"video-{calls}"
        )
        candidates[f"video:{calls}"] = candidate
        on_candidate(candidate)
        if calls in {1, 3}:
            successful += 1
        return XScanDiagnostics(video_node_count=1, media_candidate_count=1)

    collector._collect_visible_tweets = collect
    collector._collect_timeline(
        Page(), "", "https://x.com/search", 2, 5, False, lambda: False, lambda: None,
        lambda _count: None, lambda _candidate: None, lambda: successful >= 2, "video", False,
    )

    assert calls == 3
    assert successful == 2


def test_x_collector_can_emit_video_playlist_candidates() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/example/status/1",
                "authorName": "example",
                "repost": False,
                "promoted": False,
                "video": True,
                "videos": [{
                    "poster": "https://video.twimg.com/amplify_video_thumb/123/img/poster.jpg",
                    "duration": 8.5,
                    "width": 720,
                    "height": 1280,
                }],
                "media": [],
            }]

    class Page:
        url = "https://x.com/home"

        def locator(self, selector):
            return Locator()

    collector = XCollector(Path("profile"), lambda _: None)
    collector._video_playlists["123"].append("https://video.twimg.com/amplify_video/123/pl/master.m3u8")
    candidates: dict[str, ImageCandidate] = {}
    collector._collect_visible_tweets(Page(), "", Page.url, False, candidates, None, "video")

    assert len(candidates) == 1
    candidate = next(iter(candidates.values()))
    assert candidate.media_type == "video"
    assert candidate.media_url.endswith("master.m3u8")
    assert candidate.video_width == 720


def test_x_top_video_deduplicates_same_playlist_across_posts() -> None:
    playlist = "https://video.twimg.com/amplify_video/123/pl/master.m3u8"

    class Locator:
        def evaluate_all(self, _script):
            return [
                {
                    "status": "https://x.com/example/status/1",
                    "authorName": "example",
                    "repost": False,
                    "video": True,
                    "videos": [{"poster": "https://video.twimg.com/amplify_video_thumb/123/poster-a.jpg"}],
                    "media": [],
                },
                {
                    "status": "https://x.com/example/status/2",
                    "authorName": "example",
                    "repost": False,
                    "video": True,
                    "videos": [{"poster": "https://video.twimg.com/amplify_video_thumb/456/poster-b.jpg"}],
                    "media": [],
                },
            ]

    class Page:
        url = "https://x.com/search"

        def locator(self, _selector):
            return Locator()

    collector = XCollector(Path("profile"), lambda _: None)
    collector._video_playlists["123"].append(playlist)
    collector._video_playlists["456"].append(playlist)
    candidates: dict[str, ImageCandidate] = {}

    collector._collect_visible_tweets(Page(), "", Page.url, False, candidates, None, "video")

    assert len(candidates) == 1
    assert next(iter(candidates.values())).media_url == playlist


def test_x_image_mode_rejects_video_thumbnail_and_video_media() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/example/status/1",
                "authorName": "example",
                "repost": False,
                "video": True,
                "videos": [{"poster": "https://video.twimg.com/amplify_video_thumb/123/poster.jpg"}],
                "media": ["https://pbs.twimg.com/media/video-poster.jpg"],
            }]

    class Page:
        url = "https://x.com/search"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    collector = XCollector(Path("profile"), lambda _: None)
    collector._collect_visible_tweets(Page(), "", Page.url, False, candidates, None, "image")

    assert candidates == {}


def test_x_author_username_from_dom_link_is_case_insensitive_and_ignores_display_name() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/AIVideoHub_/status/1",
                "authorUsername": "AIVideoHub_",
                "authorName": "AIVideoHub 🕊️",
                "repost": False,
                "video": False,
                "media": ["https://pbs.twimg.com/media/one.jpg?name=small"],
            }]

    class Page:
        url = "https://x.com/AIVideoHub_"

        def locator(self, selector):
            assert "article" in selector
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    collector = XCollector(Path("profile"), lambda _: None)
    diagnostics = collector._collect_visible_tweets(Page(), "aivideohub_", Page.url, True, candidates, None)

    assert len(candidates) == 1
    assert diagnostics.author_match_count == 1
    assert diagnostics.author_mismatch_count == 0


def test_x_missing_tweet_text_does_not_block_normal_image_collection() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/example/status/1",
                "authorUsername": "example",
                "repost": False,
                "video": False,
                "media": ["https://pbs.twimg.com/media/one.jpg?name=small"],
            }]

    class Page:
        url = "https://x.com/example"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    diagnostics = XCollector(Path("profile"), lambda _: None)._collect_visible_tweets(
        Page(), "example", Page.url, True, candidates, None
    )

    assert len(candidates) == 1
    assert diagnostics.tweet_text_count == 0
    assert diagnostics.media_candidate_count == 1


def test_x_author_mismatch_is_diagnosed_and_filtered() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/other/status/1",
                "authorUsername": "other",
                "repost": False,
                "video": False,
                "media": ["https://pbs.twimg.com/media/other.jpg?name=small"],
            }]

    class Page:
        url = "https://x.com/example"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    collector = XCollector(Path("profile"), lambda _: None)
    diagnostics = collector._collect_visible_tweets(Page(), "example", Page.url, True, candidates, None)

    assert candidates == {}
    assert diagnostics.raw_media_count == 1
    assert diagnostics.author_match_count == 0
    assert diagnostics.author_mismatch_count == 1
    assert collector._empty_result_message("example", True, False) == "已检测到媒体，但作者匹配过滤后无候选，请检查作者识别逻辑。"


def test_x_missing_author_is_conservative_and_does_not_raise() -> None:
    class Locator:
        def evaluate_all(self, script):
            return [{
                "status": "https://x.com/example/status/1",
                "repost": False,
                "video": False,
                "media": ["https://pbs.twimg.com/media/one.jpg?name=small"],
            }]

    class Page:
        url = "https://x.com/example"

        def locator(self, selector):
            return Locator()

    candidates: dict[str, ImageCandidate] = {}
    diagnostics = XCollector(Path("profile"), lambda _: None)._collect_visible_tweets(
        Page(), "example", Page.url, True, candidates, None
    )

    assert len(candidates) == 1
    assert diagnostics.author_unknown_count == 1


def test_x_empty_result_messages_distinguish_dom_media_and_prompt_failures() -> None:
    collector = XCollector(Path("profile"), lambda _: None)
    collector.last_scan_diagnostics.article_count = 0
    assert "未检测到帖子" in collector._empty_result_message("example", True, False)
    collector.last_scan_diagnostics.article_count = 2
    collector.last_scan_diagnostics.image_node_count = 0
    collector.last_scan_diagnostics.video_node_count = 0
    assert "未识别到符合条件的图片/视频" in collector._empty_result_message("example", True, False)
    collector.last_scan_diagnostics.image_node_count = 1
    collector.last_scan_diagnostics.author_mismatch_count = 2
    assert "作者匹配过滤后无候选" in collector._empty_result_message("example", True, False)
    collector.last_scan_diagnostics.author_mismatch_count = 0
    collector.last_scan_diagnostics.prompt_detected_count = 0
    assert "未发现疑似包含 Prompt" in collector._empty_result_message("example", False, True)


def test_x_failed_scan_can_save_minimal_debug_snapshot(tmp_path: Path) -> None:
    class Page:
        def content(self):
            return "<html><body>debug</body></html>"

        def screenshot(self, path, full_page):
            Path(path).write_bytes(b"png")

    collector = XCollector(Path("profile"), lambda _: None, tmp_path)
    collector.last_scan_diagnostics.article_count = 1
    collector.last_scan_diagnostics.media_candidate_count = 0
    collector._save_debug_snapshot_if_needed(Page())

    assert (tmp_path / "debug_x_page.html").read_text(encoding="utf-8") == "<html><body>debug</body></html>"
    assert (tmp_path / "debug_x_page.png").read_bytes() == b"png"
