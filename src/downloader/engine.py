from __future__ import annotations

import hashlib
import io
import time
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable

import httpx
from PIL import Image, UnidentifiedImageError

from src.browser import PinterestCollector
from src.models import DownloadRecord, TaskConfig, TaskStats
from src.storage import TaskStore
from src.utils.paths import create_task_directory


class DownloadEngine:
    def __init__(self, config: TaskConfig, profile_dir: Path, on_log: Callable[[str], None] = print, on_stats: Callable[[TaskStats], None] = lambda _: None) -> None:
        config.validate()
        self.config, self.profile_dir, self.on_log, self.on_stats = config, profile_dir, on_log, on_stats
        self.pause_event, self.stop_event = Event(), Event()
        self.pause_event.set()
        self.stats = TaskStats()
        self.task_dir: Path | None = None

    def pause(self) -> None: self.pause_event.clear()
    def resume(self) -> None: self.pause_event.set()
    def stop(self) -> None: self.stop_event.set(); self.pause_event.set()
    def _wait_if_paused(self) -> None:
        while not self.pause_event.wait(timeout=0.2):
            if self.stop_event.is_set(): return
    def _emit(self, status: str | None = None) -> None:
        if status: self.stats.current_status = status
        self.on_stats(self.stats)

    def run(self) -> Path:
        self.task_dir = create_task_directory(self.config.output_root, self.config.keyword)
        store = TaskStore(self.task_dir, self.config)
        collector = PinterestCollector(self.profile_dir, self.on_log)
        try:
            self._emit("正在打开浏览器并收集图片")
            collector.open()
            candidates = collector.collect(self.config.keyword, self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found)
            self.on_log(f"收集完成，发现 {len(candidates)} 个候选资源。")
            self._emit("正在下载图片")
            known = store.known_hashes()
            with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=30) as client:
                for candidate in candidates:
                    if self.stop_event.is_set() or self.stats.downloaded >= self.config.target_count: break
                    self._wait_if_paused()
                    if self.stop_event.is_set(): break
                    self._download_one(client, candidate.image_url, candidate.pin_url, store, known)
                    self._emit()
                    if self.config.interval_seconds: time.sleep(self.config.interval_seconds)
            self._emit("已停止" if self.stop_event.is_set() else "任务完成")
        finally:
            collector.close()
        return self.task_dir

    def _on_found(self, count: int) -> None:
        self.stats.discovered = count; self._emit()

    def _download_one(self, client: httpx.Client, url: str, pin_url: str, store: TaskStore, known: set[str]) -> None:
        record = DownloadRecord("", self.config.keyword, pin_url, url)
        try:
            content = b""
            for attempt in range(self.config.retry_count):
                try:
                    response = client.get(url, headers={"Referer": "https://www.pinterest.com/"})
                    response.raise_for_status(); content = response.content; break
                except httpx.HTTPError:
                    if attempt == self.config.retry_count - 1: raise
                    time.sleep(1 + attempt)
            digest = hashlib.sha256(content).hexdigest()
            if digest in known:
                self.stats.skipped += 1; record.status, record.reason, record.content_hash = "skipped", "内容重复", digest; store.append(record); return
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                width, height, fmt = image.width, image.height, (image.format or "JPEG").upper()
            if width < self.config.min_width or height < self.config.min_height:
                self.stats.skipped += 1; record.status, record.reason = "skipped", "尺寸不足"; record.width, record.height = width, height; store.append(record); return
            extension = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp", "TIFF": ".tiff"}.get(fmt, "." + fmt.lower())
            filename = f"{self.stats.downloaded + 1:04d}{extension}"
            assert self.task_dir
            (self.task_dir / "images" / filename).write_bytes(content)
            record.filename, record.width, record.height, record.file_size = filename, width, height, len(content)
            record.image_format, record.downloaded_at, record.content_hash, record.status = fmt, datetime.now().isoformat(timespec="seconds"), digest, "downloaded"
            store.append(record); known.add(digest); self.stats.downloaded += 1
            self.on_log(f"已保存 {filename}（{width}×{height}）")
        except (httpx.HTTPError, UnidentifiedImageError, OSError, ValueError) as exc:
            self.stats.failed += 1; record.status, record.reason = "failed", str(exc); store.append(record); self.on_log(f"下载失败：{url} - {exc}")
