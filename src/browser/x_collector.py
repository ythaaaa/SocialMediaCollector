from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Page

from src.browser.collector import ImageCandidate, PinterestCollector
from src.utils.x import normalize_x_author


class XCollector(PinterestCollector):
    """Collect image media from an X author's visible profile timeline."""

    def __init__(self, profile_dir, log) -> None:
        super().__init__(profile_dir, log)
        self._video_playlists: dict[str, list[str]] = defaultdict(list)
        self._video_pages: set[int] = set()

    def collect_author(
        self,
        author_input: str,
        wanted: int,
        max_scrolls: int,
        only_author_content: bool,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
        media_type: str = "image",
    ) -> list[ImageCandidate]:
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        username, profile_url = normalize_x_author(author_input)
        page = self._page
        self._prepare_video_capture(page)
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        self.log(f"X 作者主页已打开：{profile_url}；如需登录，请在浏览器中人工完成。")
        return self._collect_timeline(
            page,
            username,
            profile_url,
            wanted,
            max_scrolls,
            only_author_content,
            should_stop,
            should_pause,
            on_found,
            on_candidate,
            should_finish,
            media_type,
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
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        page = self._page
        self._prepare_video_capture(page)
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        self.log("X 首页‘为你推荐’已打开；如需登录，请在浏览器中人工完成。")
        return self._collect_timeline(
            page,
            "",
            "https://x.com/home",
            wanted,
            max_scrolls,
            False,
            should_stop,
            should_pause,
            on_found,
            on_candidate,
            should_finish,
            media_type,
        )

    def _collect_timeline(
        self,
        page: Page,
        username: str,
        profile_url: str,
        wanted: int,
        max_scrolls: int,
        only_author_content: bool,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None,
        should_finish: Callable[[], bool] | None,
        media_type: str,
    ) -> list[ImageCandidate]:
        candidates: dict[str, ImageCandidate] = {}
        finish = should_finish or (lambda: False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not should_stop():
            should_pause()
            self._collect_visible_tweets(page, username, profile_url, only_author_content, candidates, on_candidate, media_type)
            on_found(len(candidates))
            if candidates or finish():
                break
            page.wait_for_timeout(1500)
        if should_stop():
            return list(candidates.values())
        if not candidates and not finish():
            raise RuntimeError("未检测到该 X 作者的图片内容；请确认主页有效、已登录且作者有公开图片帖子。")

        stale_rounds = 0
        for _ in range(max_scrolls + 1):
            if should_stop() or finish():
                break
            should_pause()
            before = len(candidates)
            self._collect_visible_tweets(page, username, profile_url, only_author_content, candidates, on_candidate, media_type)
            on_found(len(candidates))
            if finish():
                break
            stale_rounds = stale_rounds + 1 if len(candidates) == before else 0
            if stale_rounds >= 8:
                self.log("连续滚动未发现新的 X 图片帖子，结束采集；可能页面暂无更多内容。")
                break
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1200)
        return list(candidates.values())

    def _collect_visible_tweets(
        self,
        page: Page,
        username: str,
        profile_url: str,
        only_author_content: bool,
        candidates: dict[str, ImageCandidate],
        on_candidate: Callable[[ImageCandidate], None] | None,
        media_type: str = "image",
    ) -> None:
        payload = page.locator('article[data-testid="tweet"]').evaluate_all(
            """
            (articles) => articles.map((article) => {
              const statusLinks = Array.from(article.querySelectorAll('a[href*="/status/"]'))
                .map(link => link.href)
                .filter(href => !href.includes('/analytics'));
              const status = statusLinks[0] || '';
              const author = article.querySelector('[data-testid="User-Name"] a[href^="/"]')?.getAttribute('href') || '';
              const authorName = author.split('/').filter(Boolean)[0] || '';
              const social = article.querySelector('[data-testid="socialContext"]')?.textContent || '';
              const lowerSocial = social.toLowerCase();
              const repost = lowerSocial.includes('repost') || lowerSocial.includes('retweeted') || social.includes('转发');
              const videos = Array.from(article.querySelectorAll('video'))
                .filter(video => video.closest('article[data-testid="tweet"]') === article)
                .map(video => ({
                  poster: video.poster || '',
                  duration: Number.isFinite(video.duration) ? video.duration : null,
                  width: video.videoWidth || 0,
                  height: video.videoHeight || 0,
                }));
              const video = videos.length > 0 || Boolean(article.querySelector('[data-testid="videoPlayer"], img[src*="ext_tw_video_thumb"], img[src*="amplify_video_thumb"]'));
              const text = (article.innerText || '').slice(0, 1600);
              const labels = Array.from(article.querySelectorAll('[aria-label], [data-testid]'))
                .map(node => `${node.getAttribute('aria-label') || ''} ${node.getAttribute('data-testid') || ''}`)
                .join(' ');
              const promoted = /promoted|sponsored|advertisement|推广内容|广告内容|赞助内容/i.test(`${text} ${labels}`);
              const media = Array.from(article.querySelectorAll('img[src*="pbs.twimg.com/media/"]'))
                .filter((img) => img.closest('article[data-testid="tweet"]') === article)
                .map((img) => img.currentSrc || img.src)
                .filter(Boolean);
              return {status, authorName, repost, video, videos, promoted, media};
            })
            """
        )
        for item in payload:
            if item.get("promoted"):
                continue
            author_name = str(item.get("authorName") or "")
            if only_author_content and (author_name.lower() != username.lower() or item.get("repost")):
                continue
            post_url = str(item.get("status") or profile_url)
            if media_type in {"image", "mixed"} and (media_type == "mixed" or not item.get("video")):
                for raw_url in item.get("media") or []:
                    image_url = self._prefer_original_url(str(raw_url))
                    if image_url in candidates:
                        continue
                    candidate = ImageCandidate(
                        image_url=image_url,
                        pin_url=post_url,
                        source_page_url=page.url,
                        post_url=post_url,
                        platform="x",
                        author_username=username,
                        is_repost=bool(item.get("repost")),
                        media_type="image",
                        media_url=image_url,
                        media_identity=image_url,
                    )
                    candidates[image_url] = candidate
                    if on_candidate:
                        on_candidate(candidate)
            if media_type in {"video", "mixed"}:
                for video in item.get("videos") or []:
                    playlist = self._playlist_for_video(str(video.get("poster") or ""))
                    if not playlist:
                        continue
                    identity = self._video_identity(str(video.get("poster") or "")) or playlist
                    key = f"video:{identity}"
                    if key in candidates:
                        continue
                    candidate = ImageCandidate(
                        image_url=playlist,
                        pin_url=post_url,
                        source_page_url=page.url,
                        post_url=post_url,
                        platform="x",
                        author_username=username,
                        is_repost=bool(item.get("repost")),
                        media_type="video",
                        media_url=playlist,
                        media_identity=identity,
                        duration=video.get("duration"),
                        video_width=video.get("width"),
                        video_height=video.get("height"),
                    )
                    candidates[key] = candidate
                    if on_candidate:
                        on_candidate(candidate)

    def _prepare_video_capture(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._video_pages:
            return
        page.on("response", self._on_response)
        self._video_pages.add(page_id)

    def _on_response(self, response) -> None:
        try:
            url = response.url
            parts = urlsplit(url)
            if parts.netloc.lower() != "video.twimg.com" or not parts.path.lower().endswith(".m3u8"):
                return
            playlist_path = parts.path.split("/pl/", 1)[-1]
            if "/" in playlist_path:
                return
            identity = self._video_identity(parts.path)
            self._video_playlists[identity].append(url)
        except Exception:
            return

    @staticmethod
    def _video_identity(value: str) -> str:
        match = re.search(r"(?:amplify_video_thumb|amplify_video|ext_tw_video)[^/]*(?:/|%2F)(\d+)", value)
        if match:
            return match.group(1)
        match = re.search(r"/(\d{12,})/", value)
        return match.group(1) if match else ""

    def _playlist_for_video(self, poster: str) -> str:
        identity = self._video_identity(poster)
        if identity and self._video_playlists.get(identity):
            return self._video_playlists[identity][0]
        groups = [playlists for key, playlists in self._video_playlists.items() if key and playlists]
        if not identity and len(groups) == 1:
            return groups[0][0]
        return ""

    @staticmethod
    def _prefer_original_url(url: str) -> str:
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["name"] = "orig"
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
