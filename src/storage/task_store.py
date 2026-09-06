from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from src.models import DownloadRecord, TaskConfig


class TaskStore:
    fields = [
        "filename", "keyword", "pin_url", "image_url", "width", "height", "file_size",
        "image_format", "downloaded_at", "content_hash", "status", "reason",
        "collection_mode", "source_page_url", "platform", "author_username", "post_url",
        "only_author_content", "media_type", "media_url", "duration", "video_width",
        "video_height", "bitrate", "post_id", "post_text", "prompt_detected", "prompt",
        "posted_at", "collected_at", "shortcode", "caption", "media_index", "thumbnail_url",
        "metadata_source", "source", "hashtag", "source_hashtag", "source_type",
        "source_username", "source_board", "source_board_url", "source_owner",
    ]

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
        data["source_value"] = config.source_value
        data["download_interval"] = config.download_interval
        data["save_dir"] = str(config.save_dir)
        data["only_author_posts"] = config.only_author_posts
        data["only_prompt_posts"] = config.only_prompt_posts
        data["platform"] = config.canonical_platform()
        data["collection_mode"] = config.canonical_collection_mode()
        data["media_type"] = config.canonical_media_type()
        data["search_scope"] = config.canonical_search_scope()
        data["mode"] = data["collection_mode"]
        if config.canonical_platform() == "instagram" and config.canonical_collection_mode() == "hashtag":
            data["hashtag"] = config.canonical_source_value
            data["source_hashtag"] = config.canonical_source_value
        if config.canonical_platform() == "x" and config.canonical_collection_mode() == "author":
            username, profile_url = config.canonical_author()
            data["author_username"] = username
            data["source_page_url"] = profile_url
        elif config.canonical_platform() == "x":
            data["source_page_url"] = "https://x.com/home"
        elif config.canonical_platform() == "pinterest":
            data["source_type"] = config.source_type or config.canonical_collection_mode()
            if config.canonical_collection_mode() == "user":
                data["username"] = config.canonical_source_value
            elif config.canonical_collection_mode() == "board":
                data["board_url"] = config.canonical_source_value
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
        (self.task_dir / "task.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _ensure_csv(self, path: Path) -> None:
        if not path.exists():
            with path.open("w", newline="", encoding="utf-8-sig") as f:
                csv.DictWriter(f, fieldnames=self.fields).writeheader()

    def append(self, record: DownloadRecord) -> None:
        target = self.metadata_path if record.status == "downloaded" else self.failed_path
        fieldnames = self._fieldnames(target)
        with target.open("a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore").writerow(record.row())

    def _fieldnames(self, path: Path) -> list[str]:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as f:
                header = next(csv.reader(f), [])
        except (OSError, StopIteration):
            return self.fields
        return [field for field in self.fields if field in header] or self.fields

    def complete(self, stats: object, stopped: bool, error: str = "") -> None:
        path = self.task_dir / "task.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["completed_at"] = datetime.now().isoformat(timespec="seconds")
        data["stopped"] = stopped
        if error:
            data["error"] = error
        data["result"] = {key: getattr(stats, key) for key in ("discovered", "downloaded", "skipped", "duplicate_count", "failed")}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def known_hashes(self) -> set[str]:
        if not self.metadata_path.exists():
            return set()
        with self.metadata_path.open("r", newline="", encoding="utf-8-sig") as f:
            return {row["content_hash"] for row in csv.DictReader(f) if row.get("content_hash")}
