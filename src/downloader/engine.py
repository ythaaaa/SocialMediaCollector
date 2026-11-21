from __future__ import annotations

import hashlib
import io
import inspect
import logging
import os
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx
import certifi
from PIL import Image, UnidentifiedImageError

from src.browser import (
    BrowserSessionManager, ImageCandidate, InstagramCollector, PinterestCollector, PinterestVideoSource,
    XCollector, YouTubeBrowserSearchError, YouTubeShortsBrowserCollector,
)
from src.models import DownloadRecord, TaskConfig, TaskStats
from src.downloader.hls import HLSDownloadCancelled, HLSDownloadError, HLSDownloader
from src.downloader.dash import DASHDownloadCancelled, DASHDownloadError, DASHDownloader
from src.storage import CollectorDatabase, TaskStore
from src.utils.paths import create_task_directory
from src.utils.diagnostics import log_exception, log_task_stage
from src.utils.instagram import instagram_hashtag_url, normalize_hashtag
from src.adapters.instagram_gallerydl import (
    GalleryDlError,
    InstagramGalleryDlAdapter,
    PinterestGalleryDlAdapter,
    XGalleryDlAdapter,
    instagram_legacy_enabled,
)
from src.utils.pinterest import (
    normalize_pinterest_board_url,
    normalize_pinterest_pin_url,
    normalize_pinterest_username,
    pinterest_board_parts,
    pinterest_pin_id,
    pinterest_search_url,
    pinterest_user_url,
)
from src.utils.x import x_author_media_url, x_search_url
from src.utils.youtube import youtube_url_kind, youtube_video_id
from src.adapters.youtube import YouTubeAdapter, YouTubeCancelled, YouTubeError


logger = logging.getLogger(__name__)


class DownloadEngine:
    MAX_YOUTUBE_SEARCH_CANDIDATES = 150
    YOUTUBE_SEARCH_EXPANSION_STEP = 20
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
        self.on_control_status: Callable[[str], None] = lambda _status: None
        self.pause_event, self.stop_event = Event(), Event()
        self.pause_event.set()
        self.stats = TaskStats()
        self.task_dir: Path | None = None
        self.task_output_dir: Path | None = None
        self._diagnostic_stage = "task initialization"
        self._current_task_candidate_keys: set[tuple[str, str]] = set()
        self._current_task_hashes: set[str] = set()

    @property
    def diagnostic_stage(self) -> str:
        return self._diagnostic_stage

    @diagnostic_stage.setter
    def diagnostic_stage(self, stage: str) -> None:
        if stage == getattr(self, "_diagnostic_stage", None):
            return
        self._diagnostic_stage = stage
        try:
            log_task_stage(
                stage,
                task_id=self.config.task_id,
                platform=self.config.canonical_platform(),
                mode=self.config.canonical_collection_mode(),
            )
        except OSError:
            # Diagnostics storage must not interrupt a real collection task.
            pass

    def pause(self) -> None:
        self.pause_event.clear()
        self.database.update_task(self.config.task_id, status="paused")
        self.on_control_status("paused")

    def resume(self) -> None:
        self.pause_event.set()
        self.database.update_task(self.config.task_id, status="running")
        self.on_control_status("running")

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
        self.diagnostic_stage = "task configuration"
        self._current_task_candidate_keys.clear()
        self._current_task_hashes.clear()
        platform = self.config.canonical_platform()
        mode = self.config.canonical_collection_mode()
        gallerydl_enabled = (
            (platform == "instagram" and mode in {"single_post", "author", "hashtag"} and not instagram_legacy_enabled())
            or (platform == "pinterest" and mode in {"search", "single_post", "user", "board"})
            or (platform == "x" and mode in {"author", "search"})
        )
        gallerydl_author_username = ""
        gallerydl_source_hashtag = ""
        gallerydl_source_username = ""
        gallerydl_source_board_url = ""
        gallerydl_source_type = ""
        if platform == "instagram" and mode == "hashtag" and not gallerydl_enabled:
            raise RuntimeError("Instagram Hashtag 需要使用 gallery-dl；请取消设置 SMC_INSTAGRAM_LEGACY=1。")
        if platform == "instagram" and not gallerydl_enabled:
            if self.browser_manager is None:
                raise RuntimeError("Instagram 尚未登录，请先点击『打开浏览器 / 登录』，在 Chrome 中完成登录后再开始采集。")
            if self.browser_manager.is_login_running():
                raise RuntimeError("Instagram 登录浏览器仍在运行。请完成登录并关闭该浏览器后，再开始采集。")
            if not self.browser_manager.is_running():
                self.browser_manager.open_collection_browser("https://www.instagram.com/")
        if mode == "current_page" and not (self.browser_manager and self.browser_manager.is_running()):
            raise RuntimeError("请先点击『打开浏览器 / 登录』，在 Pinterest 中打开需要采集的页面，然后开始下载。")
        if platform == "instagram" and mode == "hashtag":
            hashtag = normalize_hashtag(self.config.canonical_source_value)
            gallerydl_source_hashtag = hashtag
            self.config = replace(
                self.config,
                source_identifier=hashtag,
                hashtag=hashtag,
                source_page_url=instagram_hashtag_url(hashtag),
            )
            task_name = f"instagram_hashtag_{hashtag}"
        elif platform == "instagram" and mode == "author":
            from src.browser.instagram_collector import normalize_author
            username, _ = normalize_author(self.config.canonical_source_value)
            gallerydl_author_username = username
            self.config = replace(self.config, source_identifier=username, source_page_url=f"https://www.instagram.com/{username}/")
            task_name = f"instagram_{username}"
        elif platform == "instagram" and mode == "single_post":
            from src.browser.instagram_collector import extract_shortcode
            from src.browser.instagram_collector import normalize_single_post_url
            post_url = normalize_single_post_url(self.config.canonical_source_value)
            self.config = replace(self.config, source_identifier=post_url, source_page_url=post_url)
            task_name = f"instagram_{extract_shortcode(post_url) or post_url}"
        elif platform == "pinterest" and mode == "search":
            query = self.config.keyword.strip()
            search_url = pinterest_search_url(query)
            self.config = replace(self.config, source_page_url=search_url)
            task_name = query
        elif platform == "pinterest" and mode == "single_post":
            pin_url = normalize_pinterest_pin_url(self.config.canonical_source_value)
            self.config = replace(self.config, source_identifier=pin_url, source_page_url=pin_url)
            task_name = f"pinterest_{pinterest_pin_id(pin_url) or pin_url.rsplit('/', 1)[-1]}"
        elif platform == "pinterest" and mode == "user":
            username = normalize_pinterest_username(self.config.canonical_source_value)
            source_url = pinterest_user_url(username)
            gallerydl_source_username = username
            gallerydl_source_type = "user"
            self.config = replace(
                self.config,
                source_identifier=username,
                username=username,
                source_type="user",
                source_page_url=source_url,
            )
            task_name = f"pinterest_user_{username}"
        elif platform == "pinterest" and mode == "board":
            board_url = normalize_pinterest_board_url(self.config.canonical_source_value)
            owner, board = pinterest_board_parts(board_url)
            gallerydl_source_board_url = board_url
            gallerydl_source_type = "board"
            self.config = replace(
                self.config,
                source_identifier=board_url,
                board_url=board_url,
                source_type="board",
                source_page_url=board_url,
            )
            task_name = f"pinterest_board_{owner}_{board}"
        elif platform == "x" and mode == "author":
            username, source_url = x_author_media_url(self.config.canonical_source_value)
            gallerydl_source_username = username
            gallerydl_source_type = "author"
            self.config = replace(
                self.config, source_identifier=username, author_username=username,
                source_page_url=source_url,
            )
            task_name = f"x_{username}"
        elif platform == "x" and mode == "search":
            raw_query = self.config.canonical_source_value
            query, source_url = x_search_url(raw_query, self.config.canonical_media_type())
            gallerydl_source_type = "search"
            self.config = replace(self.config, source_page_url=source_url)
            task_name = f"x_{raw_query}"
        elif platform == "x":
            task_name = "x_首页推荐流"
        elif platform == "youtube":
            if mode == "youtube_search":
                task_name = "youtube_search"
            else:
                kind = youtube_url_kind(self.config.canonical_source_value)
                self.config = replace(self.config, source_identifier=self.config.canonical_source_value, source_page_url=self.config.canonical_source_value)
                task_name = f"youtube_{kind}"
        else:
            task_name = {
                "search": self.config.keyword,
                "home": "首页推荐流",
                "current_page": "当前页面",
            }[mode]
        # ``config.output_root`` is always the user-selected save root. The
        # generated task directory is a separate runtime-only value.
        self.diagnostic_stage = "task directory creation"
        self.on_log(f"[diag] stage={self.diagnostic_stage} task_id={self.config.task_id} output_root={self.config.output_root}")
        self.task_output_dir = create_task_directory(self.config.output_root, task_name)
        self.task_dir = self.task_output_dir
        self.on_log(f"[diag] task directory ready path={self.task_output_dir}")
        if self.on_task_dir:
            self.on_task_dir(self.task_output_dir)
        if platform == "youtube":
            return self._run_youtube()
        collector = None
        if platform == "instagram" and not gallerydl_enabled:
            collector = InstagramCollector(self.profile_dir, self.on_log)
        elif platform == "x" and not gallerydl_enabled:
            collector = XCollector(self.profile_dir, self.on_log, self.task_output_dir)
        elif not gallerydl_enabled:
            collector = PinterestCollector(self.profile_dir, self.on_log)
        store: TaskStore | None = None
        try:
            self.diagnostic_stage = "browser connection"
            self.on_log(f"[diag] stage={self.diagnostic_stage} executable={__import__('sys').executable} resource_root={getattr(__import__('sys'), '_MEIPASS', '')}")
            self._emit("正在准备采集并下载素材")
            if gallerydl_enabled:
                self._emit(f"正在使用 gallery-dl 发现 {platform.title()} 素材")
            elif self.browser_manager and self.browser_manager.is_running() and self.browser_manager.endpoint:
                collector.connect_over_cdp(self.browser_manager.endpoint)
            else:
                collector.open()
            self.diagnostic_stage = "page read"
            page = collector.prepare_current_page() if collector is not None and mode == "current_page" and hasattr(collector, "prepare_current_page") else getattr(collector, "page", None)
            if mode == "current_page" and page:
                self.config = replace(self.config, source_page_url=page.url)
                self.on_log(f"[diag] current page selected host={urlsplit(page.url).hostname or ''}")
            self.database.start_task(self.config, self.task_output_dir)
            store = TaskStore(self.task_output_dir, self.config)
            known = store.known_hashes()
            self._current_task_hashes.update(known)
            self.diagnostic_stage = "HTTP/TLS client initialization"
            cert_path = Path(certifi.where())
            self.on_log(
                f"[diag] stage={self.diagnostic_stage} certifi_path={cert_path} "
                f"certifi_exists={cert_path.is_file()} certifi_size={cert_path.stat().st_size if cert_path.is_file() else 0} "
                f"SSL_CERT_FILE={os.environ.get('SSL_CERT_FILE', '')!r} "
                f"SSL_CERT_DIR={os.environ.get('SSL_CERT_DIR', '')!r}"
            )
            with httpx.Client(headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True, timeout=30) as client:
                self.diagnostic_stage = "page read"
                on_candidate = lambda candidate: self._process_candidate(client, candidate, store, known)
                should_finish = lambda: self.stats.downloaded >= self.config.target_count
                if gallerydl_enabled:
                    def process_gallery_candidate(candidate: ImageCandidate) -> bool:
                        before = self.stats.downloaded
                        self._process_candidate(client, candidate, store, known)
                        if self.stats.downloaded > before:
                            self.on_log(f"{platform.title()} download success")
                        return not self.stop_event.is_set() and self.stats.downloaded < self.config.target_count

                    def on_gallerydl_discovered(_candidate: ImageCandidate) -> None:
                        self.stats.discovered += 1
                        self._emit()

                    def on_gallerydl_skipped(_candidate: ImageCandidate) -> None:
                        self.stats.skipped += 1
                        self._emit()

                    adapter = (
                        InstagramGalleryDlAdapter() if platform == "instagram" else
                        PinterestGalleryDlAdapter() if platform == "pinterest" else
                        XGalleryDlAdapter()
                    )
                    collect_kwargs = {
                        "media_type": self.config.canonical_media_type(),
                        "stop_event": self.stop_event,
                        "pause_event": self.pause_event,
                        "on_candidate": process_gallery_candidate,
                        "on_discovered": on_gallerydl_discovered,
                        "on_skipped": on_gallerydl_skipped,
                        "on_log": self.on_log,
                    }
                    if platform == "instagram":
                        collect_kwargs.update(
                            author_username=gallerydl_author_username or None,
                            source_hashtag=gallerydl_source_hashtag or None,
                        )
                    elif platform == "pinterest":
                        collect_kwargs.update(
                            source_type=gallerydl_source_type or mode,
                            source_username=gallerydl_source_username or None,
                            source_board_url=gallerydl_source_board_url or None,
                        )
                    elif platform == "x":
                        collect_kwargs.update(
                            source_type=gallerydl_source_type or mode,
                            source_username=gallerydl_source_username or None,
                            search_scope=self.config.canonical_search_scope(),
                            only_prompt_posts=self.config.only_prompt_posts,
                        )
                    adapter.collect(
                        self.config.source_page_url,
                        **collect_kwargs,
                    )
                    if (
                        mode in {"author", "hashtag"}
                        and not self.stop_event.is_set()
                        and self.stats.downloaded < self.config.target_count
                    ):
                        raise GalleryDlError("source exhausted before target reached")
                elif platform == "instagram" and mode == "author":
                    candidates = collector.collect_author(self.config.canonical_source_value, self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish, self.config.canonical_media_type())
                elif platform == "instagram" and mode == "single_post":
                    candidates = collector.collect_single_post(self.config.canonical_source_value, self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish, self.config.canonical_media_type())
                elif platform == "x" and mode == "author":
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
                    collect_args = (self.config.keyword, self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish)
                    if "media_type" in inspect.signature(collector.collect).parameters:
                        collect_args += (self.config.canonical_media_type(),)
                    candidates = collector.collect(*collect_args)
                elif mode == "home":
                    collect_args = (self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish)
                    if "media_type" in inspect.signature(collector.collect_home).parameters:
                        collect_args += (self.config.canonical_media_type(),)
                    candidates = collector.collect_home(*collect_args)
                else:
                    self.diagnostic_stage = "resource parsing"
                    collect_args = (self.config.target_count, self.config.max_scrolls, self.stop_event.is_set, self._wait_if_paused, self._on_found, on_candidate, should_finish)
                    if "media_type" in inspect.signature(collector.collect_current_page).parameters:
                        collect_args += (self.config.canonical_media_type(),)
                    candidates = collector.collect_current_page(*collect_args)
            discovered_count = self.stats.discovered if gallerydl_enabled else len(candidates)
            self.on_log(f"收集完成，发现 {discovered_count} 个候选资源。")
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
            log_exception(exc, config=self.config, context={"stage": self.diagnostic_stage})
            self.on_log(f"[diag] failure stage={self.diagnostic_stage} type={type(exc).__name__} errno={getattr(exc, 'errno', '')} filename={getattr(exc, 'filename', '')} filename2={getattr(exc, 'filename2', '')}")
            stopped = self.stop_event.is_set()
            if store is not None:
                store.complete(self.stats, stopped, str(exc))
            self.database.finish_task(
                self.config,
                self.stats,
                "stopped" if stopped else "failed",
                str(exc),
                self.task_output_dir,
            )
            if stopped:
                self._emit("已停止")
                return self.task_dir
            raise
        finally:
            if collector is not None:
                collector.close()
        return self.task_dir

    def _run_youtube(self) -> Path:
        """Process yt-dlp entries sequentially so target counts mean saved files."""
        if self.config.canonical_collection_mode() == "youtube_search":
            return self._run_youtube_search()
        assert self.task_dir and self.task_output_dir
        store = TaskStore(self.task_dir, self.config)
        self.database.start_task(self.config, self.task_output_dir)
        adapter = YouTubeAdapter()
        known = store.known_hashes()
        self._current_task_hashes.update(known)
        try:
            self._emit("正在使用 yt-dlp 发现 YouTube 视频")
            for candidate in adapter.entries(self.config.canonical_source_value, cookie_source=self.config.youtube_cookie_source, browser_profile=self.config.youtube_browser_profile, cookie_file=self.config.youtube_cookie_file):
                if self.stop_event.is_set() or self.stats.downloaded >= self.config.target_count:
                    break
                self.stats.discovered += 1
                self._emit()
                self._wait_if_paused()
                keys = self._current_candidate_keys(candidate)
                if any(key in self._current_task_candidate_keys for key in keys):
                    self._record_duplicate("已跳过当前任务重复 YouTube 视频")
                    continue
                self._current_task_candidate_keys.update(keys)
                if self.config.skip_history_duplicates and self.database.find_duplicate(candidate, self.config.canonical_source_value, require_local_file=False):
                    self._record_duplicate("已跳过历史重复 YouTube 视频")
                    self._emit()
                    continue
                self._emit(f"正在下载：{candidate.post_text or candidate.post_id}")
                try:
                    self._download_youtube_candidate(adapter, candidate, store, known)
                except YouTubeCancelled:
                    break
            status = "已停止" if self.stop_event.is_set() else ("任务完成" if self.stats.downloaded >= self.config.target_count else "采集结束（YouTube 已无更多条目）")
            store.complete(self.stats, self.stop_event.is_set())
            self.database.finish_task(self.config, self.stats, "stopped" if self.stop_event.is_set() else "completed", task_output_dir=self.task_output_dir)
            self._emit(status)
            return self.task_dir
        except Exception as exc:
            store.complete(self.stats, self.stop_event.is_set(), str(exc))
            self.database.finish_task(self.config, self.stats, "stopped" if self.stop_event.is_set() else "failed", str(exc), self.task_output_dir)
            if self.stop_event.is_set():
                self._emit("已停止")
                return self.task_dir
            raise

    @staticmethod
    def _youtube_search_budget(target_count: int) -> int:
        multiplier = 3 if target_count <= 20 else 2
        return min(DownloadEngine.MAX_YOUTUBE_SEARCH_CANDIDATES, max(20, target_count * multiplier))

    def _run_youtube_search(self) -> Path:
        """Search each term through yt-dlp, then consume candidates round-robin."""
        assert self.task_dir and self.task_output_dir
        store = TaskStore(self.task_dir, self.config)
        self.database.start_task(self.config, self.task_output_dir)
        adapter = YouTubeAdapter()
        known = store.known_hashes()
        self._current_task_hashes.update(known)
        keywords = self.config.youtube_search_keywords
        initial_budget = self._youtube_search_budget(self.config.target_count)
        pools: dict[str, list[ImageCandidate]] = {}
        requested = {keyword: initial_budget for keyword in keywords}
        result_counts = {keyword: 0 for keyword in keywords}
        # Shorts/Videos first use YouTube's type-filtered search source.  A
        # filtered result set can legitimately be much smaller than a task's
        # target, so it must not terminate the whole search: its exhausted
        # keyword switches once to normal ytsearch and keeps the same type
        # check during metadata enrichment.
        active_content_types = {keyword: self.config.youtube_search_content_type for keyword in keywords}
        active_search_sources = {
            keyword: "browser" if self.config.youtube_search_source == "browser_shorts" else "ytdlp"
            for keyword in keywords
        }
        source_exhausted = {keyword: False for keyword in keywords}
        fallback_started: set[str] = set()
        search_seen_keys: set[tuple[str, str]] = set()
        search_seen_keys_by_keyword = {keyword: set() for keyword in keywords}
        reached_candidate_cap = False
        browser_search: YouTubeShortsBrowserCollector | None = None

        def browser_collector() -> YouTubeShortsBrowserCollector:
            nonlocal browser_search
            if browser_search is not None:
                return browser_search
            browser_search = YouTubeShortsBrowserCollector(self.profile_dir, self.on_log)
            try:
                if self.browser_manager and self.browser_manager.is_running() and self.browser_manager.endpoint:
                    browser_search.connect_over_cdp(self.browser_manager.endpoint)
                else:
                    browser_search.open()
            except Exception as exc:
                browser_search.close()
                browser_search = None
                raise YouTubeBrowserSearchError(f"无法启动浏览器辅助 Shorts 搜索：{exc}") from exc
            return browser_search

        def fetch(keyword: str, limit: int, *, expanding: bool) -> int:
            """Fetch a larger discovery prefix and append only new video ids."""
            nonlocal reached_candidate_cap
            if self.stop_event.is_set():
                return 0
            self._wait_if_paused()
            if self.stop_event.is_set():
                return 0
            try:
                if active_search_sources[keyword] == "browser":
                    candidates = browser_collector().search(
                        keyword, limit, should_stop=self.stop_event.is_set,
                        should_pause=self._wait_if_paused, max_scrolls=self.config.max_scrolls,
                    )
                else:
                    candidates = list(adapter.search(
                        keyword, limit, content_type=active_content_types[keyword],
                        cookie_source=self.config.youtube_cookie_source,
                        browser_profile=self.config.youtube_browser_profile,
                        cookie_file=self.config.youtube_cookie_file,
                    ))
            except (YouTubeError, YouTubeBrowserSearchError) as exc:
                label = "浏览器辅助 Shorts 搜索" if active_search_sources[keyword] == "browser" else "YouTube 关键词搜索"
                self.on_log(f"{label}失败，继续其他关键词：{keyword} - {exc}")
                result_counts[keyword] = 0
                source_exhausted[keyword] = True
                return 0
            result_counts[keyword] = len(candidates)
            if len(candidates) < limit:
                source_exhausted[keyword] = True
            pool = pools.setdefault(keyword, [])
            new_candidates: list[ImageCandidate] = []
            for candidate in candidates:
                keys = self._current_candidate_keys(candidate)
                # Missing identity is retained for the normal candidate path,
                # where it is skipped without ever entering a seen set.
                if keys and any(key in search_seen_keys_by_keyword[keyword] for key in keys):
                    continue
                new_global_keys = keys - search_seen_keys
                if new_global_keys and len(search_seen_keys) >= self.MAX_YOUTUBE_SEARCH_CANDIDATES:
                    reached_candidate_cap = True
                    break
                if keys:
                    search_seen_keys_by_keyword[keyword].update(keys)
                    search_seen_keys.update(keys)
                new_candidates.append(candidate)
            pool.extend(new_candidates)
            if expanding:
                prefix = "浏览器辅助 Shorts 搜索" if active_search_sources[keyword] == "browser" else "搜索"
                self.on_log(f"{prefix}候选扩展至 {limit}，新增 {len(new_candidates)} 个唯一视频（{keyword}）")
            else:
                if active_search_sources[keyword] == "browser":
                    self.on_log(f"YouTube Shorts 浏览器辅助搜索发现 {len(new_candidates)} 个候选（{keyword}）")
                elif active_content_types[keyword] == "shorts":
                    self.on_log(f"YouTube Shorts 精确搜索发现 {len(new_candidates)} 个候选（{keyword}）")
                elif active_content_types[keyword] == "video":
                    self.on_log(f"YouTube 普通视频精确搜索发现 {len(new_candidates)} 个候选（{keyword}）")
                else:
                    self.on_log(f"YouTube 搜索发现 {len(new_candidates)} 个候选（{keyword}）")
            if expanding and not new_candidates:
                # A larger prefix that contributes no new identity cannot
                # make this source useful again.  Let a type-filtered source
                # fall through to generic ytsearch, rather than repeatedly
                # requesting the same prefix through the global cap.
                source_exhausted[keyword] = True
            for index, candidate in enumerate(new_candidates[:20], start=1):
                keys = self._current_candidate_keys(candidate)
                dedupe_key = next(iter(keys))[1] if keys else "<missing>"
                logger.debug(
                    "YouTube search candidate [%d] id=%s title=%s url=%s dedupe=%s",
                    index, candidate.post_id, candidate.post_text, candidate.post_url or candidate.media_url, dedupe_key,
                )
            return len(new_candidates)

        try:
            for keyword in keywords:
                if self.stop_event.is_set():
                    break
                source_type = active_content_types[keyword]
                source = {
                    "all": "通用 ytsearchN",
                    "shorts": "Shorts 精确搜索 URL",
                    "video": "普通视频精确搜索 URL",
                }[source_type] if active_search_sources[keyword] == "ytdlp" else "浏览器辅助 Shorts 搜索"
                content_label = {"all": "全部", "shorts": "Shorts", "video": "普通视频"}[source_type]
                strict_label = "开启" if self.config.youtube_search_strict_shorts else "关闭"
                self.on_log(
                    f"YouTube 搜索配置：内容类型={content_label}；严格 Shorts={strict_label}；"
                    f"搜索源={source}"
                )
                self._emit(f"YouTube 关键词搜索：{keyword}（首批 {initial_budget} 个候选）")
                fetch(keyword, initial_budget, expanding=False)

            positions = {keyword: 0 for keyword in keywords}
            while not self.stop_event.is_set() and self.stats.downloaded < self.config.target_count:
                made_progress = False
                for keyword in keywords:
                    if self.stop_event.is_set() or self.stats.downloaded >= self.config.target_count:
                        break
                    self._wait_if_paused()
                    if self.stop_event.is_set():
                        break
                    candidates = pools.get(keyword, [])
                    position = positions[keyword]
                    if position >= len(candidates):
                        continue
                    candidate = candidates[position]
                    positions[keyword] += 1
                    made_progress = True
                    keys = self._current_candidate_keys(candidate)
                    if not keys:
                        self.stats.skipped += 1
                        self.on_log(f"YouTube 搜索候选缺少可识别 video_id，已跳过：{candidate.post_url or candidate.media_url}")
                        self._emit()
                        continue
                    if any(key in self._current_task_candidate_keys for key in keys):
                        self.stats.skipped += 1
                        self._record_duplicate("已跳过当前任务重复 YouTube 搜索视频")
                        self._emit()
                        continue
                    self._current_task_candidate_keys.update(keys)
                    self.stats.discovered += 1
                    self._emit()
                    if self.config.skip_history_duplicates and self.database.find_duplicate(candidate, self.config.canonical_source_value, require_local_file=False):
                        self.stats.skipped += 1
                        self._record_duplicate("已跳过历史重复 YouTube 搜索视频")
                        self._emit()
                        continue
                    try:
                        candidate = self._enrich_youtube_search_candidate(adapter, candidate)
                    except YouTubeError as exc:
                        self.stats.skipped += 1
                        self.on_log(f"YouTube 搜索候选 metadata 读取失败，已跳过：{candidate.post_id} - {exc}")
                        self._emit()
                        continue
                    filter_reason = self._youtube_search_filter_reason(candidate)
                    if filter_reason:
                        self.stats.skipped += 1
                        self.on_log(f"YouTube 搜索候选{filter_reason}，已跳过：{candidate.post_id}")
                        self._emit()
                        continue
                    self._emit(f"正在下载：[{self.stats.downloaded + 1}] {candidate.post_text or candidate.post_id}")
                    try:
                        self._download_youtube_candidate(adapter, candidate, store, known)
                    except YouTubeCancelled:
                        break
                if made_progress:
                    continue
                if reached_candidate_cap or len(search_seen_keys) >= self.MAX_YOUTUBE_SEARCH_CANDIDATES:
                    reached_candidate_cap = True
                    break
                # Once a type-filtered source is genuinely exhausted, use
                # generic ytsearch as a second discovery source.  It shares
                # search_seen_keys with the first source, while the existing
                # metadata media_type check remains the safety gate.
                switched = 0
                for keyword in keywords:
                    if self.stop_event.is_set() or self.stats.downloaded >= self.config.target_count:
                        break
                    if (
                        source_exhausted[keyword]
                        and active_content_types[keyword] != "all"
                        and keyword not in fallback_started
                        and not self.config.youtube_search_strict_shorts
                    ):
                        kind = "Shorts" if active_content_types[keyword] == "shorts" else "普通视频"
                        self.on_log(f"{kind} 精确搜索候选已耗尽，正在使用通用 YouTube 搜索继续补充{kind}...（{keyword}）")
                        fallback_started.add(keyword)
                        active_search_sources[keyword] = "ytdlp"
                        active_content_types[keyword] = "all"
                        source_exhausted[keyword] = False
                        requested[keyword] = initial_budget
                        result_counts[keyword] = 0
                        switched += fetch(keyword, requested[keyword], expanding=False)
                if switched:
                    continue

                expandable = [
                    keyword for keyword in keywords
                    if not source_exhausted[keyword]
                    and result_counts.get(keyword, 0) >= requested[keyword]
                    and requested[keyword] < self.MAX_YOUTUBE_SEARCH_CANDIDATES
                ]
                if not expandable:
                    break
                self.on_log("当前候选不足，继续获取更多 YouTube 搜索结果...")
                added = 0
                for keyword in expandable:
                    if self.stop_event.is_set() or len(search_seen_keys) >= self.MAX_YOUTUBE_SEARCH_CANDIDATES:
                        reached_candidate_cap = len(search_seen_keys) >= self.MAX_YOUTUBE_SEARCH_CANDIDATES
                        break
                    requested[keyword] = min(
                        self.MAX_YOUTUBE_SEARCH_CANDIDATES,
                        requested[keyword] + self.YOUTUBE_SEARCH_EXPANSION_STEP,
                    )
                    added_now = fetch(keyword, requested[keyword], expanding=True)
                    added += added_now
                if not added:
                    # fetch() marks a source exhausted when an expanded
                    # prefix contains no new identity.  Loop once more so a
                    # filtered source can activate its generic fallback.
                    continue
            status = "已停止" if self.stop_event.is_set() else (
                "任务完成" if self.stats.downloaded >= self.config.target_count
                else "采集结束（YouTube Shorts 精确搜索候选不足，已达到安全上限）"
                if self.config.youtube_search_strict_shorts and reached_candidate_cap
                else "采集结束（YouTube Shorts 精确搜索候选不足）"
                if self.config.youtube_search_strict_shorts
                else "采集结束（YouTube 搜索候选已达到安全上限，未找到足够符合条件的视频）"
                if reached_candidate_cap
                else "采集结束（YouTube 搜索结果已耗尽）"
            )
            store.complete(self.stats, self.stop_event.is_set())
            self.database.finish_task(self.config, self.stats, "stopped" if self.stop_event.is_set() else "completed", task_output_dir=self.task_output_dir)
            self._emit(status)
            return self.task_dir
        except Exception as exc:
            store.complete(self.stats, self.stop_event.is_set(), str(exc))
            self.database.finish_task(self.config, self.stats, "stopped" if self.stop_event.is_set() else "failed", str(exc), self.task_output_dir)
            if self.stop_event.is_set():
                self._emit("已停止")
                return self.task_dir
            raise
        finally:
            if browser_search is not None:
                browser_search.close()

    def _enrich_youtube_search_candidate(self, adapter: YouTubeAdapter, candidate: ImageCandidate) -> ImageCandidate:
        needs_metadata = (
            self.config.youtube_search_content_type != "all"
            or self.config.youtube_search_max_duration > 0
            or self.config.youtube_search_date_range != "any"
        )
        if not needs_metadata:
            return candidate
        info = adapter.metadata(
            candidate, cookie_source=self.config.youtube_cookie_source,
            browser_profile=self.config.youtube_browser_profile,
            cookie_file=self.config.youtube_cookie_file,
        )
        upload_date = str(info.get("upload_date") or candidate.posted_at or "")
        if not upload_date and info.get("timestamp"):
            upload_date = datetime.fromtimestamp(float(info["timestamp"]), tz=timezone.utc).strftime("%Y%m%d")
        return replace(
            candidate,
            duration=info.get("duration") if info.get("duration") is not None else candidate.duration,
            posted_at=upload_date,
            post_text=str(info.get("title") or candidate.post_text),
            thumbnail_url=str(info.get("thumbnail") or candidate.thumbnail_url),
            content_type=str(info.get("media_type") or candidate.content_type or ""),
        )

    def _youtube_search_filter_reason(self, candidate: ImageCandidate) -> str:
        requested_type = self.config.youtube_search_content_type
        is_short = candidate.content_type == "short"
        if requested_type == "shorts" and not is_short:
            return "类型不符合（普通视频）" if candidate.content_type == "video" else "类型无法确认"
        if requested_type == "video" and is_short:
            return "类型不符合（Shorts）"
        if requested_type == "video" and candidate.content_type != "video":
            return "类型无法确认"
        max_duration = self.config.youtube_search_max_duration
        if max_duration and candidate.duration is not None and candidate.duration > max_duration:
            return "时长不符合"
        date_range = self.config.youtube_search_date_range
        if date_range == "any" or not candidate.posted_at:
            return ""
        try:
            uploaded = datetime.strptime(candidate.posted_at[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return ""
        days = {"1d": 1, "7d": 7, "30d": 30, "365d": 365}[date_range]
        return "" if uploaded >= datetime.now(timezone.utc) - timedelta(days=days) else "发布时间不符合"

    def _download_youtube_candidate(self, adapter: YouTubeAdapter, candidate: ImageCandidate, store: TaskStore, known: set[str]) -> None:
        assert self.task_dir
        record = DownloadRecord(
            "", self.config.source_identifier, candidate.post_url, candidate.media_url,
            collection_mode=self.config.canonical_collection_mode(), source_page_url=self.config.source_page_url,
            platform="youtube", author_username=candidate.author_username, post_url=candidate.post_url,
            media_type=self.config.canonical_media_type(), media_url=candidate.media_url,
            duration=candidate.duration, video_width=candidate.video_width, video_height=candidate.video_height,
            post_id=candidate.post_id, post_text=candidate.post_text, thumbnail_url=candidate.thumbnail_url,
            source=candidate.source, source_type=candidate.source_type, collected_at=datetime.now().isoformat(timespec="seconds"),
        )
        try:
            media_type = self.config.canonical_media_type()
            output_dir = self.task_dir / ("images" if media_type == "thumbnail" else "videos")
            result = adapter.download(
                candidate, output_dir, self.stats.downloaded + 1, media_type=media_type,
                quality=self.config.video_quality, audio_format=self.config.audio_format,
                should_stop=self.stop_event.is_set, should_pause=self._wait_if_paused, on_log=self.on_log,
                cookie_source=self.config.youtube_cookie_source,
                browser_profile=self.config.youtube_browser_profile, cookie_file=self.config.youtube_cookie_file,
            )
            if self.stop_event.is_set():
                raise YouTubeCancelled("用户已停止任务")
            digest = adapter.file_hash(result.path)
            if digest in known or digest in self._current_task_hashes or (
                self.config.skip_history_duplicates and self.database.find_hash_duplicate(digest, result.path, require_local_file=False)
            ):
                result.path.unlink(missing_ok=True)
                self.stats.skipped += 1
                self._record_duplicate("已跳过内容重复 YouTube 视频")
                record.status, record.reason, record.content_hash = "skipped", "内容重复", digest
                store.append(record)
                return
            relative = result.path.relative_to(self.task_dir).as_posix()
            info = result.info
            record.filename, record.file_size, record.image_format = relative, result.path.stat().st_size, result.path.suffix.lstrip(".").upper()
            record.content_hash, record.status = digest, "downloaded"
            record.downloaded_at = datetime.now().isoformat(timespec="seconds")
            record.duration = info.get("duration") or record.duration
            record.video_width = info.get("width") or record.video_width
            record.video_height = info.get("height") or record.video_height
            record.bitrate = info.get("tbr") or record.bitrate
            record.thumbnail_url = info.get("thumbnail") or record.thumbnail_url
            store.append(record)
            known.add(digest); self._current_task_hashes.add(digest); self.stats.downloaded += 1
            self.database.register_media(candidate, self.config.canonical_source_value, digest, result.path, self.config.task_id)
            self.on_log(f"已保存 {relative}")
            self.on_log(f"下载统计：成功保存 {self.stats.downloaded}/{self.config.target_count}")
            self._emit()
            self._sleep_interval()
        except YouTubeCancelled:
            raise
        except (YouTubeError, OSError, ValueError) as exc:
            self.stats.failed += 1
            record.status, record.reason = "failed", str(exc)
            store.append(record)
            self.on_log(f"YouTube 下载失败，跳过后续继续：{exc}")
            self._emit()

    def _on_found(self, count: int) -> None:
        self.stats.discovered = count; self._emit()

    def _process_candidate(self, client: httpx.Client, candidate: ImageCandidate, store: TaskStore, known: set[str]) -> None:
        if self.stop_event.is_set() or self.stats.downloaded >= self.config.target_count:
            return
        self._wait_if_paused()
        if self.stop_event.is_set():
            return
        current_keys = self._current_candidate_keys(candidate)
        if current_keys and any(key in self._current_task_candidate_keys for key in current_keys):
            self._record_duplicate("已跳过当前任务重复素材")
            self._emit()
            return
        self._current_task_candidate_keys.update(current_keys)
        duplicate = None
        if self.config.skip_history_duplicates:
            duplicate = self.database.find_duplicate(
                candidate,
                self.config.canonical_source_value,
                require_local_file=False,
            )
        if duplicate:
            self._record_duplicate("已跳过历史重复素材")
            self._emit()
            return
        self._emit("正在下载视频" if candidate.media_type == "video" else "正在下载图片")
        self._download_one(client, candidate, store, known)
        self._emit()
        self._sleep_interval()

    def _should_skip_discovered_candidate(self, candidate: ImageCandidate) -> bool:
        """Apply the existing Phase 4 identity check before Media detail-page work."""
        if not self.config.skip_history_duplicates:
            return False
        duplicate = self.database.find_duplicate(
            candidate,
            self.config.canonical_source_value,
            require_local_file=False,
        )
        if not duplicate:
            return False
        self._record_duplicate("已跳过历史重复素材")
        self._emit()
        return True

    def _record_duplicate(self, message: str) -> None:
        self.stats.duplicate_count += 1
        if self.stats.duplicate_count == 1 or self.stats.duplicate_count % 10 == 0:
            self.on_log(f"{message}：{self.stats.duplicate_count} 个")

    def _current_candidate_keys(self, candidate: ImageCandidate) -> set[tuple[str, str]]:
        platform = self.config.canonical_platform()
        if platform == "youtube":
            # Stripping a watch URL query makes every /watch?v=... URL become
            # the same /watch path. YouTube task de-duplication must use only
            # the canonical video id, never that query-free URL.
            raw_identity = str(getattr(candidate, "media_identity", "") or getattr(candidate, "post_id", "") or "")
            video_id = youtube_video_id(f"https://youtu.be/{raw_identity}") if raw_identity else ""
            if not video_id:
                for value in (
                    getattr(candidate, "post_url", ""),
                    getattr(candidate, "media_url", ""),
                    getattr(candidate, "image_url", ""),
                ):
                    video_id = youtube_video_id(str(value or ""))
                    if video_id:
                        break
            return {("identity", f"youtube|{video_id}")} if video_id else set()
        keys: set[tuple[str, str]] = set()
        identity = str(getattr(candidate, "media_identity", "") or "")
        if identity:
            keys.add(("identity", f"{platform}|{identity}"))
        urls = [
            str(getattr(candidate, "media_url", "") or ""),
            str(getattr(candidate, "image_url", "") or ""),
            *(str(url or "") for url in getattr(candidate, "fallback_urls", ()) or ()),
        ]
        for url in urls:
            if not url:
                continue
            parsed = urlsplit(url)
            normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            keys.add(("url", f"{platform}|{normalized}"))
        return keys

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
        self.diagnostic_stage = "file write" if self.task_dir else "download dispatch"
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
            shortcode=candidate.shortcode,
            caption=candidate.caption,
            media_index=candidate.media_index,
            thumbnail_url=candidate.thumbnail_url,
            metadata_source=candidate.metadata_source,
            source=candidate.source,
            hashtag=candidate.hashtag or self.config.hashtag,
            source_hashtag=candidate.source_hashtag or self.config.hashtag,
            source_type=candidate.source_type or self.config.source_type or self.config.canonical_collection_mode(),
            source_username=candidate.source_username or self.config.username,
            source_board=candidate.source_board,
            source_board_url=candidate.source_board_url or self.config.board_url,
            source_owner=candidate.source_owner,
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
                        referer = {
                            "x": "https://x.com/",
                            "instagram": "https://www.instagram.com/",
                        }.get(platform, "https://www.pinterest.com/")
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
            historical_duplicate = (
                self.config.skip_history_duplicates
                and self.database.find_hash_duplicate(digest, require_local_file=False) is not None
            )
            if digest in known or digest in self._current_task_hashes or historical_duplicate:
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
            store.append(record); known.add(digest); self._current_task_hashes.add(digest); self.stats.downloaded += 1
            self.database.register_media(candidate, self.config.canonical_source_value, digest, self.task_dir / "images" / filename, self.config.task_id)
            self.on_log(f"已保存 {filename}（{width}×{height}）")
        except (HLSDownloadCancelled, DASHDownloadCancelled):
            return
        except (httpx.HTTPError, HLSDownloadError, DASHDownloadError, UnidentifiedImageError, OSError, ValueError) as exc:
            self.stats.failed += 1; record.status, record.reason = "failed", str(exc); store.append(record)
            suffix = "；当前视频已跳过，将继续处理后续候选。" if candidate.media_type == "video" else ""
            log_exception(exc, config=self.config, context={"stage": self.diagnostic_stage})
            if isinstance(exc, FileNotFoundError):
                self.on_log(
                    f"[diag] FileNotFoundError stage={self.diagnostic_stage} errno={exc.errno} "
                    f"strerror={exc.strerror} filename={exc.filename!r} filename2={getattr(exc, 'filename2', None)!r} "
                    f"cwd={os.getcwd()} program={sys.executable} resource_root={getattr(sys, '_MEIPASS', '')!r} "
                    f"task_id={self.config.task_id} task_dir={self.task_dir}\n{traceback.format_exc()}"
                )
            self.on_log(f"下载失败：{media_url} - {exc}{suffix}")

    def _download_video(self, client: httpx.Client, candidate: ImageCandidate, record: DownloadRecord, store: TaskStore, known: set[str]) -> None:
        self.diagnostic_stage = "downloader selection"
        assert self.task_dir
        videos_dir = self.task_dir / "videos"
        videos_dir.mkdir(exist_ok=True)
        basename = f"{self.stats.downloaded + 1:04d}.mp4"
        final_path = videos_dir / basename
        part_path = videos_dir / f".{basename}.part.mp4"
        sources = list(candidate.video_sources)
        if not sources:
            media_url = candidate.media_url or candidate.image_url
            sources = [PinterestVideoSource(media_url, candidate.video_width, candidate.video_height, candidate.duration, candidate.bitrate, "hls" if ".m3u8" in media_url.lower() else "mp4")]
            sources.extend(
                PinterestVideoSource(url, candidate.video_width, candidate.video_height, candidate.duration, candidate.bitrate, "hls" if ".m3u8" in url.lower() else "mp4")
                for url in candidate.fallback_urls
                if url != media_url
            )
        result = None
        selected_source = None
        last_error: Exception | None = None
        try:
            platform = self.config.canonical_platform()
            stage_label = {
                "x": "Media视频阶段",
                "instagram": "Instagram视频阶段",
            }.get(platform, "Pinterest视频阶段")
            referer = {
                "x": "https://x.com/",
                "instagram": "https://www.instagram.com/",
            }.get(platform, "https://www.pinterest.com/")
            for source in sources:
                if self.stop_event.is_set():
                    raise HLSDownloadCancelled()
                try:
                    self.on_log(f"{stage_label}=downloading_{source.kind}")
                    if source.kind == "hls":
                        self.diagnostic_stage = "external process startup: ffmpeg/hls"
                        hls_downloader = HLSDownloader(
                            client,
                            {"Referer": referer, "User-Agent": "Mozilla/5.0"},
                            self.config.retry_count,
                            self.stop_event.is_set,
                            self._wait_if_paused,
                        )
                        result = hls_downloader.download(source.url, part_path)
                        if platform == "pinterest":
                            result = hls_downloader.validate_output(part_path, result)
                            self.on_log(f"{stage_label}=integrity_pass")
                    elif source.kind == "dash":
                        self.diagnostic_stage = "external process startup: ffmpeg/dash"
                        result = DASHDownloader(
                            client,
                            {"Referer": referer, "User-Agent": "Mozilla/5.0"},
                            self.config.retry_count,
                            self.stop_event.is_set,
                            self._wait_if_paused,
                        ).download(
                            source.url,
                            part_path,
                            audio_url=source.audio_url,
                            width=source.width,
                            height=source.height,
                            bitrate=source.bitrate,
                            codec=source.codec,
                            duration=source.duration,
                        )
                    else:
                        result = self._download_direct_video(client, source.url, part_path, source, referer)
                    selected_source = source
                    break
                except (HLSDownloadCancelled, DASHDownloadCancelled):
                    raise
                except (HLSDownloadError, DASHDownloadError, httpx.HTTPError, OSError) as exc:
                    last_error = exc
                    if part_path.exists():
                        part_path.unlink()
                    self.on_log(f"{stage_label}候选失败，将继续后续来源：{self._safe_media_url(source.url)} - {exc}")
            if result is None or selected_source is None:
                raise last_error or HLSDownloadError("Pinterest 视频没有可用下载来源。")
            if self.stop_event.is_set():
                raise HLSDownloadCancelled()
            digest = self._file_hash(part_path)
            historical_duplicate = (
                self.config.skip_history_duplicates
                and self.database.find_hash_duplicate(
                    digest,
                    part_path,
                    require_local_file=False,
                ) is not None
            )
            if digest in known or digest in self._current_task_hashes or historical_duplicate:
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
            record.bitrate = result.bitrate or selected_source.bitrate
            store.append(record)
            known.add(digest)
            self._current_task_hashes.add(digest)
            self.stats.downloaded += 1
            self.database.register_media(candidate, self.config.canonical_source_value, digest, final_path, self.config.task_id)
            self.on_log(f"已保存 {record.filename}（视频）")
            self.on_log(f"{stage_label}=mp4_saved")
        except (HLSDownloadCancelled, DASHDownloadCancelled):
            self.on_log(f"{stage_label}=failed_stage=stopped")
            raise
        except (HLSDownloadError, DASHDownloadError) as exc:
            self.on_log(f"{stage_label}=failed_stage=video_download_or_ffmpeg，{exc}")
            raise
        finally:
            if part_path.exists():
                part_path.unlink()

    def _download_direct_video(self, client: httpx.Client, url: str, output_path: Path, source: PinterestVideoSource, referer: str):
        request_url = self._direct_video_request_url(url)
        with client.stream("GET", request_url, headers={"Referer": referer, "User-Agent": "Mozilla/5.0"}) as response:
            response.raise_for_status()
            with output_path.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if self.stop_event.is_set():
                        raise HLSDownloadCancelled()
                    self._wait_if_paused()
                    if self.stop_event.is_set():
                        raise HLSDownloadCancelled()
                    handle.write(chunk)
        if not output_path.exists() or output_path.stat().st_size < 1024:
            raise HLSDownloadError("Pinterest MP4 文件为空或不完整。")
        from src.downloader.hls import HLSResult
        return HLSResult(source.duration, source.width, source.height, source.bitrate)

    @staticmethod
    def _direct_video_request_url(url: str) -> str:
        """Remove only transport range controls; preserve signed CDN query data."""
        parsed = urlsplit(url)
        query_parts = [
            part for part in parsed.query.split("&")
            if part and part.split("=", 1)[0].casefold() not in {"bytestart", "byteend"}
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(query_parts), parsed.fragment))

    @staticmethod
    def _safe_media_url(url: str) -> str:
        parsed = urlsplit(url)
        safe = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return safe[:180] + ("…" if len(safe) > 180 else "")

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
