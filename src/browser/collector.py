from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, Page

from src.browser.session import BrowserSession


@dataclass(frozen=True)
class ImageCandidate:
    image_url: str
    pin_url: str
    source_page_url: str = ""
    post_url: str = ""
    platform: str = "pinterest"
    author_username: str = ""
    is_repost: bool = False
    media_type: str = "image"
    media_url: str = ""
    media_identity: str = ""
    duration: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    bitrate: int | None = None
    post_id: str = ""
    post_text: str = ""
    prompt_detected: bool = False
    prompt: str = ""
    posted_at: str = ""


class PinterestCollector(BrowserSession):
    """Collects image resource URLs visible in Pinterest's public search UI."""
    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        super().__init__(profile_dir, log)

    def collect(
        self,
        keyword: str,
        wanted: int,
        max_scrolls: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
    ) -> list[ImageCandidate]:
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        page = self._page
        page.goto(f"https://www.pinterest.com/search/pins/?q={quote_plus(keyword)}", wait_until="domcontentloaded", timeout=60000)
        self.log("浏览器已打开 Pinterest 搜索页；如出现登录或验证，请在浏览器中完成后点击继续。")
        return self._collect_page(
            page,
            wanted,
            max_scrolls,
            should_stop,
            should_pause,
            on_found,
            on_candidate,
            should_finish,
            require_pin=False,
            empty_message="未检测到 Pinterest 搜索结果。请确认已在浏览器中完成登录/验证，或稍后重试。",
        )

    def collect_home(
        self,
        wanted: int,
        max_scrolls: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
    ) -> list[ImageCandidate]:
        """Collect image Pins from the logged-in Pinterest home feed."""
        if not self._page:
            raise RuntimeError("browser is not initialized")
        page = self._page
        page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=60000)
        self.log("Pinterest 首页已打开；如出现登录或验证，请在浏览器中人工完成。")
        return self._collect_page(
            page,
            wanted,
            max_scrolls,
            should_stop,
            should_pause,
            on_found,
            on_candidate,
            should_finish,
            require_pin=True,
            empty_message="未检测到 Pinterest 首页推荐图片，请确认已登录或稍后重试。",
        )

    def collect_current_page(
        self,
        wanted: int,
        max_scrolls: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
    ) -> list[ImageCandidate]:
        """Collect from the user's current Pinterest tab without navigating it."""
        if not self._context:
            raise RuntimeError("浏览器未初始化")

        page = self._select_pinterest_page()
        if page is None:
            page = self._page or self._context.new_page()
            opened_from_blank = page.url in {"", "about:blank"}
            if opened_from_blank:
                page.goto("https://www.pinterest.com/", wait_until="domcontentloaded", timeout=60000)
            self.log("当前页面模式：请在浏览器中打开目标 Pinterest 页面，程序不会自动切换到搜索页。")
            if opened_from_blank:
                self.log("浏览器已启动，等待你导航到目标页面（最长 8 秒；保持首页即可采集首页内容）。")
                for _ in range(8):
                    if should_stop():
                        return []
                    should_pause()
                    page.wait_for_timeout(1000)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and not should_stop():
                should_pause()
                page = self._select_pinterest_page()
                if page is not None:
                    break
                page.wait_for_timeout(1000)

        if page is None or not self._is_pinterest_page(page):
            raise RuntimeError("当前页面不是 Pinterest 页面，请先在浏览器中打开目标页面。")
        self._page = page
        source_url = page.url
        self.log(f"当前页面采集开始：{source_url}")
        return self._collect_page(
            page,
            wanted,
            max_scrolls,
            should_stop,
            should_pause,
            on_found,
            on_candidate,
            should_finish,
            require_pin=False,
            empty_message="当前 Pinterest 页面没有检测到可采集图片。",
        )

    def _collect_page(
        self,
        page: Page,
        wanted: int,
        max_scrolls: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None,
        should_finish: Callable[[], bool] | None,
        require_pin: bool,
        empty_message: str,
    ) -> list[ImageCandidate]:
        candidates: dict[str, ImageCandidate] = {}
        finish = should_finish or (lambda: False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not should_stop():
            should_pause()
            self._collect_visible(page, candidates, require_pin=require_pin, on_candidate=on_candidate)
            on_found(len(candidates))
            if candidates or finish():
                break
            page.wait_for_timeout(1500)
        if should_stop():
            return list(candidates.values())
        if not candidates and not finish():
            raise RuntimeError(empty_message)

        stale_rounds = 0
        for _ in range(max_scrolls + 1):
            if should_stop() or finish():
                break
            should_pause()
            before = len(candidates)
            self._collect_visible(page, candidates, require_pin=require_pin, on_candidate=on_candidate)
            on_found(len(candidates))
            if finish():
                break
            if len(candidates) == before:
                stale_rounds += 1
            else:
                stale_rounds = 0
            if stale_rounds >= 8:
                self.log("连续滚动未发现新的 Pin，结束收集；可能页面暂无更多可采集内容。")
                break
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1100)
        return list(candidates.values())

    def _select_pinterest_page(self) -> Page | None:
        if not self._context:
            return None
        ranked: list[tuple[int, int, Page]] = []
        for index, page in enumerate(self._context.pages):
            try:
                if bool(page.evaluate("document.hasFocus()")):
                    # A focused non-Pinterest tab is the user's active tab;
                    # do not silently fall back to a Pinterest tab in the
                    # background.
                    return page if self._is_pinterest_page(page) else None
            except PlaywrightError:
                pass
            if not self._is_pinterest_page(page):
                continue
            focused = 0
            try:
                focused = int(bool(page.evaluate("document.hasFocus()")))
            except PlaywrightError:
                pass
            ranked.append((focused, index, page))
        if not ranked:
            return None
        return max(ranked, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def _is_pinterest_page(page: Page) -> bool:
        try:
            parsed = urlparse(page.url)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        if not (host == "pinterest.com" or host.endswith(".pinterest.com")):
            return False
        return "/login" not in parsed.path.lower() and "/auth" not in parsed.path.lower()

    def _wait_for_search_results(self, page: Page, candidates: dict[str, ImageCandidate], should_stop: Callable[[], bool], should_pause: Callable[[], None], on_found: Callable[[int], None]) -> None:
        """Keep the visible browser available for manual sign-in before collection."""
        self.log("正在等待搜索结果加载；如需登录或验证，请在浏览器窗口完成操作（最长等待 120 秒）。")
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not should_stop():
            should_pause()
            self._collect_visible(page, candidates)
            on_found(len(candidates))
            if len(candidates) >= 3:
                return
            page.wait_for_timeout(1500)

    def _collect_visible(
        self,
        page: Page,
        candidates: dict[str, ImageCandidate],
        require_pin: bool = False,
        on_candidate: Callable[[ImageCandidate], None] | None = None,
    ) -> None:
        try:
            items = page.locator("img").evaluate_all("""imgs => imgs.map(img => ({src: img.currentSrc || img.src, srcset: img.srcset, parent: img.closest('a')?.href || ''}))""")
        except PlaywrightError as exc:
            if "Execution context was destroyed" in str(exc):
                self.log("页面正在跳转，等待搜索结果重新加载……")
                return
            raise
        for item in items:
            best = self._best_url(item.get("src", ""), item.get("srcset", ""))
            if best and "pinimg.com" in best and (not require_pin or "/pin/" in item.get("parent", "")):
                best = self._prefer_original_url(best)
                pin_url = urljoin(page.url, item.get("parent", ""))
                candidate = ImageCandidate(best, pin_url, page.url)
                if best not in candidates:
                    candidates[best] = candidate
                    if on_candidate:
                        on_candidate(candidate)

    @staticmethod
    def _prefer_original_url(url: str) -> str:
        """Pinterest's public CDN uses a size path; prefer its original variant when available."""
        return re.sub(r"(https?://i\.pinimg\.com/)(?:\d+x)/", r"\1originals/", url)

    @staticmethod
    def _best_url(src: str, srcset: str) -> str:
        choices = [(src, 0)]
        for raw in srcset.split(","):
            bits = raw.strip().split()
            if bits:
                number = int(re.sub(r"\D", "", bits[1])) if len(bits) > 1 and re.sub(r"\D", "", bits[1]) else 0
                choices.append((bits[0], number))
        return max(choices, key=lambda pair: pair[1])[0] if choices else ""
