"""yt-dlp backed public YouTube discovery and download adapter."""
from __future__ import annotations

import hashlib
import logging
import time
from urllib.parse import urlencode
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from src.browser.collector import ImageCandidate
from src.utils.runtime import find_deno, find_ffmpeg
from src.utils.youtube import youtube_url_kind, youtube_video_id


logger = logging.getLogger(__name__)


class YouTubeError(RuntimeError):
    pass


class YouTubeCancelled(YouTubeError):
    pass


@dataclass(frozen=True)
class YouTubeDownload:
    path: Path
    info: dict


class YouTubeAdapter:
    """Keep yt-dlp isolated from the UI and expose common candidates to Engine."""

    FRAGMENT_PROTOCOLS = frozenset({"m3u8", "m3u8_native", "http_dash_segments", "dash", "ism", "f4m"})
    FRAGMENT_CONCURRENCY = 4

    def __init__(self, ydl_factory=None) -> None:
        self._ydl_factory = ydl_factory

    def _ydl(self, options: dict):
        if self._ydl_factory:
            return self._ydl_factory(options)
        try:
            import yt_dlp
        except ImportError as exc:
            raise YouTubeError("缺少 yt-dlp 运行依赖，请重新安装或打包程序。") from exc
        return yt_dlp.YoutubeDL(options)

    @staticmethod
    def _ejs_options() -> dict:
        deno = find_deno()
        if not deno:
            raise YouTubeError("YouTube JavaScript challenge solver 需要 Deno runtime。")
        try:
            import yt_dlp_ejs  # noqa: F401 -- force packaged dependency validation
        except ImportError as exc:
            raise YouTubeError("缺少 yt-dlp-ejs，无法加载 YouTube JavaScript challenge solver。") from exc
        return {"js_runtimes": {"deno": {"path": deno}}}

    @staticmethod
    def url_kind(url: str) -> str:
        return youtube_url_kind(url)

    def entries(self, url: str, *, cookie_source: str = "none", browser_profile: str = "Default", cookie_file: str = "") -> Iterable[ImageCandidate]:
        """Enumerate playlists/channels lazily enough for task-level target control."""
        options = {"quiet": True, "skip_download": True, "extract_flat": "in_playlist", "noplaylist": False}
        options.update(self._ejs_options())
        options.update(self._cookie_options(cookie_source, browser_profile, cookie_file))
        try:
            with self._ydl(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:
            cookie_error = self._cookie_error(cookie_source, str(exc))
            if cookie_error:
                raise cookie_error from exc
            if "sign in to confirm" in str(exc).lower() or "not a bot" in str(exc).lower():
                raise YouTubeError("YouTube 当前要求登录验证。请在 YouTube 设置中选择 Firefox Cookies 或 cookies.txt 后重试。") from exc
            raise YouTubeError(str(exc)) from exc
        raw_entries = info.get("entries") if isinstance(info, dict) else None
        for entry in raw_entries or [info]:
            candidate = self._candidate_from_entry(entry, url, youtube_url_kind(url))
            if candidate:
                yield candidate

    @staticmethod
    def build_search_source(keyword: str, content_type: str, limit: int) -> str:
        """Build a yt-dlp source that asks YouTube for the requested type."""
        if content_type == "all":
            return f"ytsearch{limit}:{keyword}"
        params = {
            "shorts": "EgIQCQ==",  # Current YouTube Type=Shorts filter
            "video": "EgIQAQ==",   # Current YouTube Type=Videos filter
        }
        try:
            sp = params[content_type]
        except KeyError as exc:
            raise ValueError(f"unsupported YouTube search content type: {content_type}") from exc
        return f"https://www.youtube.com/results?{urlencode({'search_query': keyword, 'sp': sp})}"

    def search(
        self, keyword: str, limit: int, *, content_type: str = "all", cookie_source: str = "none", browser_profile: str = "Default", cookie_file: str = "",
    ) -> Iterable[ImageCandidate]:
        """Use yt-dlp's official ``ytsearchN:`` extractor in flat mode."""
        if limit < 1:
            return
        source = self.build_search_source(keyword, content_type, limit)
        options = {
            "quiet": True, "skip_download": True, "extract_flat": "in_playlist",
            "noplaylist": False, "playlistend": limit,
        }
        options.update(self._ejs_options())
        options.update(self._cookie_options(cookie_source, browser_profile, cookie_file))

        def extract_candidates(search_options: dict) -> list[ImageCandidate]:
            try:
                with self._ydl(search_options) as ydl:
                    info = ydl.extract_info(source, download=False)
            except Exception as exc:
                cookie_error = self._cookie_error(cookie_source, str(exc))
                if cookie_error:
                    raise cookie_error from exc
                raise YouTubeError(str(exc)) from exc
            entries = info.get("entries") if isinstance(info, dict) else None
            logger.debug(
                "YouTube search source=%s info_type=%s playlist_type=%s entries_type=%s entries_is_none=%s",
                source, type(info).__name__, info.get("_type") if isinstance(info, dict) else None,
                type(entries).__name__ if entries is not None else None, entries is None,
            )
            if entries is None:
                return []
            candidates: list[ImageCandidate] = []
            for index, entry in enumerate(entries, start=1):
                candidate = self._candidate_from_entry(entry, f"ytsearch:{keyword}", "youtube_search", keyword)
                if candidate:
                    candidates.append(candidate)
                    if index <= 10:
                        logger.debug("YouTube search entry [%d] accepted id=%s url=%s", index, candidate.post_id, candidate.post_url)
                elif index <= 10:
                    logger.debug("YouTube search entry [%d] rejected: no canonical video id", index)
            return candidates

        try:
            candidates = extract_candidates(options)
            # Authenticated YouTube search_url responses may contain an empty
            # search shelf for public Shorts/Videos. Discovery does not need
            # credentials, so retry the identical source anonymously once;
            # download and metadata requests continue using the user's cookies.
            if not candidates and content_type != "all" and cookie_source != "none":
                logger.warning("YouTube filtered search returned no candidates with %s cookies; retrying public discovery", cookie_source)
                public_options = {
                    "quiet": True, "skip_download": True, "extract_flat": "in_playlist",
                    "noplaylist": False, "playlistend": limit, **self._ejs_options(),
                }
                candidates = extract_candidates(public_options)
        except YouTubeError:
            raise
        for candidate in candidates:
            yield candidate

    def metadata(
        self, candidate: ImageCandidate, *, cookie_source: str = "none", browser_profile: str = "Default", cookie_file: str = "",
    ) -> dict:
        """Fetch only the fields needed for pre-download search filters."""
        options = {"quiet": True, "skip_download": True, "noplaylist": True}
        options.update(self._ejs_options())
        options.update(self._cookie_options(cookie_source, browser_profile, cookie_file))
        try:
            with self._ydl(options) as ydl:
                info = ydl.extract_info(candidate.media_url, download=False)
        except Exception as exc:
            raise YouTubeError(str(exc)) from exc
        return info if isinstance(info, dict) else {}

    @staticmethod
    def _candidate_from_entry(entry: dict | None, source_url: str, source_type: str, search_keyword: str = "") -> ImageCandidate | None:
        if not isinstance(entry, dict):
            return None
        raw_id = str(entry.get("id") or "").strip()
        webpage_url = str(entry.get("webpage_url") or entry.get("original_url") or entry.get("url") or "")
        video_id = youtube_video_id(f"https://youtu.be/{raw_id}") if raw_id else ""
        if not video_id:
            video_id = youtube_video_id(webpage_url)
        if not video_id:
            return None
        # Flat ytsearch entries can expose only an id, a relative URL, or a
        # youtu.be/Shorts URL. Keep a single canonical watch URL for all forms.
        webpage_url = f"https://www.youtube.com/watch?v={video_id}"
        return ImageCandidate(
            image_url="", pin_url=webpage_url, source_page_url=source_url, post_url=webpage_url,
            platform="youtube", media_type="video", media_url=webpage_url,
            media_identity=video_id, post_id=video_id, post_text=str(entry.get("title") or ""),
            author_username=str(entry.get("channel") or entry.get("uploader") or ""),
            thumbnail_url=str(entry.get("thumbnail") or ""), duration=entry.get("duration"),
            video_width=entry.get("width"), video_height=entry.get("height"),
            posted_at=str(entry.get("upload_date") or ""), source_type=source_type, source=search_keyword,
        )

    @staticmethod
    def _format(quality: str, media_type: str) -> str:
        if media_type == "audio":
            return "bestaudio/best"
        if media_type == "thumbnail":
            return "best"
        suffix = "" if quality == "best" else f"[height<={quality}]"
        return f"bestvideo{suffix}+bestaudio/best{suffix}/best"

    @staticmethod
    def _cookie_options(source: str, browser_profile: str = "Default", cookie_file: str = "") -> dict:
        if source == "none":
            return {}
        if source == "chrome":
            return {"cookiesfrombrowser": ("chrome", browser_profile)}
        if source == "firefox":
            return {"cookiesfrombrowser": ("firefox",)}
        if source == "file":
            path = Path(cookie_file)
            if not path.is_file():
                raise YouTubeError("cookies.txt 文件不存在或无法打开。")
            return {"cookiefile": str(path)}
        raise YouTubeError("未知的 YouTube Cookies 来源。")

    @staticmethod
    def _cookie_error(source: str, message: str) -> YouTubeError | None:
        lowered = message.lower()
        if source == "chrome" and "dpapi" in lowered:
            return YouTubeError("YouTube Chrome Cookies 无法解密（DPAPI）。请尝试 Firefox Cookies 或导入 cookies.txt。")
        if source == "chrome" and ("cookie" in lowered or "chrome" in lowered):
            return YouTubeError(f"YouTube Chrome Cookies 读取失败：{message}")
        if source == "firefox" and ("profile" in lowered or "firefox" in lowered or "cookie" in lowered):
            return YouTubeError(f"未检测到 Firefox 或 Firefox Profile，或 Cookies 读取失败：{message}")
        if source == "file" and ("cookie" in lowered or "netscape" in lowered):
            return YouTubeError(f"cookies.txt 无法读取或格式无效：{message}")
        return None

    def download(
        self, candidate: ImageCandidate, output_dir: Path, index: int, *, media_type: str,
        quality: str, audio_format: str, should_stop: Callable[[], bool],
        should_pause: Callable[[], None], on_log: Callable[[str], None],
        cookie_source: str = "none", browser_profile: str = "Default", cookie_file: str = "",
    ) -> YouTubeDownload:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise YouTubeError("未找到 FFmpeg，无法下载或合并 YouTube 视频。")
        output_dir.mkdir(parents=True, exist_ok=True)
        def progress(status: dict) -> None:
            if should_stop():
                raise YouTubeCancelled("用户已停止任务")
            should_pause()
            if should_stop():
                raise YouTubeCancelled("用户已停止任务")
            if status.get("status") == "downloading":
                percent = str(status.get("_percent_str") or "").strip()
                speed = str(status.get("_speed_str") or "").strip()
                eta = str(status.get("_eta_str") or "").strip()
                if percent:
                    on_log(f"YouTube 下载进度：{percent} {speed} ETA {eta}".strip())

        def postprocess(status: dict) -> None:
            if should_stop():
                raise YouTubeCancelled("用户已停止任务")
            should_pause()
            if should_stop():
                raise YouTubeCancelled("用户已停止任务")

        format_selector = self._format(quality, media_type)
        selection_options = {
            "quiet": True, "skip_download": True, "noplaylist": True, "format": format_selector,
        }
        selection_options.update(self._ejs_options())
        selection_options.update(self._cookie_options(cookie_source, browser_profile, cookie_file))
        try:
            with self._ydl(selection_options) as ydl:
                selected_info = ydl.extract_info(candidate.media_url, download=False)
        except Exception as exc:
            message = str(exc)
            cookie_error = self._cookie_error(cookie_source, message)
            if cookie_error:
                raise cookie_error from exc
            if "sign in to confirm" in message.lower() or "not a bot" in message.lower():
                raise YouTubeError("YouTube 当前要求登录验证。请在 YouTube 设置中选择 Firefox Cookies 或 cookies.txt 后重试。") from exc
            raise YouTubeError(message) from exc
        if not isinstance(selected_info, dict):
            raise YouTubeError("yt-dlp 未返回可下载的视频信息")

        fragment_concurrency = self._fragment_concurrency(selected_info)
        on_log(self._download_plan_log(selected_info, media_type, fragment_concurrency))
        template = str(output_dir / f"{index:04d}_%(title).120B [%(id)s].%(ext)s")
        options = {
            "quiet": True, "noplaylist": True, "format": format_selector,
            "outtmpl": template, "windowsfilenames": True, "noprogress": True,
            "progress_hooks": [progress], "ffmpeg_location": str(Path(ffmpeg).parent),
            "merge_output_format": "mp4", "retries": 3, "fragment_retries": 3,
            "socket_timeout": 20, "extractor_retries": 2, "postprocessor_hooks": [postprocess],
        }
        if fragment_concurrency > 1:
            # yt-dlp only uses this setting for manifest/fragment protocols.
            # Plain HTTPS media must remain a single request, where fragment
            # concurrency provides no throughput benefit.
            options["concurrent_fragment_downloads"] = fragment_concurrency
        options.update(self._ejs_options())
        options.update(self._cookie_options(cookie_source, browser_profile, cookie_file))
        if cookie_source == "chrome": on_log(f"正在从 Chrome Profile {browser_profile} 读取 YouTube Cookies。")
        elif cookie_source == "firefox": on_log("正在从 Firefox 默认 Profile 读取 YouTube Cookies。")
        elif cookie_source == "file": on_log(f"正在加载 cookies.txt：{Path(cookie_file).name}")
        if media_type == "audio":
            options["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": audio_format if audio_format != "best" else "m4a"}]
        elif media_type == "thumbnail":
            options.update({"skip_download": True, "writethumbnail": True})
        try:
            with self._ydl(options) as ydl:
                info = ydl.extract_info(candidate.media_url, download=True)
        except YouTubeCancelled:
            raise
        except Exception as exc:
            message = str(exc)
            cookie_error = self._cookie_error(cookie_source, message)
            if cookie_error:
                raise cookie_error from exc
            if "sign in to confirm" in message.lower() or "not a bot" in message.lower():
                raise YouTubeError("YouTube 当前要求登录验证。请在 YouTube 设置中选择 Firefox Cookies 或 cookies.txt 后重试。") from exc
            if "is live" in message.lower():
                raise YouTubeError("live stream not supported in YouTube Phase 1") from exc
            raise YouTubeError(message) from exc
        output_path = self._find_completed_file(info if isinstance(info, dict) else {}, output_dir, candidate.post_id)
        if output_path is None:
            raise YouTubeError("yt-dlp 未生成输出文件")
        return YouTubeDownload(output_path, info if isinstance(info, dict) else {})

    @classmethod
    def _fragment_concurrency(cls, info: dict) -> int:
        """Return a conservative count only for formats yt-dlp downloads as fragments."""
        for item in cls._selected_formats(info):
            protocol = str(item.get("protocol") or "").lower()
            if item.get("fragments") or protocol in cls.FRAGMENT_PROTOCOLS:
                return cls.FRAGMENT_CONCURRENCY
        return 1

    @staticmethod
    def _selected_formats(info: dict) -> list[dict]:
        requested = info.get("requested_formats")
        if isinstance(requested, list) and requested:
            return [item for item in requested if isinstance(item, dict)]
        return [info]

    @staticmethod
    def _format_size(size: int | float | None, approximate: bool = False) -> str:
        if not size:
            return "未知"
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        value = float(size)
        unit = units[0]
        for unit in units:
            if value < 1024 or unit == units[-1]:
                break
            value /= 1024
        prefix = "约 " if approximate else ""
        return f"{prefix}{value:.2f} {unit}"

    @classmethod
    def _download_plan_log(cls, info: dict, requested_media_type: str, fragment_concurrency: int) -> str:
        formats = cls._selected_formats(info)
        sizes = [item.get("filesize") or item.get("filesize_approx") for item in formats]
        approximate = any(not item.get("filesize") and item.get("filesize_approx") for item in formats)
        protocols = "+".join(dict.fromkeys(str(item.get("protocol") or "unknown") for item in formats))
        format_ids = "+".join(str(item.get("format_id") or "unknown") for item in formats)
        height = next((item.get("height") for item in formats if item.get("height")), info.get("height"))
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            duration_text = time.strftime("%H:%M:%S", time.gmtime(duration))
        else:
            duration_text = "未知"
        media_kind = str(info.get("media_type") or requested_media_type or "video")
        resolution = f"{height}p" if height else ("音频" if requested_media_type == "audio" else "未知")
        return (
            f"YouTube 下载信息：标题={str(info.get('title') or '未知')}；时长={duration_text}；"
            f"媒体类型={media_kind}；分辨率={resolution}；估计大小={cls._format_size(sum(size for size in sizes if isinstance(size, (int, float))), approximate)}；"
            f"格式={format_ids}；协议={protocols}；分片并发={fragment_concurrency}"
        )

    @staticmethod
    def _find_completed_file(info: dict, output_dir: Path, video_id: str) -> Path | None:
        """Return a real final output, not a deleted audio/video merge input."""
        possible_paths: list[Path] = []
        for key in ("filepath", "_filename"):
            value = info.get(key)
            if value:
                possible_paths.append(Path(str(value)))
        for key in ("requested_downloads", "requested_formats"):
            for item in info.get(key) or []:
                if isinstance(item, dict) and item.get("filepath"):
                    possible_paths.append(Path(str(item["filepath"])))

        # yt-dlp may leave the info dictionary pointing at an intermediate
        # stream after FFmpeg merges it. Prefer a path only when it survived.
        for path in possible_paths:
            if path.is_file() and path.stat().st_size > 0:
                return path
        completed = [
            path for path in output_dir.glob(f"*{video_id}*")
            if path.is_file() and path.stat().st_size > 0 and ".part" not in path.name
        ]
        if not completed:
            return None
        return max(completed, key=lambda path: path.stat().st_mtime)

    @staticmethod
    def file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
