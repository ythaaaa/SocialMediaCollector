from __future__ import annotations

import hashlib
import io
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Callable

import httpx
from PIL import Image, UnidentifiedImageError

from src.browser import BrowserSessionManager, ImageCandidate, PinterestCollector, XCollector
from src.models import DownloadRecord, TaskConfig, TaskStats
from src.downloader.hls import HLSDownloadCancelled, HLSDownloadError, HLSDownloader
from src.storage import CollectorDatabase, TaskStore
from src.utils.paths import create_task_directory


class DownloadEngine:
    def __init__(
        self,
        config: TaskConfig,
        profile_dir: Path,
        on_log: Callable[[str], None] = print,
        on_stats: Callable[[TaskStats], None] = lambda _: None,
        on_task_dir: Callable[[Path], None] | None = None,
        database: CollectorDatabase | None = None,
        browser_manager: BrowserSessionManager | None = None,
    ) -> None:
        config.validate()
        self.config, self.profile_dir, self.on_log, self.on_stats = config, profile_dir, on_log, on_stats
        self.on_task_dir = on_task_dir
        self.database = database or CollectorDatabase(on_warning=on_log)
        self.browser_manager = browser_manager
        self.pause_event, self.stop_event = Event(), Event()
        self.pause_event.set()
        self.stats = TaskStats()
        self.task_dir: Path | None = None
        self.task_output_dir: Path | None = None

    def pause(self) -> None:
        self.pause_event.clear()
        self.database.update_task(self.config.task_id, status="paused")

    def resume(self) -> None:
        self.pause_event.set()
        self.database.update_task(self.config.task_id, status="running")

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()
        self.database.update_task(self.config.task_id, status="stopped")
    def _wait_if_paused(self) -> None:
        while not self.pause_event.wait(timeout=0.2):
            if self.stop_event.is_set(): return
    def _emit(self, status: str | None = None) -> None:
        if status: self.stats.current_status = status
        self.on_stats(self.stats)

    def run(self) -> Path:
        platform = self.config.canonical_platform()
        mode = self.config.canonical_collection_mode()
        if mode == "current_page" and not (self.browser_manager and self.browser_manager.is_running()):
            raise RuntimeError("请先点击『打开浏览器 / 登录』，在 Pinterest 中打开需要采集的页面，然后开始下载。")
        if platform == "x" and mode == "author":
            username, _ = self.config.canonical_author()
            task_name = f"x_{username}"
        elif platform == "x" and mode == "search":
            task_name = f"x_{self.config.canonical_source_value}"
        elif platform == "x":
            task_name = "x_首页推荐流"
        else:
            task_name = {
                "search": self.config.keyword,
                "home": "首页推荐流",
                "current_page": "当前页面",
            }[mode]
        # ``config.output_root`` is always the user-selected save root. The
        # generated task directory is a separate runtime-only value.
        self.task_output_dir = create_task_directory(self.config.output_root, task_name)
        self.task_dir = self.task_output_dir
        if self.on_task_dir:
            self.on_task_dir(self.task_output_dir)
        collector = XCollector(self.profile_dir, self.on_log, self.task_output_dir) if platform == "x" else PinterestCollector(self.profile_dir, self.on_log)
        store: TaskStore | None = None
        try:
            self._emit("正在打开浏览器并收集图片")
            if self.browser_manager and self.browser_manager.is_running() and self.browser_manager.endpoint:
                collector.connect_over_cdp(self.browser_manager.endpoint)
            else:
                collector.open()
            page = collector.prepare_current_page() if mode == "current_page" and hasattr(collector, "prepare_current_page") else getattr(collector, "page", None)
            if mode == "current_page" and page:
                self.config = replace(self.config, source_page_url=page.url)
            self.database.start_task(self.config, self.task_output_dir)
            store = TaskStore(self.task_output_dir, self.config)
            known = store.known_hashes()
            with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=30) as client:
                on_candidate = lambda candidate: self._process_candidate(client, candidate, store, known)
                should_finish = lambda: self.stats.downloaded >= self.config.target_count
                if platform == "x" and mode == "author":
                    candidates = collector.collect_author(self.config.canonical_source_value, self.config.target_count, self.config.max_scrolls, self.config.only_author_content, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish, self.config.canonical_media_type(), self.config.only_prompt_posts)
                elif platform == "x" and mode == "search":
                    candidates = collector.collect_search(
                        self.config.canonical_source_value,
                        self.config.target_count,
                        self.config.max_scrolls,
                        self.stop_event.is_set,
                        self._wait_if_paused,
                        self._on_found,
                        on_candidate,
                        should_finish,
                        self.config.canonical_media_type(),
                        self.config.only_prompt_posts,
                        self.config.canonical_search_scope(),
                        self._should_skip_discovered_candidate,
                    )
                elif platform == "x":
                    candidates = collector.collect_home(self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish, self.config.canonical_media_type(), self.config.only_prompt_posts)
                elif mode == "search":
                    candidates = collector.collect(self.config.keyword, self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish)
                elif mode == "home":
                    candidates = collector.collect_home(self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish)
                else:
                    candidates = collector.collect_current_page(self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish)
            self.on_log(f"收集完成，发现 {len(candidates)} 个候选资源。")
            self.on_log(
                f"下载统计：成功保存 {self.stats.downloaded}/{self.config.target_count}，"
                f"历史重复 {self.stats.duplicate_count}，下载失败 {self.stats.failed}，"
                f"已跳过 {self.stats.skipped}。"
            )
            if not self.stop_event.is_set() and self.stats.downloaded < self.config.target_count:
                self.on_log(f"目标 {self.config.target_count} 张，实际成功 {self.stats.downloaded} 张；页面暂无更多可采集内容。")
            status = "已停止" if self.stop_event.is_set() else ("任务完成" if self.stats.downloaded >= self.config.target_count else "采集结束（页面暂无更多内容）")
            assert store is not None
            store.complete(self.stats, self.stop_event.is_set())
            self.database.finish_task(
                self.config,
                self.stats,
                "stopped" if self.stop_event.is_set() else "completed",
                task_output_dir=self.task_output_dir,
            )
            self._emit(status)
        except Exception as exc:
            if store is not None:
                store.complete(self.stats, self.stop_event.is_set(), str(exc))
            self.database.finish_task(self.config, self.stats, "failed", str(exc), self.task_output_dir)
            raise
        finally:
            collector.close()
        return self.task_dir

    def _on_found(self, count: int) -> None:
        self.stats.discovered = count; self._emit()

    def _process_candidate(self, client: httpx.Client, candidate: ImageCandidate, store: TaskStore, known: set[str]) -> None:
        if self.stop_event.is_set() or self.stats.downloaded >= self.config.target_count:
            return
        self._wait_if_paused()
        if self.stop_event.is_set():
            return
        duplicate = self.database.find_duplicate(candidate, self.config.canonical_source_value)
        if duplicate:
            self.stats.duplicate_count += 1
            if self.stats.duplicate_count == 1 or self.stats.duplicate_count % 10 == 0:
                self.on_log(f"已跳过历史重复素材：{self.stats.duplicate_count} 个")
            self._emit()
            return
        self._emit("正在下载视频" if candidate.media_type == "video" else "正在下载图片")
        self._download_one(client, candidate, store, known)
        self._emit()
        self._sleep_interval()

    def _should_skip_discovered_candidate(self, candidate: ImageCandidate) -> bool:
        """Apply the existing Phase 4 identity check before Media detail-page work."""
        duplicate = self.database.find_duplicate(candidate, self.config.canonical_source_value)
        if not duplicate:
            return False
        self.stats.duplicate_count += 1
        if self.stats.duplicate_count == 1 or self.stats.duplicate_count % 10 == 0:
            self.on_log(f"已跳过历史重复素材：{self.stats.duplicate_count} 个")
        self._emit()
        return True

    def _sleep_interval(self) -> None:
        self._sleep_controlled(self.config.interval_seconds)

    def _sleep_controlled(self, seconds: float) -> None:
        remaining = seconds
        while remaining > 0 and not self.stop_event.is_set():
            self._wait_if_paused()
            if self.stop_event.is_set():
                return
            delay = min(0.2, remaining)
            time.sleep(delay)
            remaining -= delay

    def _download_one(self, client: httpx.Client, candidate: ImageCandidate, store: TaskStore, known: set[str]) -> None:
        platform = self.config.canonical_platform()
        record_keyword = self.config.keyword or self.config.source_identifier
        media_url = candidate.media_url or candidate.image_url
        record = DownloadRecord(
            "",
            record_keyword,
            candidate.pin_url,
            media_url,
            collection_mode=self.config.canonical_collection_mode(),
            source_page_url=candidate.source_page_url,
            platform=platform,
            author_username=candidate.author_username or self.config.author_username,
            post_url=candidate.post_url or candidate.pin_url,
            only_author_content=self.config.only_author_content,
            media_type=candidate.media_type,
            media_url=media_url,
            duration=candidate.duration,
            video_width=candidate.video_width,
            video_height=candidate.video_height,
            post_id=candidate.post_id,
            post_text=candidate.post_text,
            prompt_detected=candidate.prompt_detected,
            prompt=candidate.prompt,
            posted_at=candidate.posted_at,
            collected_at=datetime.now().isoformat(timespec="seconds"),
        )
        try:
            if candidate.media_type == "video":
                self._download_video(client, candidate, record, store, known)
                return
            content: bytes | None = None
            last_error: httpx.HTTPError | None = None
            download_urls = list(dict.fromkeys(url for url in (media_url, *candidate.fallback_urls) if url))
            for download_url in download_urls:
                for attempt in range(self.config.retry_count):
                    try:
                        referer = "https://x.com/" if platform == "x" else "https://www.pinterest.com/"
                        response = client.get(download_url, headers={"Referer": referer})
                        response.raise_for_status()
                        content = response.content
                        media_url = download_url
                        break
                    except httpx.HTTPError as exc:
                        last_error = exc
                        if attempt < self.config.retry_count - 1:
                            self._sleep_controlled(1 + attempt)
                if content is not None:
                    break
            if content is None:
                raise last_error or httpx.HTTPError("图片下载失败")
            record.image_url = media_url
            record.media_url = media_url
            digest = hashlib.sha256(content).hexdigest()
            if digest in known or self.database.find_hash_duplicate(digest):
                self.stats.duplicate_count += 1
                record.status, record.reason, record.content_hash = "skipped", "内容重复", digest
                store.append(record)
                if self.stats.duplicate_count == 1 or self.stats.duplicate_count % 10 == 0:
                    self.on_log(f"已跳过历史重复素材：{self.stats.duplicate_count} 个")
                return
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
            self.database.register_media(candidate, self.config.canonical_source_value, digest, self.task_dir / "images" / filename, self.config.task_id)
            self.on_log(f"已保存 {filename}（{width}×{height}）")
        except HLSDownloadCancelled:
            return
        except (httpx.HTTPError, HLSDownloadError, UnidentifiedImageError, OSError, ValueError) as exc:
            self.stats.failed += 1; record.status, record.reason = "failed", str(exc); store.append(record)
            suffix = "；当前视频已跳过，将继续处理后续候选。" if candidate.media_type == "video" else ""
            self.on_log(f"下载失败：{media_url} - {exc}{suffix}")

    def _download_video(self, client: httpx.Client, candidate: ImageCandidate, record: DownloadRecord, store: TaskStore, known: set[str]) -> None:
        assert self.task_dir
        videos_dir = self.task_dir / "videos"
        videos_dir.mkdir(exist_ok=True)
        basename = f"{self.stats.downloaded + 1:04d}.mp4"
        final_path = videos_dir / basename
        part_path = videos_dir / f".{basename}.part.mp4"
        try:
            self.on_log("Media视频阶段=downloading_hls")
            result = HLSDownloader(
                client,
                {"Referer": "https://x.com/", "User-Agent": "Mozilla/5.0"},
                self.config.retry_count,
                self.stop_event.is_set,
                self._wait_if_paused,
            ).download(candidate.media_url or candidate.image_url, part_path)
            if self.stop_event.is_set():
                raise HLSDownloadCancelled()
            digest = self._file_hash(part_path)
            if digest in known or self.database.find_hash_duplicate(digest, part_path):
                self.stats.duplicate_count += 1
                record.status, record.reason, record.content_hash = "skipped", "内容重复", digest
                store.append(record)
                if self.stats.duplicate_count == 1 or self.stats.duplicate_count % 10 == 0:
                    self.on_log(f"已跳过历史重复素材：{self.stats.duplicate_count} 个")
                return
            if self.stop_event.is_set():
                raise HLSDownloadCancelled()
            part_path.replace(final_path)
            if self.stop_event.is_set():
                if final_path.exists():
                    final_path.unlink()
                raise HLSDownloadCancelled()
            record.filename = f"videos/{basename}"
            record.file_size = final_path.stat().st_size
            record.image_format = "MP4"
            record.downloaded_at = datetime.now().isoformat(timespec="seconds")
            record.content_hash = digest
            record.status = "downloaded"
            record.duration = result.duration or record.duration
            record.video_width = result.width or record.video_width
            record.video_height = result.height or record.video_height
            record.bitrate = result.bitrate
            store.append(record)
            known.add(digest)
            self.stats.downloaded += 1
            self.database.register_media(candidate, self.config.canonical_source_value, digest, final_path, self.config.task_id)
            self.on_log(f"已保存 {record.filename}（视频）")
            self.on_log("Media视频阶段=mp4_saved")
        except HLSDownloadCancelled:
            self.on_log("Media视频阶段失败：failed_stage=stopped")
            raise
        except HLSDownloadError as exc:
            self.on_log(f"Media视频阶段失败：failed_stage=hls_download_or_ffmpeg，{exc}")
            raise
        finally:
            if part_path.exists():
                part_path.unlink()

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
