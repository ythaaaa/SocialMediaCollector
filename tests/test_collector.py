from pathlib import Path

from src.browser.collector import ImageCandidate, PinterestCollector, _PinterestEndTracker


def test_srcset_prefers_largest_declared_candidate() -> None:
    assert PinterestCollector._best_url("small.jpg", "medium.jpg 474w, large.jpg 736w") == "large.jpg"


def test_pinimg_sized_url_prefers_original_variant() -> None:
    assert PinterestCollector._prefer_original_url("https://i.pinimg.com/736x/aa/bb/cc.jpg") == "https://i.pinimg.com/originals/aa/bb/cc.jpg"
    assert PinterestCollector._prefer_original_url("https://i.pinimg.com/60x60/aa/bb/cc.jpg") == "https://i.pinimg.com/originals/aa/bb/cc.jpg"


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


def test_current_page_accepts_pinterest_pages_but_rejects_login_and_other_sites() -> None:
    assert PinterestCollector._is_pinterest_page(FakePage("https://www.pinterest.com/pin/123/"))
    assert not PinterestCollector._is_pinterest_page(FakePage("https://www.pinterest.com/login/"))
    assert not PinterestCollector._is_pinterest_page(FakePage("https://example.com/"))


def test_current_page_reuses_existing_pinterest_tab_without_navigation(monkeypatch) -> None:
    class Page:
        url = "https://www.pinterest.com/pin/123/"

        def __init__(self) -> None:
            self.goto_calls: list[str] = []

        def evaluate(self, _script):
            return True

        def goto(self, url, **_kwargs):
            self.goto_calls.append(url)

    page = Page()
    collector = PinterestCollector(Path("profile"), lambda _: None)
    collector._context = type("Context", (), {"pages": [page]})()
    captured: list[object] = []
    monkeypatch.setattr(
        collector,
        "_collect_page",
        lambda selected_page, *_args, **_kwargs: captured.append(selected_page) or [],
    )

    collector.collect_current_page(1, 1, lambda: False, lambda: None, lambda _count: None)

    assert captured == [page]
    assert page.goto_calls == []


def test_current_page_without_managed_context_requests_browser_reopen() -> None:
    collector = PinterestCollector(Path("profile"), lambda _: None)

    try:
        collector.collect_current_page(1, 1, lambda: False, lambda: None, lambda _count: None)
    except RuntimeError as exc:
        assert "打开浏览器" in str(exc)
    else:
        raise AssertionError("expected a managed-browser prompt")


def test_pinterest_video_sources_prefer_highest_quality_and_deduplicate_urls() -> None:
    sources = PinterestCollector._video_sources(
        {
            "V_360P": {"url": "https://v1.pinimg.com/360.mp4", "width": 360, "height": 640, "duration": 1000},
            "V_720P": {"url": "https://v1.pinimg.com/720.mp4", "width": 720, "height": 1280, "duration": 2000},
            "V_HLS": {"url": "https://v1.pinimg.com/master.m3u8", "width": 720, "height": 1280, "duration": 2000},
            "V_HLS_DUP": {"url": "https://v1.pinimg.com/master.m3u8", "width": 720, "height": 1280},
        }
    )

    assert [source.kind for source in sources] == ["mp4", "hls", "mp4"]
    assert sources[0].width == 720 and sources[0].height == 1280
    assert sources[0].duration == 2.0


def test_pinterest_video_pin_is_candidate_but_its_poster_is_not_image() -> None:
    class Locator:
        def __init__(self, scripts, images) -> None:
            self.scripts, self.images = scripts, images

        def all_text_contents(self):
            return self.scripts

        def evaluate_all(self, _script):
            return self.images

    class Page:
        url = "https://www.pinterest.com/search/pins/?q=video"

        def __init__(self):
            self.scripts = [
                '{"initialReduxState":{"pins":{"123":{"id":"123","videos":{"id":"999","video_list":{"V_720P":{"url":"https://v1.pinimg.com/video.mp4","width":720,"height":1280,"duration":15000}}}}}}}'
            ]

        def locator(self, selector):
            if selector == "script":
                return Locator(self.scripts, [])
            return Locator([], [{"src": "https://i.pinimg.com/236x/poster.jpg", "srcset": "", "parent": "https://www.pinterest.com/pin/123/"}])

    collector = PinterestCollector(Path("profile"), lambda _: None)
    candidates: dict[str, ImageCandidate] = {}
    collector._collect_visible(Page(), candidates, require_pin=True, media_type="mixed")

    assert len(candidates) == 1
    candidate = next(iter(candidates.values()))
    assert candidate.media_type == "video"
    assert candidate.image_url == ""
    assert candidate.media_url.endswith("video.mp4")


def test_pinterest_empty_rounds_do_not_end_before_a_delayed_sixth_pin() -> None:
    tracker = _PinterestEndTracker()

    for _ in range(5):
        assert not tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)

    assert not tracker.observe(new_urls=True, height_grew=False, pin_count_grew=False)
    assert tracker.empty_rounds == 0


def test_pinterest_scroll_height_growth_resets_empty_rounds() -> None:
    tracker = _PinterestEndTracker()

    for _ in range(4):
        tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)
    assert not tracker.observe(new_urls=False, height_grew=True, pin_count_grew=False)
    assert tracker.empty_rounds == 0


def test_pinterest_new_media_url_continues_when_virtual_dom_pin_count_is_flat() -> None:
    tracker = _PinterestEndTracker()

    for _ in range(6):
        tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)
    assert not tracker.observe(new_urls=True, height_grew=False, pin_count_grew=False)
    assert tracker.empty_rounds == 0


def test_pinterest_recovery_with_new_content_resets_empty_rounds() -> None:
    tracker = _PinterestEndTracker()

    for _ in range(tracker.empty_round_limit):
        needs_recovery = tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)
    assert needs_recovery
    assert not tracker.recovery_finished(new_urls=True, height_grew=False, pin_count_grew=False)
    assert tracker.empty_rounds == 0


def test_pinterest_only_confirms_end_after_empty_recovery() -> None:
    tracker = _PinterestEndTracker()

    for _ in range(tracker.empty_round_limit):
        needs_recovery = tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)
    assert needs_recovery
    assert tracker.recovery_finished(new_urls=False, height_grew=False, pin_count_grew=False)


def test_pinterest_end_tracker_requires_ten_effective_empty_rounds() -> None:
    """A 17/50 task (and a feed full of skipped duplicates) cannot end after a short gap."""
    tracker = _PinterestEndTracker()

    for _ in range(9):
        assert not tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)
    assert tracker.observe(new_urls=False, height_grew=False, pin_count_grew=False)


def test_pinterest_collector_does_not_end_a_17_of_50_task_before_delayed_content(monkeypatch) -> None:
    class Page:
        def wait_for_timeout(self, _milliseconds):
            return None

    collector = PinterestCollector(Path("profile"), lambda _message: None)
    calls = 0

    def collect_visible(_page, candidates, **_kwargs):
        nonlocal calls
        calls += 1
        if calls in {1, 7}:  # Initial content, then a Pin after five empty scrolls.
            candidates[f"https://i.pinimg.com/originals/{calls}.jpg"] = ImageCandidate("", "")

    monkeypatch.setattr(collector, "_collect_visible", collect_visible)
    monkeypatch.setattr(collector, "_scroll_metrics", lambda _page: (1000, 10))
    monkeypatch.setattr(collector, "_scroll_page", lambda _page, _distance: None)

    candidates = collector._collect_page(
        Page(), 50, 6, lambda: False, lambda: None, lambda _count: None,
        None, lambda: False, True, "image", "empty",
    )

    assert len(candidates) == 2


def test_pinterest_collector_stops_at_successful_target_not_discovered_count(monkeypatch) -> None:
    class Page:
        def wait_for_timeout(self, _milliseconds):
            return None

    collector = PinterestCollector(Path("profile"), lambda _message: None)
    successful_saved = 0

    def collect_visible(_page, candidates, **_kwargs):
        index = len(candidates)
        candidate = ImageCandidate(f"https://i.pinimg.com/originals/{index}.jpg", "")
        candidates[candidate.image_url] = candidate
        _kwargs["on_candidate"](candidate)

    def saved(_candidate):
        nonlocal successful_saved
        successful_saved += 1

    monkeypatch.setattr(collector, "_collect_visible", collect_visible)
    monkeypatch.setattr(collector, "_scroll_metrics", lambda _page: (1000, 10))
    monkeypatch.setattr(collector, "_scroll_page", lambda _page, _distance: None)
    monkeypatch.setattr(collector, "_recover_page", lambda _page: None)

    candidates = collector._collect_page(
        Page(), 50, 60, lambda: False, lambda: None, lambda _count: None,
        saved, lambda: successful_saved >= 50, True, "image", "empty",
    )

    assert successful_saved == 50
    assert len(candidates) == 50
