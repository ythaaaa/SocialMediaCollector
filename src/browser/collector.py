from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus, urljoin

from playwright.sync_api import BrowserContext, Page, sync_playwright


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
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir), headless=False, viewport={"width": 1366, "height": 900}
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def collect(self, keyword: str, wanted: int, max_scrolls: int, should_stop: Callable[[], bool], should_pause: Callable[[], None], on_found: Callable[[int], None]) -> list[ImageCandidate]:
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        page = self._page
        page.goto(f"https://www.pinterest.com/search/pins/?q={quote_plus(keyword)}", wait_until="domcontentloaded", timeout=60000)
        self.log("浏览器已打开 Pinterest 搜索页；如出现登录或验证，请在浏览器中完成后点击继续。")
        page.wait_for_timeout(2500)
        candidates: dict[str, ImageCandidate] = {}
        stale_rounds = 0
        for _ in range(max_scrolls):
            if should_stop(): break
            should_pause()
            for item in page.locator("img").evaluate_all("""imgs => imgs.map(img => ({src: img.currentSrc || img.src, srcset: img.srcset, parent: img.closest('a')?.href || ''}))"""):
                best = self._best_url(item.get("src", ""), item.get("srcset", ""))
                if best and "pinimg.com" in best:
                    candidates.setdefault(best, ImageCandidate(best, item.get("parent", "")))
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
