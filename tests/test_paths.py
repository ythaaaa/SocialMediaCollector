from pathlib import Path

from src.utils.paths import safe_folder_name


def test_safe_folder_name_preserves_chinese_and_removes_windows_invalid_chars() -> None:
    assert safe_folder_name("东方:古风/美女") == "东方_古风_美女"
    assert safe_folder_name("   ") == "pinterest"
