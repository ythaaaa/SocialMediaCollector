from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.models import DownloadRecord, TaskConfig


class TaskStore:
    fields = ["filename", "keyword", "pin_url", "image_url", "width", "height", "file_size", "image_format", "downloaded_at", "content_hash", "status", "reason"]

    def __init__(self, task_dir: Path, config: TaskConfig) -> None:
        self.task_dir = task_dir
        self.metadata_path = task_dir / "metadata.csv"
        self.failed_path = task_dir / "failed.csv"
        self._write_task(config)
        self._ensure_csv(self.metadata_path)
        self._ensure_csv(self.failed_path)

    def _write_task(self, config: TaskConfig) -> None:
        data = asdict(config)
        data["output_root"] = str(config.output_root)
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
        (self.task_dir / "task.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _ensure_csv(self, path: Path) -> None:
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=self.fields).writeheader()

    def append(self, record: DownloadRecord) -> None:
        target = self.metadata_path if record.status == "downloaded" else self.failed_path
        with target.open("a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=self.fields).writerow(record.row())

    def complete(self, stats: object, stopped: bool, error: str = "") -> None:
        path = self.task_dir / "task.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["completed_at"] = datetime.now().isoformat(timespec="seconds")
        data["stopped"] = stopped
        if error:
            data["error"] = error
        data["result"] = {key: getattr(stats, key) for key in ("discovered", "downloaded", "skipped", "failed")}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def known_hashes(self) -> set[str]:
        if not self.metadata_path.exists():
            return set()
        with self.metadata_path.open("r", newline="", encoding="utf-8-sig") as f:
            return {row["content_hash"] for row in csv.DictReader(f) if row.get("content_hash")}
