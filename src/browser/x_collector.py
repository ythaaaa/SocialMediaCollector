from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Page

from src.browser.collector import ImageCandidate, PinterestCollector
from src.extractors import PromptDetector
from src.utils.x import normalize_x_author


@dataclass
class XScanDiagnostics:
    article_count: int = 0
    status_link_count: int = 0
    tweet_text_count: int = 0
    image_node_count: int = 0
    video_node_count: int = 0
    author_match_count: int = 0
    author_mismatch_count: int = 0
    author_unknown_count: int = 0
    media_candidate_count: int = 0
    prompt_label_matched: int = 0
    prompt_parameter_matched: int = 0
    prompt_detected_count: int = 0
    prompt_filtered_count: int = 0

    @property
    def raw_media_count(self) -> int:
        return self.image_node_count + self.video_node_count

    def signature(self) -> tuple[int, ...]:
        return (
            self.article_count,
            self.status_link_count,
            self.tweet_text_count,
            self.image_node_count,
            self.video_node_count,
            self.author_match_count,
            self.author_mismatch_count,
            self.author_unknown_count,
            self.media_candidate_count,
            self.prompt_label_matched,
            self.prompt_parameter_matched,
            self.prompt_detected_count,
            self.prompt_filtered_count,
        )

    def as_log_message(self) -> str:
        return (
            "X 页面扫描："
            f"article={self.article_count} "
            f"status={self.status_link_count} "
            f"tweetText={self.tweet_text_count} "
            f"images={self.image_node_count} "
            f"videos={self.video_node_count} "
            f"authorMatch={self.author_match_count} "
            f"authorMismatch={self.author_mismatch_count} "
            f"mediaCandidates={self.media_candidate_count} "
            f"promptLabelMatched={self.prompt_label_matched} "
            f"promptParameterMatched={self.prompt_parameter_matched} "
            f"promptDetected={self.prompt_detected_count} "
            f"promptFiltered={self.prompt_filtered_count}"
        )


class XCollector(PinterestCollector):
    """Collect image media from an X author's visible profile timeline."""

    def __init__(self, profile_dir, log, debug_dir: Path | None = None) -> None:
        super().__init__(profile_dir, log)
        self.prompt_detector = PromptDetector()
        self._video_playlists: dict[str, list[str]] = defaultdict(list)
        self._video_pages: set[int] = set()
        self.debug_dir = debug_dir
        self.last_scan_diagnostics = XScanDiagnostics()
        self._last_diagnostics_signature: tuple[int, ...] | None = None
        self._prompt_sample_keys: set[str] = set()
        self._prompt_sample_count = 0
        self._prompt_miss_logged = False

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
        only_prompt_posts: bool = False,
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
            only_prompt_posts,
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
        only_prompt_posts: bool = False,
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
            only_prompt_posts,
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
        only_prompt_posts: bool,
    ) -> list[ImageCandidate]:
        candidates: dict[str, ImageCandidate] = {}
        finish = should_finish or (lambda: False)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline and not should_stop():
            should_pause()
            self._collect_visible_tweets(page, username, profile_url, only_author_content, candidates, on_candidate, media_type, only_prompt_posts)
            on_found(len(candidates))
            if candidates or finish():
                break
            page.wait_for_timeout(1500)
        if should_stop():
            return list(candidates.values())
        if not candidates and not finish():
            self._save_debug_snapshot_if_needed(page)
            raise RuntimeError(self._empty_result_message(username, only_author_content, only_prompt_posts))

        stale_rounds = 0
        for _ in range(max_scrolls + 1):
            if should_stop() or finish():
                break
            should_pause()
            before = len(candidates)
            self._collect_visible_tweets(page, username, profile_url, only_author_content, candidates, on_candidate, media_type, only_prompt_posts)
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
        only_prompt_posts: bool = False,
    ) -> XScanDiagnostics:
        payload = page.locator('article, [data-testid="tweet"]').evaluate_all(
            r"""
            (roots) => {
              const articles = Array.from(new Set(roots)).filter((node) => (
                node.tagName.toLowerCase() === 'article' || !node.closest('article')
              ));
              const usernameFromHref = (href) => {
                try {
                  const url = new URL(href, window.location.origin);
                  const parts = url.pathname.split('/').filter(Boolean);
                  if (!parts.length || ['i', 'home', 'explore', 'search', 'notifications', 'messages'].includes(parts[0].toLowerCase())) {
                    return '';
                  }
                  const candidate = parts[0].replace(/^@/, '');
                  return /^[A-Za-z0-9_]{1,15}$/.test(candidate) ? candidate : '';
                } catch (_) {
                  return '';
                }
              };
              return articles.map((article) => {
              const statusLinks = Array.from(article.querySelectorAll('a[href*="/status/"]'))
                .filter(link => link.closest('article') === article)
                .map(link => link.href)
                .filter(href => !href.includes('/analytics'));
              const status = statusLinks[0] || '';
              const links = Array.from(article.querySelectorAll('a[href]'))
                .filter(link => link.closest('article') === article);
              const userNameLinks = links.filter(link => link.closest('[data-testid="User-Name"]'));
              const oneSegmentLinks = links.filter(link => {
                try { return new URL(link.href, window.location.origin).pathname.split('/').filter(Boolean).length === 1; }
                catch (_) { return false; }
              });
              const authorLink = userNameLinks.find(link => usernameFromHref(link.href))
                || oneSegmentLinks.find(link => usernameFromHref(link.href))
                || links.find(link => link.href.toLowerCase().includes('/status/') && usernameFromHref(link.href));
              const authorUsername = authorLink ? usernameFromHref(authorLink.href) : '';
              const social = article.querySelector('[data-testid="socialContext"]')?.textContent || '';
              const lowerSocial = social.toLowerCase();
              const repost = lowerSocial.includes('repost') || lowerSocial.includes('retweeted') || social.includes('转发');
              const videos = Array.from(article.querySelectorAll('video'))
                .filter(video => video.closest('article') === article)
                .map(video => ({
                  poster: video.poster || '',
                  duration: Number.isFinite(video.duration) ? video.duration : null,
                  width: video.videoWidth || 0,
                  height: video.videoHeight || 0,
                }));
              const videoThumbs = Array.from(article.querySelectorAll('[data-testid="videoPlayer"] img, img[src*="ext_tw_video_thumb"], img[src*="amplify_video_thumb"]'))
                .filter(img => img.closest('article') === article)
                .map(img => ({poster: img.currentSrc || img.src || '', duration: null, width: 0, height: 0}))
                .filter(item => item.poster && !videos.some(video => video.poster === item.poster));
              videos.push(...videoThumbs);
              const video = videos.length > 0 || Boolean(article.querySelector('[data-testid="videoPlayer"], img[src*="ext_tw_video_thumb"], img[src*="amplify_video_thumb"]'));
              const tweetText = Array.from(article.querySelectorAll('[data-testid="tweetText"]'))
                .filter(node => node.closest('article') === article)
                .map(node => node.innerText || '')
                .find(value => value.trim()) || '';
              const postedTime = Array.from(article.querySelectorAll('time'))
                .find(node => node.closest('article') === article);
              const postedAt = postedTime?.getAttribute('datetime') || '';
              const text = (article.innerText || '').slice(0, 1600);
              const labels = Array.from(article.querySelectorAll('[aria-label], [data-testid]'))
                .map(node => `${node.getAttribute('aria-label') || ''} ${node.getAttribute('data-testid') || ''}`)
                .join(' ');
              const promoted = /promoted|sponsored|advertisement|推广内容|广告内容|赞助内容/i.test(`${text} ${labels}`);
              const media = Array.from(article.querySelectorAll('[data-testid="tweetPhoto"] img, img'))
                .filter((img) => img.closest('article') === article)
                .map((img) => [img.currentSrc || '', img.src || '', ...(img.srcset || '').split(',').map(value => value.trim().split(/\s+/)[0])]
                  .find(url => /pbs\.twimg\.com\/media\//i.test(url)) || '')
                .filter((url) => /pbs\.twimg\.com\/media\//i.test(url));
              const uniqueMedia = Array.from(new Set(media));
              return {status, authorUsername, repost, video, videos, promoted, media: uniqueMedia, tweetText, postedAt, statusLinkCount: statusLinks.length};
              })
            }
            """
        )
        diagnostics = XScanDiagnostics(article_count=len(payload))
        for item in payload:
            diagnostics.status_link_count += int(item.get("statusLinkCount") or 0)
            if str(item.get("tweetText") or "").strip():
                diagnostics.tweet_text_count += 1
                self._log_prompt_sample(str(item.get("tweetText") or ""), str(item.get("status") or ""))
            diagnostics.image_node_count += len(item.get("media") or [])
            diagnostics.video_node_count += len(item.get("videos") or [])
            if item.get("promoted"):
                continue
            author_name = str(item.get("authorUsername") or item.get("authorName") or "").strip().lstrip("@").strip("/")
            if username and author_name:
                if author_name.casefold() == username.casefold() and not item.get("repost"):
                    diagnostics.author_match_count += 1
                else:
                    diagnostics.author_mismatch_count += 1
            elif username:
                diagnostics.author_unknown_count += 1
            if only_author_content and username and author_name and (author_name.casefold() != username.casefold() or item.get("repost")):
                continue
            detection = self.prompt_detector.detect(str(item.get("tweetText") or ""))
            if detection.label_matched:
                diagnostics.prompt_label_matched += 1
            if detection.parameter_matched:
                diagnostics.prompt_parameter_matched += 1
            if detection.prompt_detected:
                diagnostics.prompt_detected_count += 1
            if only_prompt_posts and not detection.prompt_detected:
                if item.get("media") or item.get("videos"):
                    diagnostics.prompt_filtered_count += 1
                continue
            post_url = str(item.get("status") or profile_url)
            post_id = self._post_id(post_url)
            post_text = str(item.get("tweetText") or "")
            posted_at = str(item.get("postedAt") or "")
            candidate_author = author_name or username
            image_allowed = media_type in {"image", "mixed"} and (media_type == "mixed" or not item.get("video"))
            if image_allowed:
                diagnostics.media_candidate_count += len(item.get("media") or [])
            if media_type in {"video", "mixed"}:
                diagnostics.media_candidate_count += len(item.get("videos") or [])
            if image_allowed:
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
                        author_username=candidate_author,
                        is_repost=bool(item.get("repost")),
                        media_type="image",
                        media_url=image_url,
                        media_identity=image_url,
                        post_id=post_id,
                        post_text=post_text,
                        prompt_detected=detection.prompt_detected,
                        prompt=detection.prompt,
                        posted_at=posted_at,
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
                        author_username=candidate_author,
                        is_repost=bool(item.get("repost")),
                        media_type="video",
                        media_url=playlist,
                        media_identity=identity,
                        duration=video.get("duration"),
                        video_width=video.get("width"),
                        video_height=video.get("height"),
                        post_id=post_id,
                        post_text=post_text,
                        prompt_detected=detection.prompt_detected,
                        prompt=detection.prompt,
                        posted_at=posted_at,
                    )
                    candidates[key] = candidate
                    if on_candidate:
                        on_candidate(candidate)

        self.last_scan_diagnostics = diagnostics
        if diagnostics.signature() != self._last_diagnostics_signature:
            self.log(diagnostics.as_log_message())
            self._last_diagnostics_signature = diagnostics.signature()
        if diagnostics.tweet_text_count and diagnostics.prompt_detected_count == 0 and not self._prompt_miss_logged:
            self.log("已抓取正文，但 PromptDetector 未命中。请查看 Prompt 诊断样本。")
            self._prompt_miss_logged = True
        elif diagnostics.prompt_detected_count:
            self._prompt_miss_logged = False
        return diagnostics

    def _log_prompt_sample(self, text: str, status: str) -> None:
        if self._prompt_sample_count >= 5:
            return
        key = f"{status}|{text}"
        if key in self._prompt_sample_keys:
            return
        self._prompt_sample_keys.add(key)
        self._prompt_sample_count += 1
        sample = re.sub(r"\s+", " ", text).strip()[:240]
        if len(text) > 240:
            sample += "…"
        self.log(f'Prompt诊断 #{self._prompt_sample_count}: "{sample.replace(chr(34), chr(39))}"')

    def _empty_result_message(self, username: str, only_author_content: bool, only_prompt_posts: bool) -> str:
        diagnostics = self.last_scan_diagnostics
        if diagnostics.article_count == 0:
            return "X 页面未检测到帖子，可能未登录、页面加载失败或 X 页面结构发生变化。"
        if diagnostics.raw_media_count == 0:
            return "已检测到 X 帖子，但未识别到符合条件的图片/视频。"
        if only_author_content and username and diagnostics.author_match_count == 0 and diagnostics.author_mismatch_count > 0:
            return "已检测到媒体，但作者匹配过滤后无候选，请检查作者识别逻辑。"
        if only_prompt_posts and diagnostics.prompt_detected_count == 0:
            return "已检测到媒体，但当前扫描范围内未发现疑似包含 Prompt 的帖子。"
        if diagnostics.media_candidate_count == 0:
            return "已检测到 X 帖子，但未识别到符合条件的图片/视频。"
        return "已检测到 X 内容，但没有可下载的候选资源。"

    def _save_debug_snapshot_if_needed(self, page: Page) -> None:
        diagnostics = self.last_scan_diagnostics
        if not self.debug_dir or not (diagnostics.article_count == 0 or diagnostics.media_candidate_count == 0):
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / "debug_x_page.html").write_text(page.content(), encoding="utf-8")
            try:
                page.screenshot(path=str(self.debug_dir / "debug_x_page.png"), full_page=True)
            except Exception as exc:
                self.log(f"X 调试截图保存失败：{exc}")
            self.log(f"X 失败诊断快照已保存到：{self.debug_dir}")
        except Exception as exc:
            self.log(f"X 调试页面保存失败：{exc}")

    def _prepare_video_capture(self, page: Page) -> None:
        page_id = id(page)
        if page_id in self._video_pages:
            return
        page.on("response", self._on_response)
        self._video_pages.add(page_id)

    @staticmethod
    def _post_id(post_url: str) -> str:
        match = re.search(r"/status/(\d+)", post_url)
        return match.group(1) if match else ""

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
