"""yt-dlp backed public YouTube discovery and download adapter."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable, Iterable

from src.browser.collector import ImageCandidate
from src.utils.runtime import find_deno, find_ffmpeg
from src.utils.youtube import youtube_url_kind


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
        entries = raw_entries or [info]
        for entry in entries:
            if not entry:
                continue
            video_id = str(entry.get("id") or "")
            webpage_url = str(entry.get("webpage_url") or entry.get("url") or "")
            if webpage_url and not webpage_url.startswith("http") and video_id:
                webpage_url = f"https://www.youtube.com/watch?v={video_id}"
            if not video_id or not webpage_url:
                continue
            yield ImageCandidate(
                image_url="", pin_url=webpage_url, source_page_url=url, post_url=webpage_url,
                platform="youtube", media_type="video", media_url=webpage_url,
                media_identity=video_id, post_id=video_id, post_text=str(entry.get("title") or ""),
                author_username=str(entry.get("channel") or entry.get("uploader") or ""),
                thumbnail_url=str(entry.get("thumbnail") or ""), duration=entry.get("duration"),
                video_width=entry.get("width"), video_height=entry.get("height"),
                source_type=youtube_url_kind(url),
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

        template = str(output_dir / f"{index:04d}_%(title).120B [%(id)s].%(ext)s")
        options = {
            "quiet": True, "noplaylist": True, "format": self._format(quality, media_type),
            "outtmpl": template, "windowsfilenames": True, "noprogress": True,
            "progress_hooks": [progress], "ffmpeg_location": str(Path(ffmpeg).parent),
            "merge_output_format": "mp4", "retries": 3, "fragment_retries": 3,
            "socket_timeout": 20, "extractor_retries": 2, "postprocessor_hooks": [postprocess],
        }
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
