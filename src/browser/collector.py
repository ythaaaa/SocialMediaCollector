from __future__ import annotations

import base64
import json
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
    fallback_urls: tuple[str, ...] = ()
    video_sources: tuple["PinterestVideoSource", ...] = ()
    width: int | None = None
    height: int | None = None
    shortcode: str = ""
    caption: str = ""
    media_index: int = 0
    thumbnail_url: str = ""
    metadata_source: str = ""
    source: str = ""


@dataclass(frozen=True)
class PinterestVideoSource:
    url: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    bitrate: int | None = None
    kind: str = "mp4"
    representation_id: str = ""
    codec: str = ""
    mime_type: str = ""
    frame_rate: str = ""
    sample_rate: int | None = None
    channels: int | None = None
    audio_url: str = ""
    audio_codec: str = ""
    audio_bitrate: int | None = None
    container: str = ""


class PinterestCollector(BrowserSession):
    """Collects image resource URLs visible in Pinterest's public search UI."""
    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        super().__init__(profile_dir, log)
        self._video_pins: dict[str, dict] = {}
        self._video_listener_pages: set[int] = set()

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
        media_type: str = "image",
    ) -> list[ImageCandidate]:
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        page = self._page
        self._reset_video_state(page)
        self._attach_video_response_listener(page)
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
            require_pin=True,
            media_type=media_type,
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
        media_type: str = "image",
    ) -> list[ImageCandidate]:
        """Collect image Pins from the logged-in Pinterest home feed."""
        if not self._page:
            raise RuntimeError("browser is not initialized")
        page = self._page
        self._reset_video_state(page)
        self._attach_video_response_listener(page)
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
            media_type=media_type,
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
        media_type: str = "image",
    ) -> list[ImageCandidate]:
        """Collect from the user's current Pinterest tab without navigating it."""
        if not self._context:
            raise RuntimeError("请先点击『打开浏览器 / 登录』，在 Pinterest 中打开需要采集的页面，然后开始下载。")

        page = self.prepare_current_page()
        if page is None:
            raise RuntimeError("请先在托管浏览器中打开需要采集的 Pinterest 页面，然后开始下载。")
        source_url = page.url
        self._reset_video_state(page)
        self._attach_video_response_listener(page)
        self.log(f"当前页面：{source_url}")
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
            media_type=media_type,
            empty_message="当前 Pinterest 页面没有检测到可采集图片。",
        )

    def prepare_current_page(self) -> Page | None:
        """Select the active Pinterest tab without opening or navigating a page."""
        if not self._context:
            return None
        try:
            page = self._select_pinterest_page()
        except Exception:
            self._context = None
            self._page = None
            return None
        if page is None or not self._is_pinterest_page(page):
            return None
        self._page = page
        return page

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
        media_type: str,
        empty_message: str,
    ) -> list[ImageCandidate]:
        candidates: dict[str, ImageCandidate] = {}
        finish = should_finish or (lambda: False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not should_stop():
            should_pause()
            self._collect_visible(page, candidates, require_pin=require_pin, on_candidate=on_candidate, media_type=media_type)
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
            self._collect_visible(page, candidates, require_pin=require_pin, on_candidate=on_candidate, media_type=media_type)
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
        media_type: str = "image",
    ) -> None:
        video_pin_ids = self._collect_video_candidates(page, candidates, media_type, on_candidate)
        try:
            items = page.locator("img").evaluate_all("""imgs => imgs.map(img => ({src: img.currentSrc || img.src, srcset: img.srcset, parent: img.closest('a')?.href || ''}))""")
        except PlaywrightError as exc:
            if "Execution context was destroyed" in str(exc):
                self.log("页面正在跳转，等待搜索结果重新加载……")
                return
            raise
        for item in items:
            best = self._best_url(item.get("src", ""), item.get("srcset", ""))
            pin_url = urljoin(page.url, item.get("parent", ""))
            pin_id = self._pin_id_from_url(pin_url)
            if pin_id in video_pin_ids:
                continue
            if media_type == "video":
                continue
            if best and "pinimg.com" in best and (not require_pin or "/pin/" in item.get("parent", "")):
                fallback_url = best
                best = self._prefer_original_url(fallback_url)
                candidate = ImageCandidate(
                    best,
                    pin_url,
                    page.url,
                    post_url=pin_url,
                    post_id=pin_id,
                    fallback_urls=(fallback_url,) if best != fallback_url else (),
                )
                if best not in candidates:
                    candidates[best] = candidate
                    if on_candidate:
                        on_candidate(candidate)

    def _reset_video_state(self, page: Page) -> None:
        self._video_pins.clear()
        self._attach_video_response_listener(page)

    def _attach_video_response_listener(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._video_listener_pages or not hasattr(page, "on"):
            return
        self._video_listener_pages.add(page_id)
        page.on("response", self._capture_video_response)

    def _capture_video_response(self, response) -> None:
        url = str(getattr(response, "url", ""))
        if "/resource/" not in url:
            return
        try:
            payload = response.json()
        except Exception:
            return
        self._ingest_video_payload(payload)

    def _collect_video_candidates(
        self,
        page: Page,
        candidates: dict[str, ImageCandidate],
        media_type: str,
        on_candidate: Callable[[ImageCandidate], None] | None,
    ) -> set[str]:
        self._ingest_page_state(page)
        video_pin_ids = set(self._video_pins)
        if media_type not in {"video", "mixed"}:
            return video_pin_ids
        for pin_id, pin in self._video_pins.items():
            sources = self._video_sources(pin.get("videos", {}).get("video_list", {}))
            if not sources:
                continue
            selected = sources[0]
            pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
            identity = f"pinterest-video:{pin_id}:{pin.get('videos', {}).get('id') or pin_id}"
            candidate = ImageCandidate(
                "",
                pin_url,
                page.url,
                post_url=pin_url,
                media_type="video",
                media_url=selected.url,
                media_identity=identity,
                duration=selected.duration,
                video_width=selected.width,
                video_height=selected.height,
                bitrate=selected.bitrate,
                post_id=pin_id,
                fallback_urls=tuple(source.url for source in sources[1:]),
                video_sources=tuple(sources),
            )
            if identity not in candidates:
                candidates[identity] = candidate
                if on_candidate:
                    on_candidate(candidate)
        return video_pin_ids

    def _ingest_page_state(self, page: Page) -> None:
        try:
            scripts = page.locator("script").all_text_contents()
        except PlaywrightError:
            return
        for text in scripts:
            if "video_list" not in text:
                continue
            try:
                self._ingest_video_payload(json.loads(text))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue

    def _ingest_video_payload(self, payload) -> None:
        def walk(value) -> None:
            if isinstance(value, dict):
                videos = value.get("videos")
                video_list = videos.get("video_list") if isinstance(videos, dict) else None
                if isinstance(video_list, dict):
                    pin_id = str(value.get("id") or self._decode_node_id(value.get("node_id")) or "")
                    if pin_id.isdigit():
                        self._video_pins[pin_id] = value
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)

    @staticmethod
    def _decode_node_id(value) -> str:
        if not value:
            return ""
        try:
            decoded = base64.b64decode(str(value)).decode("utf-8")
        except Exception:
            return ""
        return decoded.split(":", 1)[1] if decoded.startswith("Pin:") else ""

    @staticmethod
    def _video_sources(video_list: dict) -> list[PinterestVideoSource]:
        sources: list[PinterestVideoSource] = []
        seen: set[str] = set()
        for name, raw in video_list.items():
            if not isinstance(raw, dict) or not raw.get("url"):
                continue
            url = str(raw["url"])
            if url in seen:
                continue
            seen.add(url)
            duration = raw.get("duration")
            try:
                duration = float(duration) / 1000 if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            kind = "hls" if ".m3u8" in url.lower() else "mp4"
            sources.append(PinterestVideoSource(
                url=url,
                width=int(raw["width"]) if str(raw.get("width", "")).isdigit() else None,
                height=int(raw["height"]) if str(raw.get("height", "")).isdigit() else None,
                duration=duration,
                bitrate=int(raw["bitrate"]) if str(raw.get("bitrate", "")).isdigit() else None,
                kind=kind,
            ))
        return sorted(
            sources,
            key=lambda source: (
                (source.width or 0) * (source.height or 0),
                source.bitrate or 0,
                1 if source.kind == "mp4" else 0,
            ),
            reverse=True,
        )

    @staticmethod
    def _pin_id_from_url(url: str) -> str:
        match = re.search(r"/pin/(\d+)", url or "")
        return match.group(1) if match else ""

    @staticmethod
    def _prefer_original_url(url: str) -> str:
        """Pinterest's public CDN uses a size path; prefer its original variant when available."""
        return re.sub(r"(https?://i\.pinimg\.com/)(?:\d+x\d*|\d+x)/", r"\1originals/", url)

    @staticmethod
    def _best_url(src: str, srcset: str) -> str:
        choices = [(src, 0)]
        for raw in srcset.split(","):
            bits = raw.strip().split()
            if bits:
                number = int(re.sub(r"\D", "", bits[1])) if len(bits) > 1 and re.sub(r"\D", "", bits[1]) else 0
                choices.append((bits[0], number))
        return max(choices, key=lambda pair: pair[1])[0] if choices else ""
