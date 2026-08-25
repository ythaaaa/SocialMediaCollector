from pathlib import Path

from src.browser import ImageCandidate, XCollector
from src.models import TaskConfig
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
