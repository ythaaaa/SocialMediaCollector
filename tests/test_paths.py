from pathlib import Path

from src.utils.paths import create_task_directory, safe_folder_name
from src.models import TaskConfig


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


def test_consecutive_task_directories_are_siblings_under_save_root(tmp_path: Path) -> None:
    first = create_task_directory(tmp_path, "x_A")
    second = create_task_directory(tmp_path, "x_B")

    assert first.parent == tmp_path
    assert second.parent == tmp_path
    assert first != second
