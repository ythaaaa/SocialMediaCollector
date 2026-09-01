from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from src.downloader import queue_runner as runner_module
from src.models import TaskConfig
from src.storage import QueueStore
from src.tasks import BatchInputError, QueueTask, build_batch_configs, parse_batch_sources
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


def test_queue_tasks_create_sibling_directories_under_one_save_root(tmp_path: Path) -> None:
    configs = build_batch_configs(
        TaskConfig("", 5, tmp_path, platform="pinterest", collection_mode="search", interval_seconds=0),
        "one\ntwo\nthree",
    )
    directories = [create_task_directory(config.output_root, config.keyword) for config in configs]

    assert all(config.output_root == tmp_path for config in configs)
    assert all(directory.parent == tmp_path for directory in directories)


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
