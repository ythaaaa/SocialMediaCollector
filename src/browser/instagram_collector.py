from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Error as PlaywrightError, Page

from src.browser.collector import ImageCandidate, PinterestVideoSource
from src.browser.session import BrowserSession


INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
RESERVED_AUTHOR_PATHS = {"explore", "reels", "p", "accounts", "direct", "stories", "about", "web"}
POST_PATHS = {"p", "reel", "reels", "tv"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class InstagramMedia:
    media_index: int
    media_type: str
    media_url: str
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    bitrate: int | None = None
    thumbnail_url: str = ""
    source: str = ""


@dataclass(frozen=True)
class InstagramPostMetadata:
    platform: str
    collection_mode: str
    source_value: str
    post_id: str
    shortcode: str
    post_url: str
    author_username: str
    caption: str
    posted_at: str | None
    collected_at: str
    media_type: str
    media: tuple[InstagramMedia, ...]
    metadata_source: str

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["media"] = [asdict(item) for item in self.media]
        return data


@dataclass
class InstagramScanDiagnostics:
    page_url: str = ""
    mode: str = ""
    post_links_count: int = 0
    reel_links_count: int = 0
    img_count: int = 0
    video_count: int = 0
    candidate_image_count: int = 0
    candidate_video_count: int = 0
    author_match_count: int = 0
    carousel_next_count: int = 0
    video_source_detected_count: int = 0
    direct_mp4_count: int = 0
    dash_manifest_count: int = 0
    hls_manifest_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        return ", ".join(f"{key}={value}" for key, value in asdict(self).items())


def _instagram_url_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _remove_range_query(url: str) -> str:
    parsed = urlparse(url)
    query_parts = [
        part for part in parsed.query.split("&")
        if part and part.split("=", 1)[0].casefold() not in {"bytestart", "byteend"}
    ]
    return parsed._replace(query="&".join(query_parts), fragment="").geturl()


def _media_dedupe_key(media: InstagramMedia) -> tuple[str, str]:
    url_key = _remove_range_query(media.media_url)
    if media.media_type == "video" and media.source.startswith("network:"):
        parsed = urlparse(media.media_url)
        if parsed.netloc and parsed.path:
            # Instagram may issue several signed/ranged requests for one
            # video. Keep the first signed URL, but treat the CDN resource as
            # one candidate during discovery.
            url_key = f"{parsed.netloc}{parsed.path}"
    return media.media_type, url_key


def _network_media_key(url: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc and parsed.path:
        return f"{parsed.netloc}{parsed.path}"
    return _remove_range_query(url)


def normalize_author(value: str) -> tuple[str, str]:
    """Normalize an Instagram username or profile URL to username and URL."""
    raw = (value or "").strip()
    if raw.startswith("@"):
        raw = raw[1:].strip()
    if not raw:
        raise ValueError("Instagram 作者不能为空")

    if "://" not in raw and "/" not in raw:
        username = raw
    else:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if parsed.scheme not in {"http", "https"} or _instagram_url_host(parsed.geturl()) not in INSTAGRAM_HOSTS:
            raise ValueError("Instagram 作者地址无效")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise ValueError("请输入 Instagram 作者主页，而不是帖子、Reel 或其它页面地址")
        username = parts[0]

    if username.casefold() in RESERVED_AUTHOR_PATHS or not USERNAME_RE.fullmatch(username):
        raise ValueError("Instagram 作者用户名无效")
    return username, f"https://www.instagram.com/{username}/"


def normalize_author_input(value: str) -> tuple[str, str]:
    return normalize_author(value)


def normalize_single_post_url(value: str) -> str:
    raw = (value or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or _instagram_url_host(parsed.geturl()) not in INSTAGRAM_HOSTS:
        raise ValueError("Instagram 帖子或 Reel 地址无效")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0].casefold() not in POST_PATHS or not SHORTCODE_RE.fullmatch(parts[1]):
        raise ValueError("请输入 Instagram /p/ 或 /reel/ 帖子地址")
    path_kind = "reel" if parts[0].casefold() == "reels" else parts[0].casefold()
    return f"https://www.instagram.com/{path_kind}/{parts[1]}/"


def extract_shortcode(value: str) -> str:
    try:
        canonical = normalize_single_post_url(value)
    except ValueError:
        return ""
    parts = [part for part in urlparse(canonical).path.split("/") if part]
    return parts[1] if len(parts) == 2 else ""


def parse_srcset(srcset: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for raw_item in (srcset or "").split(","):
        parts = raw_item.strip().split()
        if not parts:
            continue
        width = None
        if len(parts) > 1:
            match = re.fullmatch(r"(\d+)w", parts[1])
            if match:
                width = int(match.group(1))
        candidates.append({"url": parts[0], "width": width, "source": "srcset"})
    return candidates


def choose_best_image(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    usable = [item for item in candidates if str(item.get("url") or "").startswith(("http://", "https://"))]
    if not usable:
        return None

    def score(item: dict[str, Any]) -> tuple[int, int, int]:
        width = _positive_int(item.get("width")) or 0
        height = _positive_int(item.get("height")) or 0
        return width * height, width, height

    return max(usable, key=score)


def choose_best_video(candidates: list[InstagramMedia]) -> InstagramMedia | None:
    """Choose the highest-quality usable video without requiring dimensions."""
    usable = [
        item for item in candidates
        if item.media_type == "video" and item.media_url.startswith(("http://", "https://"))
    ]
    return max(
        usable,
        key=lambda item: (
            (item.width or 0) * (item.height or 0),
            item.bitrate or 0,
        ),
        default=None,
    )


def media_identity(shortcode: str, media_index: int) -> str:
    return f"{shortcode}:{media_index}"


def media_matches_filter(candidate_type: str, requested_type: str) -> bool:
    return requested_type == "mixed" or candidate_type == requested_type


def _positive_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _float_value(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _first_url(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in value:
            url = _first_url(item)
            if url:
                return url
    if isinstance(value, dict):
        return str(value.get("url") or value.get("src") or "")
    return ""


class InstagramCollector(BrowserSession):
    """Discover Instagram post media while reusing the app's browser session."""

    MAX_CAROUSEL_SLIDES = 20
    AUTHOR_NO_NEW_POST_ROUNDS = 5

    def __init__(self, profile_dir: Path, log: Callable[[str], None], debug_dir: Path | None = None) -> None:
        super().__init__(profile_dir, log)
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self.last_scan_diagnostics = InstagramScanDiagnostics()
        self.last_video_source_types: set[str] = set()
        self._worker_pages: set[int] = set()
        self._worker_page_objects: list[Page] = []
        self._network_listener_pages: set[int] = set()
        self._network_media: list[InstagramMedia] = []
        self._network_source_counts: dict[str, int] = {"direct_mp4": 0, "DASH": 0, "HLS": 0}
        self._network_seen_urls: set[str] = set()
        self._network_post_links: list[str] = []
        self._network_seen_post_links: set[str] = set()
        self._author_discovery_username = ""

    def close(self) -> None:
        for page in self._worker_page_objects:
            try:
                if not page.is_closed():
                    page.close()
            except Exception:
                pass
        self._worker_page_objects.clear()
        self._worker_pages.clear()
        self._network_listener_pages.clear()
        self._network_media.clear()
        self._network_seen_urls.clear()
        self._network_post_links.clear()
        self._network_seen_post_links.clear()
        self._author_discovery_username = ""
        super().close()

    @staticmethod
    def ensure_logged_in(page: Page) -> None:
        """Perform a small collection-time login check without login automation."""
        page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=60000)
        if "/accounts/login" in (page.url or "").casefold():
            raise RuntimeError(
                "Instagram 尚未登录，请先点击『打开浏览器 / 登录』，在 Chrome 中完成登录后再开始采集。"
            )
        try:
            login_controls = page.locator(
                "input[name='username'], input[name='password'], "
                "input[autocomplete='username'], input[autocomplete='current-password'], "
                "form[action*='/accounts/login']"
            )
            if login_controls.count() > 0:
                raise RuntimeError(
                    "Instagram 尚未登录，请先点击『打开浏览器 / 登录』，在 Chrome 中完成登录后再开始采集。"
                )
        except RuntimeError:
            raise
        except Exception:
            # A page that does not expose the optional DOM probe is allowed to
            # continue; the URL redirect remains the primary safe check.
            pass

    def collect_author(
        self,
        author_input: str,
        wanted: int,
        max_scrolls: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
        media_type: str = "mixed",
    ) -> list[ImageCandidate]:
        self.last_video_source_types.clear()
        username, profile_url = normalize_author(author_input)
        page = self._task_page()
        self.ensure_logged_in(page)
        self._author_discovery_username = username.casefold()
        self._network_post_links.clear()
        self._network_seen_post_links.clear()
        self._attach_network_listener(page)
        page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)
        self.log(f"Instagram 作者主页已打开：{profile_url}；如需登录或验证，请在浏览器中人工完成。")
        self._wait_for_author_content(page, should_stop)
        seen_links: set[str] = set()
        diagnostics = InstagramScanDiagnostics(page_url=profile_url, mode="author")
        initial_links = self._author_discovery_links(page)
        if not initial_links:
            self._log_author_page_diagnostic(page, profile_url, username, 0, 0)
            self.last_scan_diagnostics = diagnostics
            return []
        candidates: dict[str, ImageCandidate] = {}
        finish = should_finish or (lambda: False)
        def handle_candidate(candidate: ImageCandidate) -> None:
            if candidate.media_identity in candidates:
                return
            candidates[candidate.media_identity] = candidate
            if on_candidate:
                on_candidate(candidate)

        no_new_post_rounds = 0
        for round_index in range(max(0, max_scrolls) + 1):
            if should_stop() or finish():
                break
            should_pause()
            if should_stop() or finish():
                break

            discovered = self._author_discovery_links(page)
            new_links = [link for link in discovered if link not in seen_links]
            seen_links.update(new_links)
            diagnostics.post_links_count = sum(1 for link in seen_links if "/p/" in urlparse(link).path)
            diagnostics.reel_links_count = sum(1 for link in seen_links if "/reel/" in urlparse(link).path)
            if new_links:
                no_new_post_rounds = 0
                self.log(f"Instagram 作者页：发现 {len(new_links)} 个新帖子")
            else:
                no_new_post_rounds += 1

            for post_url in new_links:
                if should_stop() or finish():
                    break
                should_pause()
                if should_stop() or finish():
                    break
                try:
                    self._network_media.clear()
                    self._reset_network_source_counts()
                    self._attach_network_listener(page)
                    page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as exc:
                    self.log(f"Instagram 帖子打开失败，跳过：{post_url} - {exc}")
                    continue
                metadata, post_diagnostics = self._discover_post(
                    page,
                    post_url,
                    "author",
                    username,
                    media_type,
                    on_candidate=handle_candidate,
                    should_stop=should_stop,
                    should_pause=should_pause,
                    should_finish=finish,
                )
                self._merge_diagnostics(diagnostics, post_diagnostics)
                on_found(len(candidates))
                if finish():
                    self.log("Instagram 作者页：目标已达到，停止继续滚动")
                    break

            if should_stop() or finish():
                break
            if round_index >= max(0, max_scrolls) or no_new_post_rounds >= self.AUTHOR_NO_NEW_POST_ROUNDS:
                break
            page.mouse.wheel(0, 1800)
            if should_stop():
                break
            page.wait_for_timeout(900)
            if should_stop():
                break
        self.last_scan_diagnostics = diagnostics
        self._log_diagnostic_if_needed(diagnostics, not candidates)
        return list(candidates.values())

    def collect_single_post(
        self,
        post_input: str,
        wanted: int,
        max_scrolls: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
        on_found: Callable[[int], None],
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
        media_type: str = "mixed",
    ) -> list[ImageCandidate]:
        del max_scrolls
        self.last_video_source_types.clear()
        post_url = normalize_single_post_url(post_input)
        if should_stop():
            return []
        should_pause()
        page = self._task_page()
        self.ensure_logged_in(page)
        self._network_media.clear()
        self._reset_network_source_counts()
        self._attach_network_listener(page)
        page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
        self.log(f"Instagram 单条帖子已打开：{post_url}；如需登录或验证，请在浏览器中人工完成。")
        metadata, diagnostics = self._discover_post(page, post_url, "single_post", "", media_type)
        self.last_scan_diagnostics = diagnostics
        candidates = self._candidates_from_metadata(metadata) if metadata else []
        if should_finish and should_finish():
            candidates = candidates[:wanted]
        if on_candidate:
            for candidate in candidates[:wanted]:
                if should_stop():
                    break
                should_pause()
                on_candidate(candidate)
        on_found(min(len(candidates), wanted))
        self._log_diagnostic_if_needed(diagnostics, not candidates)
        return candidates[:wanted]

    def _task_page(self) -> Page:
        if self._context:
            page = self._context.new_page()
            self._worker_pages.add(id(page))
            self._worker_page_objects.append(page)
            return page
        if not self._page:
            raise RuntimeError("浏览器未初始化")
        return self._page

    @staticmethod
    def _discover_post_links(page: Page) -> list[str]:
        try:
            values = page.locator("a").evaluate_all(
                """links => links.map(link => link.href || link.getAttribute('href') || '')"""
            )
        except Exception:
            return []
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            try:
                normalized = normalize_single_post_url(urljoin(page.url, str(value)))
            except ValueError:
                continue
            if normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def _author_discovery_links(self, page: Page) -> list[str]:
        result = self._discover_post_links(page)
        seen = set(result)
        for link in self._network_post_links:
            if link not in seen:
                seen.add(link)
                result.append(link)
        return result

    def _wait_for_author_content(self, page: Page, should_stop: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if should_stop():
                return
            path = urlparse(page.url).path.casefold()
            if "/accounts/login" in path or "/challenge" in path or "/checkpoint" in path:
                return
            if self._author_discovery_links(page):
                return
            page.wait_for_timeout(400)

    def _log_author_page_diagnostic(
        self,
        page: Page,
        requested_url: str,
        username: str,
        new_post_count: int,
        scroll_round: int,
    ) -> None:
        try:
            hrefs = page.locator("a[href]").evaluate_all(
                "links => links.map(link => link.href || link.getAttribute('href') || '')"
            )
        except Exception:
            hrefs = []
        final_url = urlparse(page.url)._replace(query="", fragment="").geturl()
        path = urlparse(page.url).path.casefold()
        try:
            body_text_length = len(page.locator("body").inner_text())
        except Exception:
            body_text_length = 0
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            ready_state = str(page.evaluate("document.readyState"))
        except Exception:
            ready_state = "unknown"
        body_text = ""
        try:
            body_text = page.locator("body").inner_text().casefold()
        except Exception:
            pass
        if "/accounts/login" in path:
            login_state = "login_redirect"
        elif "/challenge" in path or "/checkpoint" in path:
            login_state = "auth_challenge"
        elif any(token in body_text for token in ("private account", "此帐户是私密帐户")):
            login_state = "private_account"
        elif any(token in body_text for token in ("page isn't available", "页面不可用", "user not found", "用户不存在")):
            login_state = "page_unavailable"
        else:
            login_state = "normal"
        pattern_counts = {
            "anchor_count": len(hrefs),
            "p_link_count": sum("/p/" in str(value).casefold() for value in hrefs),
            "reel_link_count": sum("/reel/" in str(value).casefold() for value in hrefs),
            "reels_link_count": sum("/reels/" in str(value).casefold() for value in hrefs),
            "tv_link_count": sum("/tv/" in str(value).casefold() for value in hrefs),
            "target_username_href_count": sum(username.casefold() in str(value).casefold() for value in hrefs),
            "relative_href_count": sum(str(value).startswith("/") for value in hrefs),
            "absolute_instagram_href_count": sum(
                (urlparse(str(value)).hostname or "").casefold().endswith("instagram.com") for value in hrefs
            ),
        }
        self.log(
            "Instagram author diagnostic: "
            f"username={username}, requested_url={requested_url}, final_url={final_url}, "
            f"page_title={title!r}, ready_state={ready_state}, body_text_length={body_text_length}, "
            f"login_state={login_state}, new_post_count={new_post_count}, scroll_round={scroll_round}, "
            f"img_count={page.locator('img').count()}, video_count={page.locator('video').count()}, "
            + ", ".join(f"{key}={value}" for key, value in pattern_counts.items())
        )
        if not any(pattern_counts[key] for key in ("p_link_count", "reel_link_count", "tv_link_count")):
            samples: list[str] = []
            seen_samples: set[str] = set()
            for value in hrefs[:50]:
                parsed = urlparse(str(value))
                sample = parsed.path or "/"
                if sample not in seen_samples:
                    seen_samples.add(sample)
                    samples.append(sample)
            if samples:
                self.log(f"Instagram author href samples: {samples}")

    def _discover_post(
        self,
        page: Page,
        post_url: str,
        mode: str,
        expected_author: str,
        requested_media_type: str,
        on_candidate: Callable[[ImageCandidate], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        should_pause: Callable[[], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
    ) -> tuple[InstagramPostMetadata | None, InstagramScanDiagnostics]:
        diagnostics = InstagramScanDiagnostics(page_url=post_url, mode=mode)
        payloads = self._script_payloads(page)
        payload_source_counts = self._video_source_counts(payloads)
        self.last_video_source_types.update(payload_source_counts)
        info = self._post_info(payloads)
        shortcode = str(info.get("shortcode") or extract_shortcode(post_url))
        author = str(info.get("author_username") or "")
        if expected_author and author and author.casefold() != expected_author.casefold():
            self.log(f"Instagram 作者不匹配，跳过帖子：{post_url}")
            return None, diagnostics
        if expected_author and not author:
            self.log(f"Instagram 无法确认帖子作者，跳过并记录 diagnostic：{post_url}")
            return None, diagnostics
        if expected_author and author.casefold() == expected_author.casefold():
            diagnostics.author_match_count = 1

        media = self._media_from_payloads(payloads, shortcode)
        dom_media, dom_counts = self._media_from_dom(page, post_url)
        diagnostics.img_count = dom_counts[0]
        diagnostics.video_count = dom_counts[1]
        if not media:
            media = dom_media
        else:
            media = self._merge_media(media, dom_media)
        if not any(item.media_type == "video" for item in media):
            media = self._merge_media(media, self._network_media)
        finish = should_finish or (lambda: False)
        stopped = should_stop or (lambda: False)
        pause = should_pause or (lambda: None)

        def build_metadata(items: list[InstagramMedia]) -> InstagramPostMetadata:
            overall_type = "mixed" if {item.media_type for item in items} == {"image", "video"} else items[0].media_type
            metadata_source = ";".join(sorted({item.source for item in items if item.source})) or "instagram_dom"
            return InstagramPostMetadata(
                platform="instagram",
                collection_mode=mode,
                source_value=expected_author or post_url,
                post_id=shortcode,
                shortcode=shortcode,
                post_url=post_url,
                author_username=author,
                caption=str(info.get("caption") or ""),
                posted_at=str(info.get("posted_at") or "") or None,
                collected_at=datetime.now().isoformat(timespec="seconds"),
                media_type=overall_type,
                media=tuple(items),
                metadata_source=metadata_source,
            )

        def emit_media(items: list[InstagramMedia]) -> None:
            if not on_candidate:
                return
            filtered_items = [item for item in items if media_matches_filter(item.media_type, requested_media_type)]
            if not filtered_items:
                return
            for candidate in self._candidates_from_metadata(build_metadata(filtered_items)):
                if stopped() or finish():
                    break
                pause()
                if stopped() or finish():
                    break
                on_candidate(candidate)

        filtered_before_carousel = [item for item in media if media_matches_filter(item.media_type, requested_media_type)]
        emit_media(filtered_before_carousel)
        if finish() or stopped():
            carousel_media, carousel_next_count = [], 0
        else:
            carousel_media, carousel_next_count = self._scan_carousel(
                page,
                post_url,
                media,
                on_media=emit_media,
                should_stop=stopped,
                should_pause=pause,
                should_finish=finish,
            )
        diagnostics.carousel_next_count = carousel_next_count
        media = self._merge_media(media, carousel_media)
        self.last_video_source_types.update(
            source_type
            for source_type in (self._source_type(item.source) for item in media if item.media_type == "video")
            if source_type
        )

        filtered = [item for item in media if media_matches_filter(item.media_type, requested_media_type)]
        diagnostics.candidate_image_count = sum(item.media_type == "image" for item in filtered)
        diagnostics.candidate_video_count = sum(item.media_type == "video" for item in filtered)
        diagnostics.direct_mp4_count = payload_source_counts.get("direct_mp4", 0) + self._network_source_counts.get("direct_mp4", 0)
        diagnostics.dash_manifest_count = payload_source_counts.get("DASH", 0) + self._network_source_counts.get("DASH", 0)
        diagnostics.hls_manifest_count = payload_source_counts.get("HLS", 0) + self._network_source_counts.get("HLS", 0)
        diagnostics.video_source_detected_count = (
            diagnostics.direct_mp4_count + diagnostics.dash_manifest_count + diagnostics.hls_manifest_count
        )
        if not filtered:
            self._save_debug_snapshot(page, diagnostics)
            return None, diagnostics

        return build_metadata(filtered), diagnostics

    @staticmethod
    def _script_payloads(page: Page) -> list[Any]:
        try:
            scripts = page.locator("script").all_text_contents()
        except Exception:
            return []
        payloads: list[Any] = []
        for text in scripts:
            raw = text.strip()
            if not raw:
                continue
            try:
                payloads.append(json.loads(raw))
                continue
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            for marker in ("window._sharedData =", "__additionalDataLoaded("):
                start = raw.find("{")
                if marker in raw and start >= 0:
                    try:
                        payloads.append(json.JSONDecoder().raw_decode(raw[start:])[0])
                    except (ValueError, json.JSONDecodeError):
                        pass
                    break
        return payloads

    @classmethod
    def _post_info(cls, payloads: list[Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}

        def walk(value: Any) -> None:
            if result.get("shortcode") and result.get("author_username") and result.get("caption"):
                return
            if isinstance(value, dict):
                code = value.get("shortcode") or value.get("code")
                media_signal = any(key in value for key in ("display_url", "display_resources", "video_url", "carousel_media", "edge_sidecar_to_children"))
                if code and media_signal and not result.get("shortcode"):
                    result["shortcode"] = str(code)
                    result["author_username"] = cls._username_from(value.get("owner") or value.get("user") or value.get("author"))
                    result["caption"] = cls._caption_from(value)
                    result["posted_at"] = cls._posted_at_from(value)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for payload in payloads:
            walk(payload)
            if not result.get("caption"):
                result["caption"] = cls._jsonld_caption(payload)
            if not result.get("author_username"):
                result["author_username"] = cls._jsonld_author(payload)
            if not result.get("posted_at"):
                result["posted_at"] = cls._jsonld_date(payload)
        return result

    @staticmethod
    def _video_source_types(payloads: list[Any]) -> set[str]:
        return set(InstagramCollector._video_source_counts(payloads))

    @staticmethod
    def _video_source_counts(payloads: list[Any]) -> dict[str, int]:
        urls: dict[str, set[str]] = {"direct_mp4": set(), "DASH": set(), "HLS": set()}

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                direct = value.get("video_url")
                if isinstance(direct, str) and direct.startswith(("http://", "https://")):
                    urls["direct_mp4"].add(direct)
                for key in ("video_versions", "video_resources"):
                    variants = value.get(key)
                    if isinstance(variants, dict):
                        variants = variants.get("candidates") or variants.get("resources") or []
                    if isinstance(variants, list):
                        for item in variants:
                            if isinstance(item, dict):
                                url = item.get("url") or item.get("src")
                                if isinstance(url, str) and url.startswith(("http://", "https://")):
                                    urls["direct_mp4"].add(url)
                for key in ("video_dash_manifest", "dash_manifest"):
                    manifest = value.get(key)
                    if manifest:
                        urls["DASH"].add(str(manifest))
                for key in ("video_hls_manifest", "hls_manifest"):
                    manifest = value.get(key)
                    if manifest:
                        urls["HLS"].add(str(manifest))
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        for payload in payloads:
            walk(payload)
        return {key: len(values) for key, values in urls.items() if values}

    def _collect_network_author_links(self, payload: Any) -> None:
        target = self._author_discovery_username
        if not target:
            return

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                code = str(value.get("shortcode") or value.get("code") or "")
                owner = value.get("owner") or value.get("user") or value.get("author") or {}
                owner_username = self._username_from(owner).casefold()
                if code and SHORTCODE_RE.fullmatch(code) and owner_username == target:
                    link = self._network_post_url(value, code)
                    if link and link not in self._network_seen_post_links:
                        self._network_seen_post_links.add(link)
                        self._network_post_links.append(link)
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)

    @staticmethod
    def _network_post_url(node: dict[str, Any], code: str) -> str:
        for key in ("permalink", "post_url", "url", "link"):
            value = node.get(key)
            if not isinstance(value, str):
                continue
            try:
                return normalize_single_post_url(value)
            except ValueError:
                continue
        product_type = str(node.get("product_type") or node.get("media_product_type") or "").casefold()
        typename = str(node.get("__typename") or "").casefold()
        media_type = node.get("media_type")
        path_kind = "reel" if media_type in {2, "2", "video"} or "reel" in product_type or "clip" in product_type or "reel" in typename or "clip" in typename else "p"
        return f"https://www.instagram.com/{path_kind}/{code}/"

    @staticmethod
    def _source_type(source: str) -> str:
        lowered = (source or "").casefold()
        if "direct_mp4" in lowered or source == "dom:video":
            return "direct_mp4"
        if "dash" in lowered:
            return "DASH"
        if "hls" in lowered or "m3u8" in lowered:
            return "HLS"
        return ""

    def _reset_network_source_counts(self) -> None:
        self._network_source_counts = {"direct_mp4": 0, "DASH": 0, "HLS": 0}
        self._network_seen_urls.clear()

    def _attach_network_listener(self, page: Page) -> None:
        if id(page) in self._network_listener_pages:
            return
        page.on("response", self._on_network_response)
        self._network_listener_pages.add(id(page))

    def _on_network_response(self, response: Any) -> None:
        try:
            url = str(response.url or "")
            headers = {str(key).casefold(): str(value).casefold() for key, value in (response.headers or {}).items()}
            content_type = headers.get("content-type", "").split(";", 1)[0].strip()
        except Exception:
            return
        if not url.startswith(("http://", "https://")):
            return
        lowered_url = url.casefold()
        if self._author_discovery_username and "json" in content_type:
            try:
                self._collect_network_author_links(response.json())
            except Exception:
                pass
        if content_type in {"application/dash+xml", "application/vnd.apple.mpegurl", "application/x-mpegurl"}:
            kind = "DASH" if "dash" in content_type else "HLS"
            self._network_source_counts[kind] += 1
            self.last_video_source_types.add(kind)
            return
        if content_type == "video/mp4" or ".mp4" in lowered_url:
            dedupe_url = _network_media_key(url)
            if dedupe_url in self._network_seen_urls:
                return
            self._network_seen_urls.add(dedupe_url)
            self._network_source_counts["direct_mp4"] += 1
            self.last_video_source_types.add("direct_mp4")
            self._network_media.append(InstagramMedia(0, "video", url, source="network:direct_mp4"))

    @staticmethod
    def _username_from(value: Any) -> str:
        return str(value.get("username") or value.get("name") or "") if isinstance(value, dict) else ""

    @staticmethod
    def _caption_from(value: dict[str, Any]) -> str:
        caption = value.get("caption")
        if isinstance(caption, str):
            return caption
        if isinstance(caption, dict):
            return str(caption.get("text") or "")
        edge = value.get("edge_media_to_caption")
        if isinstance(edge, dict):
            edges = edge.get("edges") or []
            if edges and isinstance(edges[0], dict):
                node = edges[0].get("node") or {}
                return str(node.get("text") or "") if isinstance(node, dict) else ""
        return ""

    @staticmethod
    def _posted_at_from(value: dict[str, Any]) -> str:
        raw = value.get("taken_at_timestamp") or value.get("taken_at") or value.get("uploadDate") or ""
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw, tz=timezone.utc).isoformat()
        return str(raw)

    @staticmethod
    def _jsonld_caption(value: Any) -> str:
        if isinstance(value, dict):
            if value.get("@type") in {"ImageObject", "VideoObject", "SocialMediaPosting"}:
                return str(value.get("caption") or value.get("description") or "")
            return next((InstagramCollector._jsonld_caption(item) for item in value.values() if InstagramCollector._jsonld_caption(item)), "")
        if isinstance(value, list):
            return next((InstagramCollector._jsonld_caption(item) for item in value if InstagramCollector._jsonld_caption(item)), "")
        return ""

    @staticmethod
    def _jsonld_author(value: Any) -> str:
        if isinstance(value, dict):
            author = value.get("author")
            if isinstance(author, dict) and author.get("name"):
                return str(author["name"])
            return next((InstagramCollector._jsonld_author(item) for item in value.values() if InstagramCollector._jsonld_author(item)), "")
        if isinstance(value, list):
            return next((InstagramCollector._jsonld_author(item) for item in value if InstagramCollector._jsonld_author(item)), "")
        return ""

    @staticmethod
    def _jsonld_date(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("uploadDate", "datePublished", "dateCreated"):
                if value.get(key):
                    return str(value[key])
            return next((InstagramCollector._jsonld_date(item) for item in value.values() if InstagramCollector._jsonld_date(item)), "")
        if isinstance(value, list):
            return next((InstagramCollector._jsonld_date(item) for item in value if InstagramCollector._jsonld_date(item)), "")
        return ""

    @classmethod
    def _media_from_payloads(cls, payloads: list[Any], target_shortcode: str = "") -> list[InstagramMedia]:
        result: list[InstagramMedia] = []
        seen: set[tuple[str, str]] = set()
        target = target_shortcode.casefold()

        def walk(value: Any, inside_target: bool = False) -> None:
            if isinstance(value, dict):
                code = str(value.get("shortcode") or value.get("code") or "").casefold()
                current_target = inside_target or (bool(target) and code == target)
                has_carousel = bool(value.get("edge_sidecar_to_children") or value.get("carousel_media"))
                if (not target or current_target) and not has_carousel:
                    media = cls._media_from_node(value, len(result))
                    if media and _media_dedupe_key(media) not in seen:
                        seen.add(_media_dedupe_key(media))
                        result.append(media)
                for child in value.values():
                    walk(child, current_target)
            elif isinstance(value, list):
                for child in value:
                    walk(child, inside_target)

        for payload in payloads:
            walk(payload)
        return [InstagramMedia(**{**asdict(item), "media_index": index}) for index, item in enumerate(result)]

    @classmethod
    def _media_from_node(cls, node: dict[str, Any], index: int) -> InstagramMedia | None:
        typename = str(node.get("__typename") or "").lower()
        video_signal = any(
            node.get(key)
            for key in (
                "video_url",
                "video_versions",
                "video_resources",
                "video_dash_manifest",
                "dash_manifest",
                "video_hls_manifest",
                "hls_manifest",
            )
        )
        is_video = (
            bool(node.get("is_video"))
            or node.get("media_type") in {2, "2", "video"}
            or "video" in typename
            or video_signal
        )
        if is_video:
            variants = node.get("video_versions") or node.get("video_resources") or []
            chosen = cls._best_variant(variants)
            direct_url = node.get("video_url") or node.get("contentUrl")
            url = str(direct_url or (chosen or {}).get("url") or "")
            manifest = str(node.get("video_dash_manifest") or node.get("dash_manifest") or "")
            hls = str(node.get("video_hls_manifest") or node.get("hls_manifest") or "")
            if manifest:
                # Keep the source type available to diagnostics without treating a manifest as a downloadable poster.
                url = url or ""
            if hls:
                url = url or ""
            if url.startswith("blob:"):
                url = ""
            if url:
                source = "direct_mp4"
            elif manifest:
                source = "DASH"
            elif hls:
                source = "HLS"
            else:
                source = "video_without_direct_url"
            return InstagramMedia(
                index,
                "video",
                url,
                _positive_int((chosen or {}).get("width")) or _positive_int(node.get("width")) or _positive_int(node.get("original_width")),
                _positive_int((chosen or {}).get("height")) or _positive_int(node.get("height")) or _positive_int(node.get("original_height")),
                _float_value(node.get("video_duration") or node.get("duration") or (chosen or {}).get("duration")),
                _positive_int((chosen or {}).get("bitrate")),
                str(node.get("thumbnail_src") or node.get("display_url") or ""),
                source,
            ) if url else None

        if not any(key in node for key in ("display_url", "display_resources", "thumbnail_resources", "image_versions2", "image_versions", "contentUrl", "image")):
            return None
        variants = cls._image_variants(node)
        best = choose_best_image(variants)
        if not best:
            return None
        return InstagramMedia(
            index,
            "image",
            str(best["url"]),
            _positive_int(best.get("width")),
            _positive_int(best.get("height")),
            source=str(best.get("source") or "embedded_json"),
        )

    @staticmethod
    def _image_variants(node: dict[str, Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for key in ("display_resources", "thumbnail_resources", "image_versions2", "candidates", "image_versions"):
            value = node.get(key)
            if isinstance(value, dict):
                value = value.get("candidates") or value.get("resources") or []
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        url = item.get("src") or item.get("url")
                        if url:
                            result.append({
                                "url": str(url),
                                "width": item.get("config_width") or item.get("width") or item.get("w"),
                                "height": item.get("config_height") or item.get("height") or item.get("h"),
                                "source": key,
                            })
        for key in ("display_url", "contentUrl", "image", "url"):
            if isinstance(node.get(key), str) and node[key].startswith(("http://", "https://")):
                result.append({"url": node[key], "width": node.get("width"), "height": node.get("height"), "source": key})
        return result

    @staticmethod
    def _best_variant(variants: Any) -> dict[str, Any] | None:
        if isinstance(variants, dict):
            variants = variants.get("candidates") or variants.get("resources") or []
        if not isinstance(variants, list):
            return None
        usable: list[dict[str, Any]] = []
        for item in variants:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("src")
            if str(url or "").startswith(("http://", "https://")):
                usable.append({**item, "url": str(url)})
        return max(
            usable,
            key=lambda item: (
                (_positive_int(item.get("width")) or 0) * (_positive_int(item.get("height")) or 0),
                _positive_int(item.get("bitrate")) or 0,
            ),
            default=None,
        )

    @classmethod
    def _media_from_dom(cls, page: Page, post_url: str) -> tuple[list[InstagramMedia], tuple[int, int]]:
        try:
            items = page.locator("img, video").evaluate_all(
                """nodes => nodes.map(node => ({tag: node.tagName.toLowerCase(), src: node.currentSrc || node.src || '', srcset: node.srcset || '', poster: node.poster || '', width: node.naturalWidth || node.videoWidth || 0, height: node.naturalHeight || node.videoHeight || 0, href: node.closest('a')?.href || '', article: !!node.closest('article'), alt: node.alt || ''}))"""
            )
        except Exception:
            return [], (0, 0)
        result: list[InstagramMedia] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            tag = str(item.get("tag") or "")
            href = str(item.get("href") or "")
            if not cls._dom_item_belongs_to_post(item, href, post_url):
                continue
            if tag == "video":
                src = str(item.get("src") or "")
                if src.startswith("blob:"):
                    continue
                if not src.startswith(("http://", "https://")):
                    continue
                media = InstagramMedia(len(result), "video", src, _positive_int(item.get("width")), _positive_int(item.get("height")), thumbnail_url=str(item.get("poster") or ""), source="dom:video")
            else:
                variants = parse_srcset(str(item.get("srcset") or ""))
                variants.append({"url": item.get("src"), "width": item.get("width"), "height": item.get("height"), "source": "dom:src"})
                best = choose_best_image(variants)
                if not best:
                    continue
                media = InstagramMedia(len(result), "image", str(best["url"]), _positive_int(best.get("width")), _positive_int(best.get("height")), source=f"dom:{best.get('source')}")
            if (media.media_type, media.media_url) not in seen:
                seen.add((media.media_type, media.media_url))
                result.append(media)
        return [InstagramMedia(**{**asdict(item), "media_index": index}) for index, item in enumerate(result)], (
            sum(1 for item in items if str(item.get("tag")) == "img"),
            sum(1 for item in items if str(item.get("tag")) == "video"),
        )

    @staticmethod
    def _dom_item_belongs_to_post(item: dict[str, Any], href: str, post_url: str) -> bool:
        path = urlparse(href).path.lower()
        if "/p/" in path or "/reel/" in path or "/tv/" in path:
            return True
        if not item.get("article"):
            return False
        alt = str(item.get("alt") or "").casefold()
        return not any(token in alt for token in ("profile picture", "头像", "avatar", "logo"))

    @classmethod
    def _scan_carousel(
        cls,
        page: Page,
        post_url: str,
        current: list[InstagramMedia],
        on_media: Callable[[list[InstagramMedia]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        should_pause: Callable[[], None] | None = None,
        should_finish: Callable[[], bool] | None = None,
    ) -> tuple[list[InstagramMedia], int]:
        result = list(current)
        clicked = 0
        seen = {_media_dedupe_key(item) for item in result}
        stopped = should_stop or (lambda: False)
        pause = should_pause or (lambda: None)
        finish = should_finish or (lambda: False)
        for _ in range(cls.MAX_CAROUSEL_SLIDES):
            if stopped() or finish():
                break
            pause()
            if stopped() or finish():
                break
            try:
                locator = page.locator("button[aria-label*='Next'], button[aria-label*='next'], button[aria-label*='下一']")
                if locator.count() == 0:
                    break
                locator.first.click(timeout=1500)
                clicked += 1
                page.wait_for_timeout(250)
                items, _ = cls._media_from_dom(page, post_url)
            except Exception:
                break
            fresh = [item for item in items if _media_dedupe_key(item) not in seen]
            if not fresh:
                break
            for item in fresh:
                seen.add(_media_dedupe_key(item))
                result.append(item)
            if on_media:
                on_media(fresh)
            if stopped() or finish():
                break
        return [InstagramMedia(**{**asdict(item), "media_index": index}) for index, item in enumerate(result)], clicked

    @staticmethod
    def _merge_media(first: list[InstagramMedia], second: list[InstagramMedia]) -> list[InstagramMedia]:
        result = list(first)
        seen = {_media_dedupe_key(item) for item in result}
        for item in second:
            if _media_dedupe_key(item) not in seen:
                seen.add(_media_dedupe_key(item))
                result.append(item)
        return [InstagramMedia(**{**asdict(item), "media_index": index}) for index, item in enumerate(result)]

    @staticmethod
    def _merge_diagnostics(target: InstagramScanDiagnostics, source: InstagramScanDiagnostics) -> None:
        for field in ("img_count", "video_count", "candidate_image_count", "candidate_video_count", "author_match_count", "carousel_next_count"):
            setattr(target, field, getattr(target, field) + getattr(source, field))

    @staticmethod
    def _candidates_from_metadata(metadata: InstagramPostMetadata | None) -> list[ImageCandidate]:
        if metadata is None:
            return []
        result: list[ImageCandidate] = []
        for item in metadata.media:
            identity = media_identity(metadata.shortcode, item.media_index)
            result.append(
                ImageCandidate(
                    image_url=item.media_url if item.media_type == "image" else "",
                    pin_url=metadata.post_url,
                    source_page_url=metadata.post_url,
                    post_url=metadata.post_url,
                    platform="instagram",
                    author_username=metadata.author_username,
                    media_type=item.media_type,
                    media_url=item.media_url,
                    media_identity=identity,
                    duration=item.duration,
                    video_width=item.width if item.media_type == "video" else None,
                    video_height=item.height if item.media_type == "video" else None,
                    post_id=metadata.post_id,
                    post_text=metadata.caption,
                    posted_at=metadata.posted_at or "",
                    width=item.width,
                    height=item.height,
                    shortcode=metadata.shortcode,
                    caption=metadata.caption,
                    media_index=item.media_index,
                    thumbnail_url=item.thumbnail_url,
                    metadata_source=metadata.metadata_source,
                    source=InstagramCollector._source_type(item.source) or item.source,
                )
            )
        return result

    def _log_diagnostic_if_needed(self, diagnostics: InstagramScanDiagnostics, failed: bool) -> None:
        if failed:
            self.log(f"Instagram diagnostic: {diagnostics.summary()}; video_sources={sorted(self.last_video_source_types)}")

    def _save_debug_snapshot(self, page: Page, diagnostics: InstagramScanDiagnostics) -> None:
        if not self.debug_dir:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / "instagram_diagnostic.json").write_text(json.dumps(diagnostics.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(self.debug_dir / "instagram_diagnostic.png"), full_page=False)
        except Exception:
            pass
