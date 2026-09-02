from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from src.models import TaskConfig
from src.utils.paths import application_data_directory


class CollectorDatabase:
    """Small, thread-safe-by-connection SQLite store for global media and history."""

    def __init__(self, path: Path | None = None, on_warning: Callable[[str], None] | None = None) -> None:
        self.path = Path(path) if path else application_data_directory() / "collector.db"
        self.on_warning = on_warning or (lambda _message: None)
        self._initialize()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _warn(self, exc: Exception) -> None:
        self.on_warning(f"历史/去重数据库暂不可用，下载仍会继续：{exc}")

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as db:
                db.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS media_index (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        platform TEXT NOT NULL,
                        media_type TEXT NOT NULL,
                        media_url TEXT NOT NULL DEFAULT '',
                        post_url TEXT NOT NULL DEFAULT '',
                        post_id TEXT NOT NULL DEFAULT '',
                        source_value TEXT NOT NULL DEFAULT '',
                        media_identity TEXT NOT NULL DEFAULT '',
                        file_hash TEXT NOT NULL DEFAULT '',
                        local_file TEXT NOT NULL DEFAULT '',
                        task_id TEXT NOT NULL DEFAULT '',
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS task_history (
                        task_id TEXT PRIMARY KEY,
                        platform TEXT NOT NULL,
                        collection_mode TEXT NOT NULL,
                        source_value TEXT NOT NULL DEFAULT '',
                        media_type TEXT NOT NULL,
                        target_count INTEGER NOT NULL DEFAULT 0,
                        success_count INTEGER NOT NULL DEFAULT 0,
                        duplicate_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL,
                        error_message TEXT NOT NULL DEFAULT '',
                        save_root TEXT NOT NULL DEFAULT '',
                        task_output_dir TEXT NOT NULL DEFAULT '',
                        started_at TEXT NOT NULL DEFAULT '',
                        finished_at TEXT NOT NULL DEFAULT '',
                        config_json TEXT NOT NULL DEFAULT '{}'
                    );
                    """
                )
                columns = {row[1] for row in db.execute("PRAGMA table_info(media_index)")}
                if "media_identity" not in columns:
                    db.execute("ALTER TABLE media_index ADD COLUMN media_identity TEXT NOT NULL DEFAULT ''")
                columns = {row[1] for row in db.execute("PRAGMA table_info(task_history)")}
                if "config_json" not in columns:
                    db.execute("ALTER TABLE task_history ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")
                db.executescript(
                    """
                    CREATE INDEX IF NOT EXISTS idx_media_url ON media_index(platform, media_url);
                    CREATE INDEX IF NOT EXISTS idx_media_file_hash ON media_index(file_hash);
                    CREATE INDEX IF NOT EXISTS idx_media_post_url ON media_index(post_url);
                    CREATE INDEX IF NOT EXISTS idx_media_post_id ON media_index(platform, post_id);
                    CREATE INDEX IF NOT EXISTS idx_media_task_id ON media_index(task_id);
                    CREATE INDEX IF NOT EXISTS idx_media_identity ON media_index(platform, source_value, media_identity);
                    CREATE INDEX IF NOT EXISTS idx_history_started_at ON task_history(started_at);
                    """
                )
        except Exception as exc:  # database health must not block collection
            self._warn(exc)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    @staticmethod
    def _local_exists(row: dict[str, Any]) -> bool:
        value = str(row.get("local_file") or "")
        return bool(value) and Path(value).exists()

    def _find_media(self, db: sqlite3.Connection, candidate: Any, source_value: str) -> dict[str, Any] | None:
        platform = str(getattr(candidate, "platform", "") or "pinterest").lower()
        media_url = str(getattr(candidate, "media_url", "") or getattr(candidate, "image_url", "") or "")
        if media_url:
            row = db.execute(
                "SELECT * FROM media_index WHERE platform = ? AND media_url = ? ORDER BY id LIMIT 1",
                (platform, media_url),
            ).fetchone()
            if row:
                return self._row(row)
        post_id = str(getattr(candidate, "post_id", "") or "")
        media_identity = str(getattr(candidate, "media_identity", "") or "")
        if platform == "x" and post_id and media_identity:
            row = db.execute(
                "SELECT * FROM media_index WHERE platform = ? AND post_id = ? AND media_identity = ? ORDER BY id LIMIT 1",
                (platform, post_id, media_identity),
            ).fetchone()
            if row:
                return self._row(row)
        if platform == "pinterest" and source_value and media_identity:
            row = db.execute(
                "SELECT * FROM media_index WHERE platform = ? AND source_value = ? AND media_identity = ? ORDER BY id LIMIT 1",
                (platform, source_value, media_identity),
            ).fetchone()
            if row:
                return self._row(row)
        return None

    def find_duplicate(self, candidate: Any, source_value: str = "") -> dict[str, Any] | None:
        try:
            with self._connect() as db:
                row = self._find_media(db, candidate, source_value)
                if not row:
                    return None
                if self._local_exists(row):
                    db.execute("UPDATE media_index SET last_seen_at = ? WHERE id = ?", (self._now(), row["id"]))
                    return row
                db.execute(
                    "UPDATE media_index SET local_file = '', last_seen_at = ? WHERE id = ?",
                    (self._now(), row["id"]),
                )
                return None
        except Exception as exc:
            self._warn(exc)
            return None

    def find_hash_duplicate(self, file_hash: str, current_file: Path | None = None) -> dict[str, Any] | None:
        if not file_hash:
            return None
        try:
            with self._connect() as db:
                for row in db.execute("SELECT * FROM media_index WHERE file_hash = ? ORDER BY id", (file_hash,)):
                    item = self._row(row)
                    if item and self._local_exists(item) and Path(item["local_file"]) != (current_file or Path("")):
                        db.execute("UPDATE media_index SET last_seen_at = ? WHERE id = ?", (self._now(), item["id"]))
                        return item
                return None
        except Exception as exc:
            self._warn(exc)
            return None

    def register_media(self, candidate: Any, source_value: str, file_hash: str, local_file: Path, task_id: str) -> None:
        try:
            now = self._now()
            platform = str(getattr(candidate, "platform", "") or "pinterest").lower()
            media_type = str(getattr(candidate, "media_type", "image") or "image")
            media_url = str(getattr(candidate, "media_url", "") or getattr(candidate, "image_url", "") or "")
            post_url = str(getattr(candidate, "post_url", "") or getattr(candidate, "pin_url", "") or "")
            post_id = str(getattr(candidate, "post_id", "") or "")
            media_identity = str(getattr(candidate, "media_identity", "") or "")
            with self._connect() as db:
                row = self._find_media(db, candidate, source_value)
                if row:
                    db.execute(
                        """UPDATE media_index SET media_type=?, media_url=?, post_url=?, post_id=?,
                           source_value=?, media_identity=?, file_hash=?, local_file=?, task_id=?, last_seen_at=?
                           WHERE id=?""",
                        (media_type, media_url, post_url, post_id, source_value, media_identity, file_hash,
                         str(local_file), task_id, now, row["id"]),
                    )
                else:
                    db.execute(
                        """INSERT INTO media_index
                           (platform, media_type, media_url, post_url, post_id, source_value, media_identity,
                            file_hash, local_file, task_id, first_seen_at, last_seen_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (platform, media_type, media_url, post_url, post_id, source_value, media_identity,
                         file_hash, str(local_file), task_id, now, now),
                    )
        except Exception as exc:
            self._warn(exc)

    def start_task(self, config: TaskConfig, task_output_dir: Path) -> None:
        try:
            now = self._now()
            data = asdict(config)
            data["output_root"] = str(config.output_root)
            data["search_scope"] = config.canonical_search_scope()
            with self._connect() as db:
                db.execute(
                    """INSERT INTO task_history
                       (task_id, platform, collection_mode, source_value, media_type, target_count, status,
                        save_root, task_output_dir, started_at, config_json)
                       VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                       ON CONFLICT(task_id) DO UPDATE SET status='running', error_message='',
                       task_output_dir=excluded.task_output_dir, started_at=excluded.started_at,
                       finished_at='', config_json=excluded.config_json""",
                    (config.task_id, config.canonical_platform(), config.canonical_collection_mode(),
                     config.canonical_source_value, config.canonical_media_type(), config.target_count,
                     str(config.output_root), str(task_output_dir), now, json.dumps(data, ensure_ascii=False)),
                )
        except Exception as exc:
            self._warn(exc)

    def finish_task(self, config: TaskConfig, stats: Any, status: str, error: str = "", task_output_dir: Path | None = None) -> None:
        self.update_task(
            config.task_id,
            success_count=int(getattr(stats, "downloaded", 0)),
            duplicate_count=int(getattr(stats, "duplicate_count", 0)),
            failed_count=int(getattr(stats, "failed", 0)),
            status=status,
            error_message=error,
            task_output_dir=str(task_output_dir or ""),
            finished_at=self._now(),
        )

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "success_count", "duplicate_count", "failed_count", "status", "error_message",
            "task_output_dir", "finished_at", "started_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        try:
            clause = ", ".join(f"{key} = ?" for key in values)
            with self._connect() as db:
                db.execute(f"UPDATE task_history SET {clause} WHERE task_id = ?", (*values.values(), task_id))
        except Exception as exc:
            self._warn(exc)

    def mark_running_interrupted(self) -> None:
        try:
            with self._connect() as db:
                db.execute(
                    "UPDATE task_history SET status='interrupted', error_message='interrupted', finished_at=? WHERE status='running'",
                    (self._now(),),
                )
        except Exception as exc:
            self._warn(exc)

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            with self._connect() as db:
                rows = db.execute("SELECT * FROM task_history ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)).fetchall()
                return [dict(row) for row in rows]
        except Exception as exc:
            self._warn(exc)
            return []

    def get_history(self, task_id: str) -> dict[str, Any] | None:
        try:
            with self._connect() as db:
                return self._row(db.execute("SELECT * FROM task_history WHERE task_id = ?", (task_id,)).fetchone())
        except Exception as exc:
            self._warn(exc)
            return None

    def delete_task_history(self, task_ids: Iterable[str]) -> int:
        """Delete only task history rows; media_index and files are untouched."""
        ids = [str(task_id).strip() for task_id in task_ids if str(task_id).strip()]
        if not ids:
            return 0
        placeholders = ", ".join("?" for _ in ids)
        try:
            with self._connect() as db:
                running = db.execute(
                    f"SELECT COUNT(*) FROM task_history WHERE task_id IN ({placeholders}) AND status = 'running'",
                    ids,
                ).fetchone()[0]
                if running:
                    return 0
                cursor = db.execute(
                    f"DELETE FROM task_history WHERE task_id IN ({placeholders})",
                    ids,
                )
                return max(0, cursor.rowcount)
        except Exception as exc:
            self._warn(exc)
            return 0

    def clear_task_history(self) -> int:
        """Clear task history in one transaction without touching global indexes."""
        try:
            with self._connect() as db:
                count = int(db.execute("SELECT COUNT(*) FROM task_history").fetchone()[0])
                if db.execute("SELECT COUNT(*) FROM task_history WHERE status = 'running'").fetchone()[0]:
                    return 0
                db.execute("DELETE FROM task_history")
                return count
        except Exception as exc:
            self._warn(exc)
            return 0

    def load_task_config(self, task_id: str) -> TaskConfig | None:
        row = self.get_history(task_id)
        if not row:
            return None
        try:
            data = json.loads(row.get("config_json") or "{}")
            if not isinstance(data, dict) or not data:
                return None
            return TaskConfig.from_dict(data)
        except (TypeError, ValueError, OSError):
            return None
