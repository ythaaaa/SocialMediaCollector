from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from src.models import TASK_STATUSES, TaskConfig
from src.utils.x import normalize_x_author


class BatchInputError(ValueError):
    def __init__(self, line_number: int, value: str, reason: str) -> None:
        self.line_number = line_number
        self.value = value
        self.reason = reason
        super().__init__(f"第 {line_number} 行无效：{reason}")


@dataclass
class QueueTask:
    config: TaskConfig
    status: str = "pending"
    error: str = ""

    def __post_init__(self) -> None:
        if self.status not in TASK_STATUSES:
            self.status = "pending"

    @property
    def task_id(self) -> str:
        return self.config.task_id

    @property
    def source_value(self) -> str:
        return self.config.canonical_source_value


REMOVABLE_QUEUE_STATUSES = frozenset({"pending", "stopped"})


def remove_queue_tasks(tasks: list[QueueTask], task_ids: set[str]) -> list[QueueTask]:
    """Return a queue without selected removable tasks.

    The caller should apply the returned list to the existing queue with
    slice assignment so a running QueueRunner keeps the same list object.
    """
    selected = [task for task in tasks if task.task_id in task_ids]
    if any(task.status == "running" for task in selected):
        raise ValueError("运行中的任务请先停止后再移除。")
    if any(task.status not in REMOVABLE_QUEUE_STATUSES for task in selected):
        raise ValueError("只能删除等待或已停止的任务。")
    selected_ids = {task.task_id for task in selected}
    return [task for task in tasks if task.task_id not in selected_ids]


def parse_batch_sources(platform: str, collection_mode: str, text: str) -> list[str]:
    """Parse, trim, validate and de-duplicate one source per non-empty line."""
    platform = platform.strip().lower()
    collection_mode = collection_mode.strip().lower()
    if (platform, collection_mode) not in {("pinterest", "search"), ("x", "author"), ("x", "search")}:
        raise ValueError("批量任务目前支持 Pinterest 关键词搜索、X 关键词搜索和 X 作者主页")

    sources: list[str] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        value = raw_line.strip()
        if not value:
            continue
        try:
            if platform == "x" and collection_mode == "author":
                value = normalize_x_author(value)[0]
            elif platform == "x" and collection_mode == "search":
                TaskConfig(value, 1, Path("."), platform="x", collection_mode="search").validate()
            else:
                TaskConfig(value, 1, Path("."), platform="pinterest", collection_mode="search").validate()
        except ValueError as exc:
            raise BatchInputError(line_number, raw_line, str(exc)) from exc
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            sources.append(value)
    return sources


def build_batch_configs(base_config: TaskConfig, text: str) -> list[TaskConfig]:
    platform = base_config.canonical_platform()
    mode = base_config.canonical_collection_mode()
    sources = parse_batch_sources(platform, mode, text)
    configs: list[TaskConfig] = []
    for source in sources:
        configs.append(
            replace(
                base_config,
                task_id=uuid4().hex,
                status="pending",
                keyword=source if platform == "pinterest" else "",
                source_identifier=source if platform == "x" and mode in {"author", "search"} else "",
                # Keep the user's selected directory as the immutable save root.
                # The engine creates the per-task output directory at runtime.
                output_root=base_config.output_root,
            )
        )
    return configs
