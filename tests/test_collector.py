from src.browser.collector import PinterestCollector


def test_srcset_prefers_largest_declared_candidate() -> None:
    assert PinterestCollector._best_url("small.jpg", "medium.jpg 474w, large.jpg 736w") == "large.jpg"
