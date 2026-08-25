from src.browser.collector import PinterestCollector


def test_srcset_prefers_largest_declared_candidate() -> None:
    assert PinterestCollector._best_url("small.jpg", "medium.jpg 474w, large.jpg 736w") == "large.jpg"


def test_pinimg_sized_url_prefers_original_variant() -> None:
    assert PinterestCollector._prefer_original_url("https://i.pinimg.com/736x/aa/bb/cc.jpg") == "https://i.pinimg.com/originals/aa/bb/cc.jpg"


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


def test_current_page_accepts_pinterest_pages_but_rejects_login_and_other_sites() -> None:
    assert PinterestCollector._is_pinterest_page(FakePage("https://www.pinterest.com/pin/123/"))
    assert not PinterestCollector._is_pinterest_page(FakePage("https://www.pinterest.com/login/"))
    assert not PinterestCollector._is_pinterest_page(FakePage("https://example.com/"))
