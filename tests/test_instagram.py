from __future__ import annotations

from pathlib import Path

import pytest

from src.browser.instagram_collector import (
    InstagramCollector,
    InstagramMedia,
    InstagramPostMetadata,
    InstagramScanDiagnostics,
    choose_best_image,
    choose_best_video,
    extract_shortcode,
    media_identity,
    media_matches_filter,
    normalize_author,
    normalize_single_post_url,
    parse_srcset,
)
from src.browser.collector import ImageCandidate
from src.models import TaskConfig


@pytest.mark.parametrize(
    ("value", "username", "url"),
    [
        ("alice", "alice", "https://www.instagram.com/alice/"),
        ("@alice", "alice", "https://www.instagram.com/alice/"),
        ("https://instagram.com/alice/?utm_source=test", "alice", "https://www.instagram.com/alice/"),
        ("https://www.instagram.com/alice/#bio", "alice", "https://www.instagram.com/alice/"),
    ],
)
def test_instagram_author_normalize(value: str, username: str, url: str) -> None:
    assert normalize_author(value) == (username, url)


@pytest.mark.parametrize("value", [
    "https://www.instagram.com/explore/",
    "https://www.instagram.com/p/ABC123/",
    "https://www.instagram.com/reels/",
    "https://example.com/alice/",
    "not a username",
])
def test_instagram_author_normalize_rejects_non_author_urls(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_author(value)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("https://www.instagram.com/p/ABC123/?img_index=1#media", "https://www.instagram.com/p/ABC123/"),
        ("https://instagram.com/reel/XYZ_9/?utm_source=ig_web_copy_link", "https://www.instagram.com/reel/XYZ_9/"),
        ("https://www.instagram.com/reels/XYZ_10/?utm_source=ig_web_copy_link", "https://www.instagram.com/reel/XYZ_10/"),
        ("https://www.instagram.com/tv/OLD123/", "https://www.instagram.com/tv/OLD123/"),
    ],
)
def test_instagram_single_post_normalize_and_shortcode(value: str, canonical: str) -> None:
    assert normalize_single_post_url(value) == canonical
    assert extract_shortcode(value) == canonical.rstrip("/").rsplit("/", 1)[-1]


def test_instagram_single_post_rejects_non_post_urls() -> None:
    with pytest.raises(ValueError):
        normalize_single_post_url("https://www.instagram.com/alice/")


def test_instagram_media_identity_is_stable_for_single_and_carousel_items() -> None:
    assert media_identity("ABC123", 0) == "ABC123:0"
    assert media_identity("ABC123", 1) == "ABC123:1"
    assert media_identity("ABC123", 1) != media_identity("ABC123", 2)


def test_instagram_srcset_chooses_largest_image_candidate() -> None:
    candidates = parse_srcset("https://cdn.test/small.jpg 320w, https://cdn.test/medium.jpg 640w, https://cdn.test/large.jpg 1440w")
    best = choose_best_image(candidates)
    assert best == {"url": "https://cdn.test/large.jpg", "width": 1440, "source": "srcset"}


def test_instagram_payload_extracts_carousel_image_and_video() -> None:
    payload = {
        "shortcode": "ABC123",
        "owner": {"username": "alice"},
        "edge_media_to_caption": {"edges": [{"node": {"text": "A carousel"}}]},
        "edge_sidecar_to_children": {
            "edges": [
                {"node": {"__typename": "GraphImage", "display_resources": [{"src": "https://cdn.test/small.jpg", "config_width": 320}, {"src": "https://cdn.test/large.jpg", "config_width": 1440}]}},
                {"node": {"__typename": "GraphVideo", "is_video": True, "video_url": "https://cdn.test/clip.mp4", "thumbnail_src": "https://cdn.test/poster.jpg", "width": 1080, "height": 1920}},
                {"node": {"__typename": "GraphImage", "display_url": "https://cdn.test/third.jpg", "width": 900, "height": 900}},
            ],
        },
    }

    media = InstagramCollector._media_from_payloads([payload])
    info = InstagramCollector._post_info([payload])

    assert [(item.media_type, item.media_url, item.media_index) for item in media] == [
        ("image", "https://cdn.test/large.jpg", 0),
        ("video", "https://cdn.test/clip.mp4", 1),
        ("image", "https://cdn.test/third.jpg", 2),
    ]
    assert info["shortcode"] == "ABC123"
    assert info["author_username"] == "alice"
    assert info["caption"] == "A carousel"


def test_instagram_filters_image_video_and_mixed() -> None:
    assert media_matches_filter("image", "image")
    assert not media_matches_filter("video", "image")
    assert media_matches_filter("video", "video")
    assert not media_matches_filter("image", "video")
    assert media_matches_filter("image", "mixed")
    assert media_matches_filter("video", "mixed")


def test_instagram_poster_without_video_source_is_not_a_video_candidate() -> None:
    node = {"__typename": "GraphVideo", "is_video": True, "thumbnail_src": "https://cdn.test/poster.jpg"}
    assert InstagramCollector._media_from_node(node, 0) is None


def test_instagram_direct_video_url_becomes_candidate_without_dom_video_or_mp4_suffix() -> None:
    signed_url = "https://scontent.cdninstagram.com/v/t50.2886-16/clip?efg=abc%3D%3D&bytestart=0"
    payload = {
        "shortcode": "REEL123",
        "owner": {"username": "alice"},
        "media_type": 2,
        "video_url": signed_url,
        "width": 1080,
        "height": 1920,
    }

    media = InstagramCollector._media_from_payloads([payload])
    metadata = InstagramPostMetadata(
        "instagram", "single_post", "https://www.instagram.com/reel/REEL123/", "REEL123", "REEL123",
        "https://www.instagram.com/reel/REEL123/", "alice", "", None, "2026-01-01T00:00:00",
        "video", tuple(media), "direct_mp4",
    )
    candidates = InstagramCollector._candidates_from_metadata(metadata)

    assert len(candidates) == 1
    assert candidates[0].media_type == "video"
    assert candidates[0].media_url == signed_url
    assert candidates[0].source == "direct_mp4"
    assert candidates[0].media_identity == "REEL123:0"
    assert candidates[0].width == 1080
    assert candidates[0].height == 1920


def test_instagram_payload_media_targets_requested_shortcode_and_prefers_direct_url() -> None:
    payload = {
        "edges": [
            {"node": {
                "code": "TARGET123",
                "media_type": 2,
                "original_width": 1920,
                "original_height": 1080,
                "video_versions": [{"type": 101, "url": "https://cdn.test/target"}],
                "video_dash_manifest": "<MPD />",
            }},
            {"node": {
                "code": "RELATED999",
                "media_type": 2,
                "video_versions": [{"type": 101, "url": "https://cdn.test/related"}],
            }},
        ]
    }

    media = InstagramCollector._media_from_payloads([payload], "TARGET123")

    assert [(item.media_url, item.source, item.width, item.height) for item in media] == [
        ("https://cdn.test/target", "direct_mp4", 1920, 1080),
    ]


def test_instagram_network_mp4_response_enters_candidate_pool_with_unknown_dimensions() -> None:
    class Request:
        resource_type = "media"

    class Response:
        url = "https://cdninstagram.com/clip?sig=opaque"
        headers = {"content-type": "video/mp4; charset=binary"}
        request = Request()

    collector = InstagramCollector(Path("profile"), lambda _: None)
    collector._on_network_response(Response())
    Response.url = "https://cdninstagram.com/clip?sig=opaque&bytestart=818&byteend=1635"
    collector._on_network_response(Response())
    assert len(collector._network_media) == 1
    assert collector._network_media[0].media_type == "video"
    assert collector._network_media[0].width is None
    assert collector._network_media[0].source == "network:direct_mp4"
    assert collector.last_video_source_types == {"direct_mp4"}


def test_instagram_network_author_fallback_keeps_only_target_owner_posts() -> None:
    class Response:
        url = "https://www.instagram.com/api/graphql/"
        headers = {"content-type": "application/json"}

        def json(self):
            return {
                "data": {"edges": [
                    {"node": {"code": "TARGET1", "media_type": 1, "owner": {"username": "alice"}}},
                    {"node": {"code": "REEL2", "media_type": 2, "owner": {"username": "alice"}}},
                    {"node": {"code": "OTHER3", "media_type": 1, "owner": {"username": "bob"}}},
                ]}
            }

    collector = InstagramCollector(Path("profile"), lambda _: None)
    collector._author_discovery_username = "alice"
    collector._on_network_response(Response())

    assert collector._network_post_links == [
        "https://www.instagram.com/p/TARGET1/",
        "https://www.instagram.com/reel/REEL2/",
    ]


def test_instagram_video_quality_prefers_resolution_then_bitrate() -> None:
    videos = [
        InstagramMedia(0, "video", "https://cdn.test/720", 720, 1280, bitrate=900_000),
        InstagramMedia(1, "video", "https://cdn.test/1080-low", 1080, 1920, bitrate=700_000),
        InstagramMedia(2, "video", "https://cdn.test/1080-high", 1080, 1920, bitrate=1_200_000),
    ]
    assert choose_best_video(videos) == videos[2]


def test_instagram_dom_filter_excludes_avatar_and_accepts_post_media() -> None:
    assert not InstagramCollector._dom_item_belongs_to_post(
        {"article": False, "alt": "alice profile picture"},
        "https://www.instagram.com/alice/",
        "https://www.instagram.com/p/ABC123/",
    )
    assert InstagramCollector._dom_item_belongs_to_post(
        {"article": False, "alt": ""},
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/p/ABC123/",
    )


def test_instagram_author_post_links_are_normalized_and_deduplicated() -> None:
    class Locator:
        def evaluate_all(self, _script: str):
            return [
                "https://www.instagram.com/p/ABC123/?utm_source=profile",
                "/p/ABC123/#slide-2",
                "https://www.instagram.com/reel/XYZ789/",
                "https://www.instagram.com/explore/",
            ]

    class Page:
        url = "https://www.instagram.com/alice/"

        def locator(self, _selector: str):
            return Locator()

    assert InstagramCollector._discover_post_links(Page()) == [
        "https://www.instagram.com/p/ABC123/",
        "https://www.instagram.com/reel/XYZ789/",
    ]


def test_instagram_candidates_include_metadata_and_media_index() -> None:
    payload = {
        "shortcode": "ABC123",
        "owner": {"username": "alice"},
        "caption": "Hello",
        "display_resources": [{"src": "https://cdn.test/image.jpg", "config_width": 1200, "config_height": 800}],
    }
    info = InstagramCollector._post_info([payload])
    media = InstagramCollector._media_from_payloads([payload])
    metadata = InstagramPostMetadata(
        "instagram", "single_post", "https://www.instagram.com/p/ABC123/", "ABC123", "ABC123",
        "https://www.instagram.com/p/ABC123/", info["author_username"], info["caption"], None,
        "2026-01-01T00:00:00", "image", tuple(media), "display_resources",
    )
    candidates = InstagramCollector._candidates_from_metadata(metadata)

    assert candidates[0].platform == "instagram"
    assert candidates[0].media_identity == "ABC123:0"
    assert candidates[0].shortcode == "ABC123"
    assert candidates[0].caption == "Hello"
    assert candidates[0].width == 1200
    assert candidates[0].height == 800


def test_instagram_task_config_supports_author_and_single_post() -> None:
    author = TaskConfig("", 1, Path("out"), platform="instagram", collection_mode="author", source_identifier="alice", media_type="mixed")
    post = TaskConfig("", 1, Path("out"), platform="instagram", collection_mode="single_post", source_identifier="https://www.instagram.com/p/ABC123/", media_type="video")
    author.validate()
    post.validate()


def test_instagram_login_check_rejects_login_redirect_without_login_automation() -> None:
    class Locator:
        def count(self) -> int:
            return 0

    class Page:
        url = "https://www.instagram.com/accounts/login/"

        def goto(self, *_args, **_kwargs) -> None:
            return None

        def locator(self, _selector: str) -> Locator:
            raise AssertionError("URL redirect should stop the check before DOM probing")

        def click(self, *_args, **_kwargs) -> None:
            raise AssertionError("login check must not click")

        def fill(self, *_args, **_kwargs) -> None:
            raise AssertionError("login check must not fill")

    with pytest.raises(RuntimeError, match="Instagram 尚未登录"):
        InstagramCollector.ensure_logged_in(Page())


def test_instagram_login_check_only_probes_at_collection_time() -> None:
    calls: list[str] = []

    class Locator:
        def count(self) -> int:
            return 0

    class Page:
        url = "https://www.instagram.com/"

        def goto(self, url: str, **_kwargs) -> None:
            calls.append(url)

        def locator(self, _selector: str) -> Locator:
            return Locator()

        def click(self, *_args, **_kwargs) -> None:
            raise AssertionError("login check must not click")

        def fill(self, *_args, **_kwargs) -> None:
            raise AssertionError("login check must not fill")

    InstagramCollector.ensure_logged_in(Page())
    assert calls == ["https://www.instagram.com/"]


def test_instagram_author_processes_incrementally_and_stops_after_success_target() -> None:
    class Mouse:
        def __init__(self) -> None:
            self.wheels = 0

        def wheel(self, _x: int, _y: int) -> None:
            self.wheels += 1

    class Page:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/alice/"
            self.mouse = Mouse()
            self.post_gotos: list[str] = []

        def goto(self, url: str, **_kwargs) -> None:
            if "/p/" in url:
                self.post_gotos.append(url)

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def on(self, *_args) -> None:
            return None

    page = Page()
    collector = InstagramCollector(Path("profile"), lambda _: None)
    collector.ensure_logged_in = lambda _page: None
    collector._task_page = lambda: page
    links = [f"https://www.instagram.com/p/P{i}/" for i in range(1, 4)]
    collector._discover_post_links = lambda _page: links[:2] if page.mouse.wheels == 0 else links

    def discover(_page, post_url, _mode, _author, _media_type, **kwargs):
        index = post_url.rsplit("/", 2)[-2]
        kwargs["on_candidate"](ImageCandidate(
            image_url=f"https://cdn.test/{index}.jpg",
            pin_url=post_url,
            platform="instagram",
            post_url=post_url,
            media_identity=f"{index}:0",
        ))
        return None, InstagramScanDiagnostics()

    collector._discover_post = discover
    successes: list[str] = []
    collector.collect_author(
        "alice", 3, 20, lambda: False, lambda: None, lambda _count: None,
        on_candidate=lambda candidate: successes.append(candidate.media_identity),
        should_finish=lambda: len(successes) >= 3,
        media_type="image",
    )

    assert successes == ["P1:0", "P2:0", "P3:0"]
    assert page.post_gotos == links
    assert page.mouse.wheels == 1


def test_instagram_author_does_not_stop_on_discovered_count_and_has_no_new_safety_cap() -> None:
    class Mouse:
        def __init__(self) -> None:
            self.wheels = 0

        def wheel(self, _x: int, _y: int) -> None:
            self.wheels += 1

    class Page:
        def __init__(self) -> None:
            self.url = "https://www.instagram.com/alice/"
            self.mouse = Mouse()

        def goto(self, _url: str, **_kwargs) -> None:
            return None

        def wait_for_timeout(self, _milliseconds: int) -> None:
            return None

        def on(self, *_args) -> None:
            return None

    page = Page()
    collector = InstagramCollector(Path("profile"), lambda _: None)
    collector.ensure_logged_in = lambda _page: None
    collector._task_page = lambda: page
    collector._discover_post_links = lambda _page: ["https://www.instagram.com/p/ONLY/"]
    processed: list[str] = []

    def discover(_page, post_url, _mode, _author, _media_type, **_kwargs):
        processed.append(post_url)
        return None, InstagramScanDiagnostics()

    collector._discover_post = discover
    collector.collect_author("alice", 3, 50, lambda: False, lambda: None, lambda _count: None, media_type="image")

    assert processed == ["https://www.instagram.com/p/ONLY/"]
    assert page.mouse.wheels == InstagramCollector.AUTHOR_NO_NEW_POST_ROUNDS
