from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from src.models import TaskConfig
from src.tasks import QueueTask
from src.utils.paths import application_data_directory


class QueueStore:
    """Persist the small V2 queue outside the repository and browser profile."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (application_data_directory() / "queue.json")

    @staticmethod
    def _config_dict(config: TaskConfig) -> dict[str, object]:
        data = asdict(config)
        data["output_root"] = str(config.output_root)
        data["source_value"] = config.source_value
        data["download_interval"] = config.download_interval
        data["save_dir"] = str(config.save_dir)
        data["only_author_posts"] = config.only_author_posts
        data["platform"] = config.canonical_platform()
        data["collection_mode"] = config.canonical_collection_mode()
        data["media_type"] = config.canonical_media_type()
        data["search_scope"] = config.canonical_search_scope()
        return data

    def save(self, tasks: Iterable[QueueTask]) -> None:
        payload = {
            "version": 1,
            "tasks": [
                {"status": task.status, "error": task.error, "config": self._config_dict(task.config)}
                for task in tasks
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def load(self) -> list[QueueTask]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        tasks: list[QueueTask] = []
        for item in payload.get("tasks", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            try:
                config_data = item.get("config") if isinstance(item.get("config"), dict) else item
                config = TaskConfig.from_dict(config_data)
            except (TypeError, ValueError, OSError):
                continue
            status = str(item.get("status") or config.status or "pending")
            error = str(item.get("error") or "")
            if status == "running":
                status = "pending"
                error = "interrupted"
            tasks.append(QueueTask(config, status, error))
        return tasks
