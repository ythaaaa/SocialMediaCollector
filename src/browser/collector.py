from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright


@dataclass(frozen=True)
class ImageCandidate:
    image_url: str
    pin_url: str


class PinterestCollector:
    """Collects image resource URLs visible in Pinterest's public search UI."""
    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        self.profile_dir, self.log = profile_dir, log
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    def open(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        options: dict[str, object] = {"headless": False, "viewport": {"width": 1366, "height": 900}}
        try:
            self._context = self._playwright.chromium.launch_persistent_context(str(self.profile_dir), **options)
        except Exception as exc:
            chrome = self._find_chrome()
            if not chrome:
                raise RuntimeError("未找到 Playwright Chromium，也未找到本机 Chrome；请执行 playwright install chromium") from exc
            self.log(f"Playwright Chromium 不可用，改用本机 Chrome：{chrome}")
            options["executable_path"] = chrome
            self._context = self._playwright.chromium.launch_persistent_context(str(self.profile_dir), **options)
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def collect(self, keyword: str, wanted: int, max_scrolls: int, should_stop: Callable[[], bool], should_pause: Callable[[], None], on_found: Callable[[int], None]) -> list[ImageCandidate]:
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        page = self._page
        page.goto(f"https://www.pinterest.com/search/pins/?q={quote_plus(keyword)}", wait_until="domcontentloaded", timeout=60000)
        self.log("浏览器已打开 Pinterest 搜索页；如出现登录或验证，请在浏览器中完成后点击继续。")
        candidates: dict[str, ImageCandidate] = {}
        self._wait_for_search_results(page, candidates, should_stop, should_pause, on_found)
        if should_stop():
            return []
        if len(candidates) < 3:
            raise RuntimeError("未检测到 Pinterest 搜索结果。请确认已在浏览器中完成登录/验证，或稍后重试。")
        stale_rounds = 0
        for _ in range(max_scrolls):
            if should_stop(): break
            should_pause()
            self._collect_visible(page, candidates)
            on_found(len(candidates))
            if len(candidates) >= wanted * 3: break
            before = len(candidates)
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1100)
            stale_rounds = stale_rounds + 1 if before == len(candidates) else 0
            if stale_rounds >= 8:
                self.log("连续滚动未发现新图片，结束收集。")
                break
        return list(candidates.values())

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

    def _collect_visible(self, page: Page, candidates: dict[str, ImageCandidate]) -> None:
        try:
            items = page.locator("img").evaluate_all("""imgs => imgs.map(img => ({src: img.currentSrc || img.src, srcset: img.srcset, parent: img.closest('a')?.href || ''}))""")
        except PlaywrightError as exc:
            if "Execution context was destroyed" in str(exc):
                self.log("页面正在跳转，等待搜索结果重新加载……")
                return
            raise
        for item in items:
            best = self._best_url(item.get("src", ""), item.get("srcset", ""))
            if best and "pinimg.com" in best:
                best = self._prefer_original_url(best)
                candidates.setdefault(best, ImageCandidate(best, item.get("parent", "")))

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

    def close(self) -> None:
        if self._context: self._context.close()
        if self._playwright: self._playwright.stop()
        self._context = self._page = self._playwright = None

    @staticmethod
    def _find_chrome() -> str | None:
        candidates = [
            shutil.which("chrome"),
            r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        ]
        return next((path for path in candidates if path and Path(path).is_file()), None)
