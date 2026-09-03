from pathlib import Path

import pytest

from src.utils.paths import create_task_directory, safe_folder_name
from src.models import TaskConfig
from src.utils.instagram import instagram_hashtag_url, normalize_hashtag


def test_safe_folder_name_preserves_chinese_and_removes_windows_invalid_chars() -> None:
    assert safe_folder_name("东方:古风/美女") == "东方_古风_美女"
    assert safe_folder_name("   ") == "pinterest"


def test_collection_mode_is_backward_compatible() -> None:
    base = dict(keyword="", target_count=20, output_root=Path("out"))
    assert TaskConfig(**base, mode="keyword_search").canonical_collection_mode() == "search"
    assert TaskConfig(**base, mode="home_feed").canonical_collection_mode() == "home"
    assert TaskConfig(**base, collection_mode="current_page").canonical_collection_mode() == "current_page"
    TaskConfig(**base, collection_mode="home").validate()
    TaskConfig(**base, collection_mode="current_page").validate()
    legacy = TaskConfig.from_dict({"keyword": "old", "target_count": 1, "output_root": "out"})
    assert legacy.canonical_collection_mode() == "search"
    assert legacy.skip_history_duplicates is False


def test_history_dedup_config_round_trips_and_defaults_false() -> None:
    config = TaskConfig.from_dict({
        "keyword": "old",
        "target_count": 1,
        "output_root": "out",
        "skip_history_duplicates": True,
    })
    assert config.skip_history_duplicates is True
    assert TaskConfig.from_dict({"keyword": "old", "target_count": 1, "output_root": "out", "skip_history_duplicates": "false"}).skip_history_duplicates is False


def test_instagram_hashtag_normalization_accepts_plain_hash_and_tag_url() -> None:
    assert normalize_hashtag("#aiart") == "aiart"
    assert normalize_hashtag(" aiart ") == "aiart"
    assert normalize_hashtag("https://www.instagram.com/explore/tags/%E6%97%85%E8%A1%8C/") == "旅行"
    assert instagram_hashtag_url("#AIART") == "https://www.instagram.com/explore/tags/aiart/"


def test_instagram_hashtag_normalization_rejects_empty_or_invalid_input() -> None:
    for value in ("", "#", "ai art", "https://www.instagram.com/p/ABC123/"):
        with pytest.raises(ValueError):
            normalize_hashtag(value)


def test_consecutive_task_directories_are_siblings_under_save_root(tmp_path: Path) -> None:
    first = create_task_directory(tmp_path, "x_A")
    second = create_task_directory(tmp_path, "x_B")

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first != second
