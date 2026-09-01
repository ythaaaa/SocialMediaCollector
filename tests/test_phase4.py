from __future__ import annotations

import sqlite3
from pathlib import Path

from src.browser import ImageCandidate
from src.models import TaskConfig, TaskStats
from src.storage import CollectorDatabase


def _candidate(url: str, platform: str = "pinterest", media_type: str = "image") -> ImageCandidate:
    return ImageCandidate(
        image_url=url,
        pin_url="https://example.test/post/1",
        platform=platform,
        media_type=media_type,
        media_url=url,
        post_url="https://example.test/post/1",
        post_id="post-1" if platform == "x" else "",
        media_identity="media-1" if platform == "x" else "pin-1",
    )


def test_media_index_schema_and_url_dedup(tmp_path: Path) -> None:
    db = CollectorDatabase(tmp_path / "collector.db")
    columns = {row[1] for row in sqlite3.connect(db.path).execute("PRAGMA table_info(media_index)")}
    assert {"media_url", "post_url", "post_id", "file_hash", "local_file", "task_id"} <= columns
    local = tmp_path / "one.jpg"
    local.write_bytes(b"image")
    candidate = _candidate("https://cdn.test/a.jpg")
    db.register_media(candidate, "cats", "hash-a", local, "task-a")
    assert db.find_duplicate(candidate, "cats") is not None
    local.unlink()
    assert db.find_duplicate(candidate, "cats") is None


def test_hash_dedup_and_platform_media_identity(tmp_path: Path) -> None:
    db = CollectorDatabase(tmp_path / "collector.db")
    local = tmp_path / "one.mp4"
    local.write_bytes(b"video")
    pinterest = _candidate("https://cdn.test/shared", "pinterest", "image")
    x_video = _candidate("https://cdn.test/shared", "x", "video")
    db.register_media(pinterest, "source", "same-hash", local, "task-a")
    assert db.find_duplicate(x_video, "source") is None
    assert db.find_hash_duplicate("same-hash") is not None


def test_task_history_lifecycle_and_config_reload(tmp_path: Path) -> None:
    db = CollectorDatabase(tmp_path / "collector.db")
    config = TaskConfig("cats", 3, tmp_path / "root", platform="pinterest", collection_mode="search")
    output = tmp_path / "root" / "cats_1"
    db.start_task(config, output)
    assert db.get_history(config.task_id)["status"] == "running"
    db.finish_task(config, TaskStats(downloaded=2, duplicate_count=1, failed=1), "completed", task_output_dir=output)
    row = db.get_history(config.task_id)
    assert row["success_count"] == 2
    assert row["duplicate_count"] == 1
    assert row["failed_count"] == 1
    assert db.load_task_config(config.task_id).output_root == config.output_root


def test_running_history_is_marked_interrupted(tmp_path: Path) -> None:
    db = CollectorDatabase(tmp_path / "collector.db")
    config = TaskConfig("cats", 1, tmp_path)
    db.start_task(config, tmp_path / "cats_1")
    db.mark_running_interrupted()
    assert db.get_history(config.task_id)["status"] == "interrupted"
