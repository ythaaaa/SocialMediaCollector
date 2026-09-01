from pathlib import Path

from src.browser.collector import PinterestCollector


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
