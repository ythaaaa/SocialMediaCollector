from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from src.browser.collector import ImageCandidate, PinterestVideoSource
from src.browser.instagram_collector import media_identity
from src.downloader.dash import parse_mpd, select_best_audio, select_best_video
from src.adapters.instagram_cookie_bridge import InstagramAuthRequired, load_instagram_cookies
from src.adapters.pinterest_cookie_bridge import PinterestAuthRequired, load_pinterest_cookies
from src.utils.paths import profile_directory_for_platform
from src.utils.instagram import normalize_hashtag
from src.utils.runtime import runtime_root, subprocess_creation_flags


LEGACY_ENVIRONMENT_VARIABLE = "SMC_INSTAGRAM_LEGACY"
GALLERYDL_PYTHON_ENVIRONMENT_VARIABLE = "SMC_GALLERYDL_PYTHON"
GALLERYDL_HOME_ENVIRONMENT_VARIABLE = "SMC_GALLERYDL_HOME"
DEFAULT_GALLERY_DL_ROOT = Path.home() / "Desktop" / "gallery-dl"
PACKAGED_GALLERYDL_HELPER_DIRECTORY = Path("helpers") / "instagram-gallerydl-helper"
PACKAGED_GALLERYDL_HELPER_EXECUTABLE = "instagram-gallerydl-helper.exe"


class GalleryDlError(RuntimeError):
    """The isolated gallery-dl helper reported an extraction/process error."""


class GalleryDlStopped(RuntimeError):
    """The caller requested that the isolated gallery-dl helper stop."""


def _gallerydl_python_for_root(root: Path, *, windows: bool | None = None) -> Path:
    windows = os.name == "nt" if windows is None else windows
    executable = "python.exe" if windows else "python"
    scripts = "Scripts" if windows else "bin"
    return Path(root) / ".venv" / scripts / executable


def _windows_desktop_directory() -> Path | None:
    """Return the actual Windows Desktop, including redirected Known Folders."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        desktop_id = GUID(
            0xB4BFCC3A,
            0xDB2C,
            0x424C,
            (wintypes.BYTE * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        shell32 = ctypes.windll.shell32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(GUID),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_wchar_p),
        ]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        path_pointer = ctypes.c_wchar_p()
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(desktop_id), 0, None, ctypes.byref(path_pointer)
        )
        if result != 0 or not path_pointer.value:
            return None
        try:
            return Path(path_pointer.value)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(path_pointer)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _nearby_gallerydl_roots() -> tuple[Path, ...]:
    """Find gallery-dl roots placed beside the source project."""
    project_root = Path(__file__).resolve().parents[2]
    return (
        project_root.parent / "gallery-dl",
        project_root.parent.parent / "gallery-dl",
    )


def _gallerydl_not_found(checked: list[Path]) -> GalleryDlError:
    paths = "\n".join(f"- {path}" for path in checked)
    return GalleryDlError(f"gallery-dl helper not found\nchecked candidate paths:\n{paths}")


def resolve_packaged_gallerydl_helper(
    *,
    runtime_directory: Path | None = None,
) -> Path | None:
    """Return the onedir helper shipped with a packaged SMC build, if present."""
    root = Path(runtime_root() if runtime_directory is None else runtime_directory)
    helper = root / PACKAGED_GALLERYDL_HELPER_DIRECTORY / PACKAGED_GALLERYDL_HELPER_EXECUTABLE
    return helper if helper.is_file() else None


def resolve_gallerydl_python(
    environ: Mapping[str, str] | None = None,
    *,
    desktop_directory: Path | None = None,
    home_directory: Path | None = None,
) -> Path:
    """Resolve the isolated gallery-dl Python executable without machine paths.

    The optional directory arguments are dependency-injection points for tests;
    normal callers should only provide ``environ`` when they need custom env.
    """
    values = os.environ if environ is None else environ
    home = Path.home() if home_directory is None else Path(home_directory)
    checked: list[Path] = []
    seen: set[Path] = set()

    def candidate(path: Path) -> Path | None:
        path = Path(path)
        if path not in seen:
            seen.add(path)
            checked.append(path)
        return path if path.is_file() else None

    explicit_python = str(values.get(GALLERYDL_PYTHON_ENVIRONMENT_VARIABLE, "")).strip()
    if explicit_python:
        resolved = candidate(Path(explicit_python))
        if resolved:
            return resolved

    explicit_home = str(values.get(GALLERYDL_HOME_ENVIRONMENT_VARIABLE, "")).strip()
    if explicit_home:
        resolved = candidate(_gallerydl_python_for_root(Path(explicit_home)))
        if resolved:
            return resolved

    desktop = _windows_desktop_directory() if desktop_directory is None else Path(desktop_directory)
    if desktop:
        resolved = candidate(_gallerydl_python_for_root(desktop / "gallery-dl"))
        if resolved:
            return resolved

    for root in _nearby_gallerydl_roots():
        resolved = candidate(_gallerydl_python_for_root(root))
        if resolved:
            return resolved

    resolved = candidate(_gallerydl_python_for_root(home / "Desktop" / "gallery-dl"))
    if resolved:
        return resolved
    raise _gallerydl_not_found(checked)


def _gallerydl_root_from_python(python_executable: Path) -> Path:
    """Derive the gallery-dl checkout root from its isolated venv Python."""
    executable = Path(python_executable)
    if executable.parent.name.lower() in {"scripts", "bin"} and executable.parent.parent.name == ".venv":
        return executable.parent.parent.parent
    return DEFAULT_GALLERY_DL_ROOT


def _gallerydl_helper_environment() -> dict[str, str]:
    """Force the child Python standard streams to use UTF-8 on Windows."""
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def instagram_legacy_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether the legacy Instagram browser collector is requested."""
    values = os.environ if environ is None else environ
    return values.get(LEGACY_ENVIRONMENT_VARIABLE, "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _media_index(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _dash_sources(payload: dict[str, Any], width: int | None, height: int | None, duration: float | None, bitrate: int | None) -> tuple[PinterestVideoSource, ...]:
    raw_manifest = str(payload.get("dash_manifest") or "").strip()
    if not raw_manifest:
        return ()
    try:
        manifest = parse_mpd(raw_manifest)
        video = select_best_video(manifest.video)
        audio = select_best_audio(manifest.audio)
        if not video or not video.base_url:
            return ()
        return (
            PinterestVideoSource(
                url=video.base_url,
                width=video.width or width,
                height=video.height or height,
                duration=video.duration or manifest.duration or duration,
                bitrate=video.bandwidth or bitrate,
                kind="dash",
                representation_id=video.representation_id,
                codec=video.codec,
                mime_type=video.mime_type,
                audio_url=audio.base_url if audio else "",
                audio_codec=audio.codec if audio else "",
                audio_bitrate=audio.bandwidth if audio else None,
            ),
        )
    except (ValueError, TypeError):
        return ()


def candidate_from_gallerydl(payload: dict[str, Any]) -> ImageCandidate:
    """Map one safe, normalized helper payload to SMC's existing candidate type."""
    if str(payload.get("platform") or "instagram").casefold() == "pinterest":
        return _pinterest_candidate_from_gallerydl(payload)
    media_type = str(payload.get("media_type") or "image").lower()
    if media_type not in {"image", "video"}:
        raise GalleryDlError(f"unsupported Instagram media_type: {media_type}")

    shortcode = str(payload.get("shortcode") or "")
    index = _media_index(payload.get("media_index"))
    post_url = str(payload.get("post_url") or payload.get("url") or "")
    display_url = str(payload.get("display_url") or "")
    media_url = str(
        payload.get("media_url")
        or payload.get("video_url")
        or display_url
    )
    width = _int_or_none(payload.get("width"))
    height = _int_or_none(payload.get("height"))
    duration = _float_or_none(payload.get("duration"))
    bitrate = _int_or_none(payload.get("bitrate"))
    identity = str(payload.get("media_identity") or media_identity(shortcode, index))

    video_sources: tuple[PinterestVideoSource, ...] = ()
    for source_data in payload.get("video_sources", ()):
        if not isinstance(source_data, dict) or not source_data.get("url"):
            continue
        video_sources += (
            PinterestVideoSource(
                url=str(source_data["url"]),
                width=_int_or_none(source_data.get("width")) or width,
                height=_int_or_none(source_data.get("height")) or height,
                duration=_float_or_none(source_data.get("duration")) or duration,
                bitrate=_int_or_none(source_data.get("bitrate")) or bitrate,
                kind=str(source_data.get("kind") or "dash"),
                representation_id=str(source_data.get("representation_id") or ""),
                codec=str(source_data.get("codec") or ""),
                mime_type=str(source_data.get("mime_type") or ""),
                audio_url=str(source_data.get("audio_url") or ""),
                audio_codec=str(source_data.get("audio_codec") or ""),
                audio_bitrate=_int_or_none(source_data.get("audio_bitrate")),
            ),
        )
    dash_url = str(payload.get("dash_url") or "")
    if dash_url and not video_sources:
        video_sources = (
            PinterestVideoSource(
                url=dash_url,
                width=width,
                height=height,
                duration=duration,
                bitrate=bitrate,
                kind="dash",
            ),
        )
    if not video_sources:
        video_sources = _dash_sources(payload, width, height, duration, bitrate)

    return ImageCandidate(
        image_url=display_url if media_type == "image" else "",
        pin_url=post_url,
        source_page_url=post_url,
        post_url=post_url,
        platform="instagram",
        author_username=str(payload.get("username") or ""),
        media_type=media_type,
        media_url=media_url,
        media_identity=identity,
        duration=duration,
        video_width=width if media_type == "video" else None,
        video_height=height if media_type == "video" else None,
        bitrate=bitrate,
        post_id=str(payload.get("post_id") or ""),
        post_text=str(payload.get("description") or payload.get("caption") or ""),
        posted_at=str(payload.get("date") or ""),
        fallback_urls=tuple(str(item) for item in payload.get("fallback_urls", ())),
        video_sources=video_sources,
        width=width,
        height=height,
        shortcode=shortcode,
        caption=str(payload.get("caption") or payload.get("description") or ""),
        media_index=index,
        thumbnail_url=str(payload.get("thumbnail_url") or display_url),
        metadata_source="gallery-dl",
        source="gallery-dl",
        hashtag=str(payload.get("hashtag") or payload.get("source_hashtag") or ""),
        source_hashtag=str(payload.get("source_hashtag") or payload.get("hashtag") or ""),
    )


def _pinterest_candidate_from_gallerydl(payload: dict[str, Any]) -> ImageCandidate:
    media_type = str(payload.get("media_type") or "image").lower()
    if media_type not in {"image", "video"}:
        raise GalleryDlError(f"unsupported Pinterest media_type: {media_type}")
    pin_id = str(payload.get("pin_id") or payload.get("post_id") or "")
    index = _media_index(payload.get("media_index"))
    post_url = str(payload.get("pin_url") or payload.get("post_url") or payload.get("url") or "")
    source_url = str(payload.get("source_url") or post_url)
    display_url = str(payload.get("display_url") or payload.get("thumbnail_url") or "")
    media_url = str(payload.get("media_url") or payload.get("video_url") or display_url)
    width = _int_or_none(payload.get("width"))
    height = _int_or_none(payload.get("height"))
    duration = _float_or_none(payload.get("duration"))
    bitrate = _int_or_none(payload.get("bitrate"))
    identity = str(payload.get("media_identity") or f"{pin_id}:{index}")
    video_sources: list[PinterestVideoSource] = []
    for source_data in payload.get("video_sources", ()):
        if not isinstance(source_data, dict) or not source_data.get("url"):
            continue
        video_sources.append(
            PinterestVideoSource(
                url=str(source_data["url"]),
                width=_int_or_none(source_data.get("width")) or width,
                height=_int_or_none(source_data.get("height")) or height,
                duration=_float_or_none(source_data.get("duration")) or duration,
                bitrate=_int_or_none(source_data.get("bitrate")) or bitrate,
                kind=str(source_data.get("kind") or ("hls" if ".m3u8" in str(source_data["url"]).casefold() else "mp4")),
            )
        )
    fallback_urls = tuple(str(item) for item in payload.get("fallback_urls", ()) if item)
    return ImageCandidate(
        image_url=display_url if media_type == "image" else "",
        pin_url=post_url,
        source_page_url=source_url,
        post_url=post_url,
        platform="pinterest",
        author_username=str(payload.get("username") or ""),
        media_type=media_type,
        media_url=media_url,
        media_identity=identity,
        duration=duration,
        video_width=width if media_type == "video" else None,
        video_height=height if media_type == "video" else None,
        bitrate=bitrate,
        post_id=pin_id,
        post_text=str(payload.get("description") or payload.get("closeup_description") or payload.get("caption") or ""),
        fallback_urls=fallback_urls,
        video_sources=tuple(video_sources),
        width=width,
        height=height,
        shortcode=pin_id,
        caption=str(payload.get("caption") or payload.get("description") or ""),
        media_index=index,
        thumbnail_url=str(payload.get("thumbnail_url") or display_url),
        metadata_source="gallery-dl",
        source="gallery-dl",
    )


class _WorkerClient(Protocol):
    def start(self) -> None: ...

    def send(self, command: str) -> None: ...

    def read_event(self, stop_event: threading.Event) -> dict[str, Any]: ...

    def stop(self) -> None: ...


class GalleryDlWorkerClient:
    """JSONL client for the isolated gallery-dl worker process."""

    def __init__(
        self,
        url: str,
        profile_path: Path,
        media_type: str = "mixed",
        *,
        python_executable: Path | None = None,
        helper_executable: Path | None = None,
        gallery_dl_root: Path | None = None,
        worker_script: Path | None = None,
        runtime_directory: Path | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        cookies: dict[str, str] | None = None,
        source_hashtag: str = "",
        platform: str = "instagram",
    ) -> None:
        self.url = url
        self.profile_path = Path(profile_path)
        self.media_type = media_type
        self.helper_executable: Path | None = None
        if helper_executable is not None:
            packaged_helper = Path(helper_executable)
            if not packaged_helper.is_file():
                raise GalleryDlError(f"packaged gallery-dl helper not found: {packaged_helper}")
            self.helper_executable = packaged_helper
            self.python_executable: Path | None = None
            self.gallery_dl_root: Path | None = None
        elif python_executable is None and gallery_dl_root is None:
            packaged_helper = resolve_packaged_gallerydl_helper(runtime_directory=runtime_directory)
            if packaged_helper is not None:
                self.helper_executable = packaged_helper
                self.python_executable = None
                self.gallery_dl_root = None
            else:
                self.python_executable = self._default_python()
                self.gallery_dl_root = _gallerydl_root_from_python(self.python_executable)
        elif python_executable is None:
            self.python_executable = _gallerydl_python_for_root(Path(gallery_dl_root))
            if not self.python_executable.is_file():
                raise _gallerydl_not_found([self.python_executable])
            self.gallery_dl_root = Path(gallery_dl_root)
        else:
            self.python_executable = Path(python_executable)
            if not self.python_executable.is_file():
                raise _gallerydl_not_found([self.python_executable])
            self.gallery_dl_root = Path(
                gallery_dl_root or _gallerydl_root_from_python(self.python_executable)
            )
        self.worker_script = Path(worker_script or Path(__file__).with_name("gallerydl_worker.py"))
        self.cookies = dict(cookies or {})
        self.source_hashtag = source_hashtag
        self.platform = (platform or "instagram").strip().lower()
        self._popen_factory = popen_factory
        self._process: Any | None = None
        self._events: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stdin_lock = threading.Lock()

    @staticmethod
    def _default_python() -> Path:
        return resolve_gallerydl_python()

    @property
    def process(self) -> Any | None:
        return self._process

    @property
    def command(self) -> list[str]:
        if self.helper_executable is not None:
            return [str(self.helper_executable)]
        if self.python_executable is None:
            raise GalleryDlError("gallery-dl helper command is unavailable")
        return [str(self.python_executable), str(self.worker_script)]

    @property
    def is_packaged_helper(self) -> bool:
        return self.helper_executable is not None

    def start(self) -> None:
        if self._process is not None:
            raise GalleryDlError("gallery-dl helper already started")
        try:
            self._process = self._popen_factory(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=_gallerydl_helper_environment(),
                creationflags=subprocess_creation_flags(),
            )
        except OSError as exc:
            raise GalleryDlError(f"unable to start gallery-dl helper: {exc}") from exc

        thread = threading.Thread(
            target=self._read_stdout,
            name="gallery-dl-worker-reader",
            daemon=True,
        )
        thread.start()
        stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="gallery-dl-worker-stderr",
            daemon=True,
        )
        stderr_thread.start()
        self._send_json(
            {
                "type": "start",
                "url": self.url,
                "profile_path": str(self.profile_path),
                "media_type": self.media_type,
                "gallery_dl_root": str(self.gallery_dl_root or ""),
                "cookies": self.cookies,
                "source_hashtag": self.source_hashtag,
                "platform": self.platform,
            }
        )

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._events.put({"type": "error", "message": "helper stdout unavailable"})
            return
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._events.put({"type": "error", "message": f"invalid helper JSON: {exc}"})
                    continue
                if isinstance(event, dict):
                    self._events.put(event)
        except (OSError, ValueError) as exc:
            self._events.put({"type": "error", "message": f"helper stdout failed: {exc}"})
        finally:
            self._events.put(None)

    def _read_stderr(self) -> None:
        """Drain helper diagnostics without allowing locale decoding to fail."""
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for _line in process.stderr:
                pass
        except (OSError, UnicodeError, ValueError):
            pass

    def _send_json(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise GalleryDlError("gallery-dl helper stdin unavailable")
        with self._stdin_lock:
            try:
                process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                raise GalleryDlError(f"gallery-dl helper stdin failed: {exc}") from exc

    def send(self, command: str) -> None:
        self._send_json({"type": command})

    def read_event(self, stop_event: threading.Event) -> dict[str, Any]:
        while True:
            if stop_event.is_set():
                raise GalleryDlStopped("gallery-dl helper stopped")
            try:
                event = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event is not None:
                return event
            process = self._process
            code = process.poll() if process is not None else None
            raise GalleryDlError(f"gallery-dl helper exited unexpectedly (code={code})")

    def stop(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    self.send("stop")
                except GalleryDlError:
                    pass
                self._terminate_process_tree(process)
        finally:
            self._process = None

    @staticmethod
    def _terminate_process_tree(process: Any) -> None:
        pid = getattr(process, "pid", None)
        if os.name == "nt" and pid:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                creationflags=subprocess_creation_flags(),
            )
        else:
            try:
                process.terminate()
            except (OSError, AttributeError):
                pass
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError, AttributeError):
            try:
                process.kill()
            except (OSError, AttributeError):
                pass
            try:
                process.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError, AttributeError):
                pass


@dataclass
class InstagramGalleryDlAdapter:
    """Opt-in gallery-dl adapter; the normal InstagramCollector remains the default."""

    profile_path: Path | None = None
    python_executable: Path | None = None
    helper_executable: Path | None = None
    gallery_dl_root: Path | None = None
    worker_script: Path | None = None
    cookies: dict[str, str] | None = None
    client_factory: Callable[..., _WorkerClient] = GalleryDlWorkerClient

    def profile(self) -> Path:
        return Path(self.profile_path or profile_directory_for_platform("instagram"))

    def create_client(self, url: str, media_type: str = "mixed", source_hashtag: str = "") -> _WorkerClient:
        return self.client_factory(
            url,
            self.profile(),
            media_type,
            python_executable=self.python_executable,
            helper_executable=self.helper_executable,
            gallery_dl_root=self.gallery_dl_root,
            worker_script=self.worker_script,
            cookies=self.cookies,
            source_hashtag=source_hashtag,
            platform="instagram",
        )

    def collect(
        self,
        url: str,
        *,
        media_type: str = "mixed",
        author_username: str | None = None,
        source_hashtag: str | None = None,
        stop_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        on_candidate: Callable[[ImageCandidate], bool | None],
        on_discovered: Callable[[ImageCandidate], None] | None = None,
        on_skipped: Callable[[ImageCandidate], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> int:
        """Stream candidates and let the SMC callback own final download/counting.

        ``pause_event`` is set while running and cleared while paused.  The
        helper does not receive ``next`` until the callback has finished, which
        also provides the required one-candidate backpressure.
        """
        stop_event = stop_event or threading.Event()
        if pause_event is None:
            pause_event = threading.Event()
            pause_event.set()
        if self.cookies is None:
            try:
                self.cookies = load_instagram_cookies()
            except InstagramAuthRequired:
                raise
        if not self.cookies.get("sessionid"):
            raise InstagramAuthRequired("Instagram cookie loaded=no; sessionid 未找到")
        requested_media_type = (media_type or "mixed").strip().lower()
        expected_username = (author_username or "").strip().lstrip("@").casefold()
        normalized_hashtag = normalize_hashtag(source_hashtag or url) if source_hashtag or "/explore/tags/" in url.casefold() else ""
        client = self.create_client(url, "mixed", normalized_hashtag)
        delivered = 0
        pause_sent = False
        try:
            client.start()
            ready = client.read_event(stop_event)
            if ready.get("type") != "ready":
                raise GalleryDlError(ready.get("message") or "gallery-dl helper did not become ready")
            cookie_loaded = bool(ready.get("cookie_loaded"))
            if on_log:
                version = str(ready.get("gallery_dl_version") or "").strip()
                engine = f"Instagram engine=gallery-dl{(' ' + version) if version else ''}"
                if bool(getattr(client, "is_packaged_helper", False)):
                    engine += "; helper=packaged"
                on_log(f"{engine}; cookie loaded={'yes' if cookie_loaded else 'no'}")
            if not cookie_loaded:
                raise GalleryDlError("Instagram cookie loaded=no; sessionid 未找到")

            while True:
                if stop_event.is_set():
                    raise GalleryDlStopped("gallery-dl helper stopped")
                if not pause_event.is_set():
                    if not pause_sent:
                        client.send("pause")
                        pause_sent = True
                    time.sleep(0.05)
                    continue
                if pause_sent:
                    client.send("resume")
                    pause_sent = False

                client.send("next")
                event = client.read_event(stop_event)
                event_type = event.get("type")
                if event_type == "exhausted":
                    break
                if event_type == "error":
                    if event.get("code") == "AUTH_REQUIRED":
                        raise InstagramAuthRequired("Instagram AUTH_REQUIRED")
                    raise GalleryDlError(str(event.get("message") or "gallery-dl extraction failed"))
                if event_type != "candidate":
                    raise GalleryDlError(f"unexpected gallery-dl event: {event_type}")

                candidate = candidate_from_gallerydl(event)
                if on_discovered:
                    on_discovered(candidate)
                if expected_username and candidate.author_username.casefold() != expected_username:
                    if on_skipped:
                        on_skipped(candidate)
                    if on_log:
                        on_log("Instagram candidate skipped: author ownership mismatch")
                    continue
                if requested_media_type != "mixed" and candidate.media_type != requested_media_type:
                    if on_skipped:
                        on_skipped(candidate)
                    if on_log:
                        on_log(f"Instagram candidate skipped: media_type={candidate.media_type}")
                    continue
                delivered += 1
                if on_log:
                    on_log("Instagram candidate discovered")
                result = on_candidate(candidate)
                if on_log and result is not False:
                    on_log("Instagram candidate accepted")
                if result is False:
                    break
            return delivered
        except GalleryDlStopped:
            return delivered
        finally:
            client.stop()


@dataclass
class PinterestGalleryDlAdapter:
    """Pinterest Search/Pin stream using the same isolated helper lifecycle."""

    profile_path: Path | None = None
    python_executable: Path | None = None
    helper_executable: Path | None = None
    gallery_dl_root: Path | None = None
    worker_script: Path | None = None
    cookies: dict[str, str] | None = None
    client_factory: Callable[..., _WorkerClient] = GalleryDlWorkerClient

    def profile(self) -> Path:
        return Path(self.profile_path or profile_directory_for_platform("pinterest"))

    @staticmethod
    def _is_search_url(url: str) -> bool:
        return "/search/pins/" in str(url or "").casefold()

    def create_client(self, url: str, media_type: str = "mixed") -> _WorkerClient:
        return self.client_factory(
            url,
            self.profile(),
            media_type,
            python_executable=self.python_executable,
            helper_executable=self.helper_executable,
            gallery_dl_root=self.gallery_dl_root,
            worker_script=self.worker_script,
            cookies=self.cookies or {},
            platform="pinterest",
        )

    def collect(
        self,
        url: str,
        *,
        media_type: str = "mixed",
        stop_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
        on_candidate: Callable[[ImageCandidate], bool | None],
        on_discovered: Callable[[ImageCandidate], None] | None = None,
        on_skipped: Callable[[ImageCandidate], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> int:
        stop_event = stop_event or threading.Event()
        if pause_event is None:
            pause_event = threading.Event()
            pause_event.set()
        requested_media_type = (media_type or "mixed").strip().lower()
        is_search = self._is_search_url(url)
        if is_search:
            if self.cookies is None:
                self.cookies = load_pinterest_cookies()
            if not self.cookies:
                raise PinterestAuthRequired("Pinterest Search requires login; 请先打开浏览器 / 登录并同步 Cookie。")
        elif self.cookies is None:
            # Single Pin remains usable for public/anonymous Pins.  It does
            # not require the Pinterest Cookie Bridge.
            self.cookies = {}
        client = self.create_client(url, "mixed")
        delivered = 0
        pause_sent = False
        try:
            client.start()
            ready = client.read_event(stop_event)
            if ready.get("type") != "ready":
                raise GalleryDlError(ready.get("message") or "gallery-dl helper did not become ready")
            version = str(ready.get("gallery_dl_version") or "").strip()
            engine = f"Pinterest engine=gallery-dl{(' ' + version) if version else ''}"
            if bool(getattr(client, "is_packaged_helper", False)):
                engine += "; helper=packaged"
            if on_log:
                on_log(f"{engine}; cookie loaded={'yes' if ready.get('cookie_loaded') else 'no'}")

            while True:
                if stop_event.is_set():
                    raise GalleryDlStopped("gallery-dl helper stopped")
                if not pause_event.is_set():
                    if not pause_sent:
                        client.send("pause")
                        pause_sent = True
                    time.sleep(0.05)
                    continue
                if pause_sent:
                    client.send("resume")
                    pause_sent = False
                client.send("next")
                event = client.read_event(stop_event)
                event_type = event.get("type")
                if event_type == "exhausted":
                    break
                if event_type == "error":
                    if event.get("code") == "AUTH_REQUIRED":
                        raise PinterestAuthRequired("Pinterest Search requires login; 请先打开浏览器 / 登录并同步 Cookie。")
                    raise GalleryDlError(str(event.get("message") or "gallery-dl extraction failed"))
                if event_type != "candidate":
                    raise GalleryDlError(f"unexpected gallery-dl event: {event_type}")
                candidate = candidate_from_gallerydl(event)
                if on_discovered:
                    on_discovered(candidate)
                if requested_media_type != "mixed" and candidate.media_type != requested_media_type:
                    if on_skipped:
                        on_skipped(candidate)
                    if on_log:
                        on_log(f"Pinterest candidate skipped: media_type={candidate.media_type}")
                    continue
                delivered += 1
                if on_log:
                    on_log("Pinterest candidate discovered")
                result = on_candidate(candidate)
                if on_log and result is not False:
                    on_log("Pinterest candidate accepted")
                if result is False:
                    break
            return delivered
        except GalleryDlStopped:
            return delivered
        finally:
            client.stop()
