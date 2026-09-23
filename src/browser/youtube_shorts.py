from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

from playwright.sync_api import Error as PlaywrightError, Page

from src.browser.collector import ImageCandidate
from src.browser.session import BrowserSession
from src.utils.youtube import youtube_video_id


class YouTubeBrowserSearchError(RuntimeError):
    """The browser search page could not provide usable Shorts candidates."""


@dataclass
class _ShortsSearchState:
    page: Page
    source_url: str
    candidates: dict[str, ImageCandidate] = field(default_factory=dict)
    exhausted: bool = False
    empty_rounds: int = 0


class YouTubeShortsBrowserCollector(BrowserSession):
    """Discover public Shorts links from YouTube's rendered search page.

    This collector intentionally does not inspect player/network responses and
    does not download media.  The normal YouTubeAdapter remains responsible
    for metadata and downloading after Engine receives these candidates.
    """

    MAX_CANDIDATES = 150
    EMPTY_ROUND_LIMIT = 10

    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        super().__init__(profile_dir, log)
        self._states: dict[str, _ShortsSearchState] = {}

    @staticmethod
    def search_url(keyword: str) -> str:
        return f"https://www.youtube.com/results?search_query={quote_plus(keyword)}&sp=EgIQCQ%3D%3D"

    def search(
        self,
        keyword: str,
        limit: int,
        *,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        max_scrolls: int = 100,
    ) -> list[ImageCandidate]:
        if limit < 1:
            return []
        state = self._states.get(keyword)
        try:
            if state is None:
                if not self._context:
                    raise YouTubeBrowserSearchError("浏览器辅助 Shorts 搜索未初始化浏览器上下文。")
                page = self._page if not self._states and self._page else self._context.new_page()
                source_url = self.search_url(keyword)
                self.log(f"正在打开 YouTube Shorts 精确搜索：{source_url}")
                page.goto(source_url, wait_until="domcontentloaded", timeout=60000)
                state = _ShortsSearchState(page=page, source_url=source_url)
                self._states[keyword] = state
                page.wait_for_timeout(1800)

            requested = min(limit, self.MAX_CANDIDATES)
            scrolls = 0
            while (
                len(state.candidates) < requested
                and not state.exhausted
                and not should_stop()
                and scrolls < max_scrolls
            ):
                should_pause()
                if should_stop():
                    break
                before_count = len(state.candidates)
                before_height = self._scroll_height(state.page)
                self._collect_visible(state)
                if len(state.candidates) >= requested:
                    break
                self._scroll_down(state.page)
                state.page.wait_for_timeout(1500)
                self._collect_visible(state)
                after_height = self._scroll_height(state.page)
                scrolls += 1
                if len(state.candidates) > before_count or after_height > before_height:
                    state.empty_rounds = 0
                    continue
                state.empty_rounds += 1
                if state.empty_rounds < self.EMPTY_ROUND_LIMIT:
                    self.log(f"YouTube Shorts 浏览器搜索连续 {state.empty_rounds} 轮未发现新链接，继续加载...")
                    continue
                self.log("YouTube Shorts 浏览器搜索长时间未发现新链接，正在执行恢复滚动...")
                if not self._recovery(state):
                    state.exhausted = True
                    self.log("YouTube Shorts 浏览器搜索经过多轮滚动和恢复后未发现更多候选。")

            self._collect_visible(state)
        except PlaywrightError as exc:
            raise YouTubeBrowserSearchError(f"浏览器辅助 Shorts 搜索失败：{exc}") from exc

        if not state.candidates and not should_stop():
            raise YouTubeBrowserSearchError("浏览器辅助 Shorts 搜索未发现结果；请检查网络、YouTube 页面验证或稍后重试。")
        return list(state.candidates.values())[:requested]

    def _collect_visible(self, state: _ShortsSearchState) -> int:
        raw_links = state.page.locator('a[href*="/shorts/"]').evaluate_all(
            """els => els.map(a => ({href: a.href || a.getAttribute('href') || '',
                                      title: (a.getAttribute('title') || a.getAttribute('aria-label') || a.textContent || '').trim()}))"""
        )
        added = 0
        for item in raw_links:
            if not isinstance(item, dict):
                continue
            href = str(item.get("href") or "")
            video_id = youtube_video_id(href)
            if not video_id or video_id in state.candidates:
                continue
            title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
            canonical = f"https://www.youtube.com/watch?v={video_id}"
            state.candidates[video_id] = ImageCandidate(
                image_url="", pin_url=canonical, source_page_url=state.source_url,
                post_url=canonical, platform="youtube", media_type="video", media_url=canonical,
                media_identity=video_id, post_id=video_id, post_text=title,
                source_type="youtube_browser_shorts", source=state.source_url,
            )
            added += 1
            if len(state.candidates) >= self.MAX_CANDIDATES:
                state.exhausted = True
                break
        return added

    @staticmethod
    def _scroll_height(page: Page) -> int:
        value = page.evaluate("() => Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)")
        return int(value or 0)

    @staticmethod
    def _scroll_down(page: Page) -> None:
        page.evaluate("() => window.scrollBy(0, Math.max(window.innerHeight * 1.8, 1200))")

    def _recovery(self, state: _ShortsSearchState) -> bool:
        page = state.page
        before_count = len(state.candidates)
        before_height = self._scroll_height(page)
        page.evaluate("() => window.scrollBy(0, -Math.max(window.innerHeight, 900))")
        page.wait_for_timeout(1000)
        self._scroll_down(page)
        self._scroll_down(page)
        page.wait_for_timeout(2000)
        self._collect_visible(state)
        after_height = self._scroll_height(page)
        if len(state.candidates) > before_count or after_height > before_height:
            state.empty_rounds = 0
            return True
        return False
