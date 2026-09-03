from __future__ import annotations

import threading
import time
import json
from pathlib import Path

import pytest

from src.downloader import queue_runner as runner_module
from src.models import TaskConfig, TaskStats
from src.storage import CollectorDatabase, QueueStore
from src.tasks import BatchInputError, QueueTask, build_batch_configs, parse_batch_sources, remove_queue_tasks
from src.utils.paths import create_task_directory, safe_folder_name


def _base_config(platform: str = "pinterest", mode: str = "search") -> TaskConfig:
    return TaskConfig("", 5, Path("downloads"), platform=platform, collection_mode=mode, interval_seconds=0)


def test_pinterest_batch_builds_four_tasks_ignores_empty_and_deduplicates() -> None:
    configs = build_batch_configs(
        _base_config(),
        "AI character design\n\n surreal photography \nAI character design\nproduct photography\ncute 3d character",
    )
    assert [config.keyword for config in configs] == [
        "AI character design",
        "surreal photography",
        "product photography",
        "cute 3d character",
    ]
    assert all(config.target_count == 5 for config in configs)
    assert configs[0].output_root == Path("downloads")
    assert all(config.output_root == Path("downloads") for config in configs)


def test_x_batch_normalizes_three_author_formats() -> None:
    configs = build_batch_configs(
        _base_config("x", "author"),
        "AIVideoHub_\n@some_author\nhttps://x.com/example",
    )
    assert [config.canonical_source_value for config in configs] == ["AIVideoHub_", "some_author", "example"]


def test_x_search_batch_builds_keyword_tasks_and_deduplicates() -> None:
    configs = build_batch_configs(
        _base_config("x", "search"),
        "AI illustration\n\n ai illustration \nGPT Image\nproduct photography",
    )
    assert [config.canonical_source_value for config in configs] == [
        "AI illustration", "GPT Image", "product photography"
    ]
    assert all(config.collection_mode == "search" for config in configs)


def test_x_search_batch_inherits_scope_and_media_type() -> None:
    configs = build_batch_configs(
        TaskConfig(
            "",
            2,
            Path("downloads"),
            platform="x",
            collection_mode="search",
            source_identifier="",
            media_type="video",
            search_scope="top",
        ),
        "AI video\nfashion video\ncinematic video",
    )

    assert [config.canonical_source_value for config in configs] == [
        "AI video", "fashion video", "cinematic video"
    ]
    assert all(config.canonical_search_scope() == "top" for config in configs)
    assert all(config.canonical_media_type() == "video" for config in configs)


def test_queue_tasks_create_sibling_directories_under_one_save_root(tmp_path: Path) -> None:
    configs = build_batch_configs(
        TaskConfig("", 5, tmp_path, platform="pinterest", collection_mode="search", interval_seconds=0),
        "one\ntwo\nthree",
    )
    directories = [create_task_directory(config.output_root, config.keyword) for config in configs]

    assert all(config.output_root == tmp_path for config in configs)
    assert all(directory.parent == tmp_path for directory in directories)


def test_queue_persists_history_dedup_policy(tmp_path: Path) -> None:
    config = TaskConfig(
        "cats",
        1,
        tmp_path,
        platform="pinterest",
        collection_mode="search",
        skip_history_duplicates=True,
    )
    store = QueueStore(tmp_path / "queue.json")
    store.save([QueueTask(config)])

    loaded = store.load()
    assert len(loaded) == 1
    assert loaded[0].config.skip_history_duplicates is True
    assert json.loads((tmp_path / "queue.json").read_text(encoding="utf-8"))["tasks"][0]["config"]["skip_history_duplicates"] is True


def test_remove_selected_tasks_updates_queue_json_and_reload(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "queue.json")
    tasks = _runner_tasks("one", "two", "three")
    store.save(tasks)

    tasks[:] = remove_queue_tasks(tasks, {tasks[1].task_id})
    store.save(tasks)

    assert [task.config.keyword for task in store.load()] == ["one", "three"]


@pytest.mark.parametrize("status", ["pending", "failed", "stopped", "completed"])
def test_remove_selected_task_allows_non_running_statuses(status: str) -> None:
    task = _runner_tasks("removable")[0]
    task.status = status

    assert remove_queue_tasks([task], {task.task_id}) == []


def test_remove_multiple_selected_tasks_and_keep_x_and_pinterest_tasks(tmp_path: Path) -> None:
    pinterest = QueueTask(TaskConfig("cats", 1, tmp_path, platform="pinterest", collection_mode="search"))
    x_search = QueueTask(TaskConfig("", 1, tmp_path, platform="x", collection_mode="search", source_identifier="cats"))
    x_author = QueueTask(TaskConfig("", 1, tmp_path, platform="x", collection_mode="author", source_identifier="author"))
    tasks = [pinterest, x_search, x_author]

    remaining = remove_queue_tasks(tasks, {pinterest.task_id, x_author.task_id})

    assert remaining == [x_search]


def test_running_task_is_protected_from_queue_removal(tmp_path: Path) -> None:
    tasks = _runner_tasks("running", "pending")
    tasks[0].status = "running"

    with pytest.raises(ValueError, match="运行中或暂停中的任务请先停止，再从队列中删除"):
        remove_queue_tasks(tasks, {tasks[0].task_id})
    assert [task.config.keyword for task in tasks] == ["running", "pending"]


def test_paused_task_is_protected_from_queue_removal() -> None:
    task = _runner_tasks("paused")[0]
    task.status = "paused"

    with pytest.raises(ValueError, match="运行中或暂停中的任务请先停止，再从队列中删除"):
        remove_queue_tasks([task], {task.task_id})


def test_mixed_selection_with_protected_task_is_rejected_without_partial_delete() -> None:
    tasks = _runner_tasks("failed", "running", "stopped")
    tasks[0].status = "failed"
    tasks[1].status = "running"
    tasks[2].status = "stopped"

    with pytest.raises(ValueError, match="运行中或暂停中的任务请先停止，再从队列中删除"):
        remove_queue_tasks(tasks, {task.task_id for task in tasks})
    assert [task.status for task in tasks] == ["failed", "running", "stopped"]


def test_failed_queue_removal_keeps_history_files_and_media_index(tmp_path: Path) -> None:
    database = CollectorDatabase(tmp_path / "collector.db")
    config = TaskConfig("failed", 1, tmp_path)
    output = tmp_path / "failed-task"
    output.mkdir()
    image = output / "image.jpg"
    image.write_bytes(b"image")
    database.start_task(config, output)
    database.finish_task(config, TaskStats(downloaded=1), "failed", task_output_dir=output)
    database.register_media(
        type("Candidate", (), {"platform": "pinterest", "media_type": "image", "media_url": "https://cdn.test/failed.jpg", "image_url": "https://cdn.test/failed.jpg", "post_url": "", "pin_url": "", "post_id": "", "media_identity": ""})(),
        "failed",
        "hash-failed",
        image,
        config.task_id,
    )
    task = QueueTask(config, status="failed")
    store = QueueStore(tmp_path / "queue.json")
    store.save([task])

    remaining = remove_queue_tasks([task], {task.task_id})
    store.save(remaining)

    assert store.load() == []
    assert QueueStore(tmp_path / "queue.json").load() == []
    assert database.get_history(config.task_id)["status"] == "failed"
    assert output.exists() and image.exists()
    assert database.find_hash_duplicate("hash-failed") is not None


def test_clear_pending_keeps_completed_and_failed_tasks(tmp_path: Path) -> None:
    tasks = _runner_tasks("pending", "completed", "failed")
    tasks[1].status = "completed"
    tasks[2].status = "failed"
    store = QueueStore(tmp_path / "queue.json")

    tasks[:] = [task for task in tasks if task.status != "pending"]
    store.save(tasks)

    assert [task.status for task in store.load()] == ["completed", "failed"]


def test_queue_removal_does_not_delete_task_history(tmp_path: Path) -> None:
    database = CollectorDatabase(tmp_path / "collector.db")
    config = TaskConfig("kept-history", 1, tmp_path)
    database.start_task(config, tmp_path / "output")
    database.finish_task(config, TaskStats(downloaded=1), "completed", task_output_dir=tmp_path / "output")
    tasks = [QueueTask(config)]

    tasks[:] = remove_queue_tasks(tasks, {config.task_id})

    assert database.get_history(config.task_id)["status"] == "completed"


def test_batch_invalid_line_reports_line_number() -> None:
    with pytest.raises(BatchInputError, match="第 2 行无效"):
        parse_batch_sources("x", "author", "valid_user\nnot a valid user")


def _runner_tasks(*sources: str) -> list[QueueTask]:
    return [QueueTask(TaskConfig(source, 1, Path("downloads"))) for source in sources]


def test_queue_runner_executes_tasks_in_order(monkeypatch, tmp_path: Path) -> None:
    order: list[str] = []

    class FakeEngine:
        def __init__(self, config, profile_dir, on_log, on_stats) -> None:
            self.config = config

        def run(self):
            order.append(self.config.keyword)

        def pause(self):
            return None

        def resume(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_module, "DownloadEngine", FakeEngine)
    tasks = _runner_tasks("one", "two", "three")
    runner = runner_module.QueueRunner(tasks, tmp_path / "profile", QueueStore(tmp_path / "queue.json"))
    runner.run()
    assert order == ["one", "two", "three"]
    assert [task.status for task in tasks] == ["completed", "completed", "completed"]


def test_failed_task_does_not_stop_following_task(monkeypatch, tmp_path: Path) -> None:
    order: list[str] = []

    class FakeEngine:
        def __init__(self, config, profile_dir, on_log, on_stats) -> None:
            self.config = config

        def run(self):
            order.append(self.config.keyword)
            if self.config.keyword == "bad":
                raise RuntimeError("mock failure")

        def pause(self):
            return None

        def resume(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_module, "DownloadEngine", FakeEngine)
    tasks = _runner_tasks("bad", "good")
    runner_module.QueueRunner(tasks, tmp_path / "profile", QueueStore(tmp_path / "queue.json")).run()
    assert order == ["bad", "good"]
    assert tasks[0].status == "failed"
    assert tasks[0].error == "mock failure"
    assert tasks[1].status == "completed"


def test_queue_runner_continues_with_remaining_tasks_after_pending_removal(monkeypatch, tmp_path: Path) -> None:
    order: list[str] = []
    tasks = _runner_tasks("one", "two", "three")

    class FakeEngine:
        def __init__(self, config, profile_dir, on_log, on_stats) -> None:
            self.config = config

        def run(self):
            order.append(self.config.keyword)
            if self.config.keyword == "one":
                tasks[:] = [task for task in tasks if task.config.keyword != "two"]

        def pause(self):
            return None

        def resume(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_module, "DownloadEngine", FakeEngine)
    runner_module.QueueRunner(tasks, tmp_path / "profile", QueueStore(tmp_path / "queue.json")).run()

    assert order == ["one", "three"]
    assert [task.status for task in tasks] == ["completed", "completed"]


def test_pause_blocks_next_task_until_stopped(monkeypatch, tmp_path: Path) -> None:
    order: list[str] = []
    holder: dict[str, runner_module.QueueRunner] = {}

    class FakeEngine:
        def __init__(self, config, profile_dir, on_log, on_stats) -> None:
            self.config = config

        def run(self):
            order.append(self.config.keyword)
            if self.config.keyword == "one":
                holder["runner"].pause()

        def pause(self):
            return None

        def resume(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_module, "DownloadEngine", FakeEngine)
    tasks = _runner_tasks("one", "two")
    runner = runner_module.QueueRunner(tasks, tmp_path / "profile", QueueStore(tmp_path / "queue.json"))
    holder["runner"] = runner
    thread = threading.Thread(target=runner.run)
    thread.start()
    time.sleep(0.1)
    assert order == ["one"]
    assert tasks[1].status == "pending"
    runner.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert tasks[1].status == "stopped"


def test_stop_prevents_following_tasks(monkeypatch, tmp_path: Path) -> None:
    order: list[str] = []
    holder: dict[str, runner_module.QueueRunner] = {}

    class FakeEngine:
        def __init__(self, config, profile_dir, on_log, on_stats) -> None:
            self.config = config

        def run(self):
            order.append(self.config.keyword)
            holder["runner"].stop()

        def pause(self):
            return None

        def resume(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(runner_module, "DownloadEngine", FakeEngine)
    tasks = _runner_tasks("one", "two")
    runner = runner_module.QueueRunner(tasks, tmp_path / "profile", QueueStore(tmp_path / "queue.json"))
    holder["runner"] = runner
    runner.run()
    assert order == ["one"]
    assert [task.status for task in tasks] == ["stopped", "stopped"]


def test_queue_persistence_recovers_interrupted_running_task(tmp_path: Path) -> None:
    store = QueueStore(tmp_path / "queue.json")
    tasks = _runner_tasks("one")
    tasks[0].status = "running"
    store.save(tasks)
    loaded = store.load()
    assert loaded[0].status == "pending"
    assert loaded[0].error == "interrupted"


def test_windows_directory_names_are_cleaned_for_batch_paths() -> None:
    assert safe_folder_name('a<>:"/\\|?*b') == "a_________b"
