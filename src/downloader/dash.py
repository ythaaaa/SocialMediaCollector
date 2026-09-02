from __future__ import annotations

import json
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from src.utils.runtime import find_ffmpeg, find_ffprobe, subprocess_creation_flags


class DASHDownloadError(RuntimeError):
    pass


class DASHDownloadCancelled(DASHDownloadError):
    pass


class DASHParseError(ValueError):
    pass


@dataclass(frozen=True)
class DashRepresentation:
    kind: str
    representation_id: str
    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
    codec: str = ""
    mime_type: str = ""
    frame_rate: str = ""
    base_url: str = ""
    sample_rate: int | None = None
    channels: int | None = None
    duration: float | None = None
    segment_base: dict[str, str] | None = None
    segment_template: dict[str, str] | None = None


@dataclass(frozen=True)
class DashManifest:
    duration: float | None
    video: tuple[DashRepresentation, ...]
    audio: tuple[DashRepresentation, ...]


@dataclass(frozen=True)
class DASHResult:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None
    codec: str = ""
    has_audio: bool = False


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _positive_int(value: object) -> int | None:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _float_value(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _duration(value: str | None) -> float | None:
    if not value or not value.startswith("PT"):
        return None
    raw = value[2:]
    total = 0.0
    number = ""
    for char in raw:
        if char.isdigit() or char == ".":
            number += char
            continue
        if number:
            factor = {"H": 3600, "M": 60, "S": 1}.get(char)
            if factor is not None:
                total += float(number) * factor
            number = ""
    return total or None


def _parse_rate(value: str | None) -> str:
    return str(value or "")


def _inherited(child: ET.Element, parent: ET.Element, key: str) -> str:
    return str(child.attrib.get(key) or parent.attrib.get(key) or "")


def _base_url(element: ET.Element, inherited_url: str) -> str:
    nodes = _children(element, "BaseURL")
    if not nodes or not (nodes[0].text or "").strip():
        return inherited_url
    return urljoin(inherited_url, unescape((nodes[0].text or "").strip()))


def _segment_attributes(element: ET.Element, name: str) -> dict[str, str] | None:
    nodes = _children(element, name)
    if not nodes:
        return None
    return {str(key): str(value) for key, value in nodes[0].attrib.items()}


def _parse_representation(
    adaptation: ET.Element,
    representation: ET.Element,
    base_url: str,
    duration: float | None,
) -> DashRepresentation:
    content_type = _inherited(representation, adaptation, "contentType").lower()
    mime_type = _inherited(representation, adaptation, "mimeType")
    kind = "audio" if content_type == "audio" or mime_type.startswith("audio/") else "video"
    width = _positive_int(representation.attrib.get("width") or adaptation.attrib.get("width"))
    height = _positive_int(representation.attrib.get("height") or adaptation.attrib.get("height"))
    sample_rate = _positive_int(representation.attrib.get("audioSamplingRate") or adaptation.attrib.get("audioSamplingRate"))
    channels = None
    audio_channel_nodes = _children(representation, "AudioChannelConfiguration") or _children(adaptation, "AudioChannelConfiguration")
    if audio_channel_nodes:
        channels = _positive_int(audio_channel_nodes[0].attrib.get("value"))
    representation_url = _base_url(representation, base_url)
    return DashRepresentation(
        kind=kind,
        representation_id=str(representation.attrib.get("id") or ""),
        width=width,
        height=height,
        bandwidth=_positive_int(representation.attrib.get("bandwidth") or adaptation.attrib.get("bandwidth")),
        codec=str(representation.attrib.get("codecs") or adaptation.attrib.get("codecs") or ""),
        mime_type=mime_type,
        frame_rate=_parse_rate(representation.attrib.get("frameRate") or adaptation.attrib.get("frameRate")),
        base_url=representation_url,
        sample_rate=sample_rate,
        channels=channels,
        duration=duration,
        segment_base=_segment_attributes(representation, "SegmentBase") or _segment_attributes(adaptation, "SegmentBase"),
        segment_template=_segment_attributes(representation, "SegmentTemplate") or _segment_attributes(adaptation, "SegmentTemplate"),
    )


def parse_mpd(source: str, manifest_url: str = "", fetcher: Callable[[str], str] | None = None) -> DashManifest:
    """Parse an embedded MPD or an MPD URL without exposing signed URLs."""
    raw = source.strip()
    if raw.startswith(("http://", "https://")):
        if fetcher is None:
            response = httpx.get(raw, timeout=30, follow_redirects=True)
            response.raise_for_status()
            raw = response.text
        else:
            raw = fetcher(raw)
        manifest_url = manifest_url or source
    try:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            root = ET.fromstring(unescape(raw))
    except ET.ParseError as exc:
        raise DASHParseError("Instagram DASH manifest 不是有效的 MPD XML。") from exc
    if _local_name(root.tag) != "MPD":
        raise DASHParseError("Instagram DASH manifest 缺少 MPD 根节点。")
    duration = _duration(root.attrib.get("mediaPresentationDuration"))
    root_url = _base_url(root, manifest_url)
    videos: list[DashRepresentation] = []
    audios: list[DashRepresentation] = []
    for period in _children(root, "Period") or [root]:
        period_url = _base_url(period, root_url)
        period_duration = _duration(period.attrib.get("duration")) or duration
        for adaptation in _children(period, "AdaptationSet"):
            adaptation_url = _base_url(adaptation, period_url)
            representations = _children(adaptation, "Representation")
            if not representations:
                representations = [adaptation]
            for representation in representations:
                parsed = _parse_representation(adaptation, representation, adaptation_url, period_duration)
                if parsed.base_url and parsed.kind == "video":
                    videos.append(parsed)
                elif parsed.base_url and parsed.kind == "audio":
                    audios.append(parsed)
    return DashManifest(duration, tuple(videos), tuple(audios))


def quality_key(item: object) -> tuple[int, int]:
    width = getattr(item, "width", None) or 0
    height = getattr(item, "height", None) or 0
    bandwidth = getattr(item, "bandwidth", None) or getattr(item, "bitrate", None) or 0
    return width * height, bandwidth


def select_best_video(representations: tuple[DashRepresentation, ...] | list[DashRepresentation]) -> DashRepresentation | None:
    return max(representations, key=quality_key, default=None)


def select_best_audio(representations: tuple[DashRepresentation, ...] | list[DashRepresentation]) -> DashRepresentation | None:
    return max(representations, key=lambda item: (item.bandwidth or 0, item.sample_rate or 0), default=None)


class DASHDownloader:
    _MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 180.0

    def __init__(self, client: httpx.Client, headers: dict[str, str], retry_count: int, should_stop: Callable[[], bool], should_pause: Callable[[], None]) -> None:
        self.client = client
        self.headers = headers
        self.retry_count = retry_count
        self.should_stop = should_stop
        self.should_pause = should_pause
        self._deadline: float | None = None
        self._active_process: subprocess.Popen[str] | None = None

    def download(self, video_url: str, output_path: Path, audio_url: str = "", width: int | None = None, height: int | None = None, bitrate: int | None = None, codec: str = "", duration: float | None = None) -> DASHResult:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise DASHDownloadError("未检测到 FFmpeg，无法保存 Instagram DASH 视频。")
        if not video_url.startswith(("http://", "https://")):
            raise DASHDownloadError("Instagram DASH 视频地址无效。")
        self._deadline = time.monotonic() + self._MEDIA_DOWNLOAD_TIMEOUT_SECONDS
        completed = False
        try:
            with tempfile.TemporaryDirectory(prefix="instagram-dash-") as raw_temp:
                temp_dir = Path(raw_temp)
                video_path = self._download_resource(video_url, temp_dir / "video.mp4")
                audio_path = self._download_resource(audio_url, temp_dir / "audio.m4a") if audio_url else None
                command = [ffmpeg, "-y", "-loglevel", "error", "-i", str(video_path)]
                if audio_path:
                    command.extend(["-i", str(audio_path)])
                command.extend(["-map", "0:v:0", "-map", "1:a:0?" if audio_path else "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(output_path)])
                self._run_ffmpeg(command, output_path)
            if self.should_stop():
                raise DASHDownloadCancelled()
            probe = probe_media(output_path, should_stop=self.should_stop, deadline=self._deadline)
            if self.should_stop():
                raise DASHDownloadCancelled()
            completed = True
            return DASHResult(probe.get("duration") or duration, probe.get("width") or width, probe.get("height") or height, probe.get("bitrate") or bitrate, probe.get("codec") or codec, bool(probe.get("has_audio", bool(audio_url))))
        except DASHDownloadCancelled:
            self._remove_file(output_path)
            raise
        finally:
            self._terminate_active_process()
            self._deadline = None
            if not completed:
                self._remove_file(output_path)

    def _download_resource(self, url: str, output_path: Path) -> Path:
        if not url.startswith(("http://", "https://")):
            raise DASHDownloadError("Instagram DASH 资源地址无效。")
        last_error: Exception | None = None
        for attempt in range(max(1, self.retry_count)):
            try:
                if self.should_stop():
                    raise DASHDownloadCancelled()
                self.should_pause()
                if self.should_stop():
                    raise DASHDownloadCancelled()
                with self.client.stream("GET", url, headers=self.headers) as response:
                    response.raise_for_status()
                    with output_path.open("wb") as handle:
                        for chunk in response.iter_bytes(1024 * 1024):
                            if self.should_stop():
                                raise DASHDownloadCancelled()
                            self.should_pause()
                            if self._deadline is not None and time.monotonic() >= self._deadline:
                                raise DASHDownloadError("Instagram DASH 下载超时。")
                            handle.write(chunk)
                if output_path.stat().st_size < 1024:
                    raise DASHDownloadError("Instagram DASH 资源为空或不完整。")
                return output_path
            except DASHDownloadCancelled:
                self._remove_file(output_path)
                raise
            except (httpx.HTTPError, OSError) as exc:
                last_error = exc
                if output_path.exists():
                    output_path.unlink()
                if attempt + 1 < max(1, self.retry_count):
                    continue
        raise DASHDownloadError("Instagram DASH 资源下载失败。") from last_error

    def _run_ffmpeg(self, command: list[str], output_path: Path) -> DASHResult:
        process: subprocess.Popen[str] | None = None
        stderr_text = ""
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess_creation_flags())
            self._active_process = process
            while True:
                if self.should_stop():
                    self._terminate(process)
                    raise DASHDownloadCancelled()
                self.should_pause()
                remaining = (self._deadline or time.monotonic()) - time.monotonic()
                if remaining <= 0:
                    self._terminate(process)
                    raise DASHDownloadError("Instagram DASH 下载超时。")
                try:
                    _stdout_text, stderr_text = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except DASHDownloadError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise DASHDownloadError("FFmpeg 执行 Instagram DASH 失败。") from exc
        finally:
            if process is not None and process.poll() is None:
                self._terminate(process)
            if self._active_process is process:
                self._active_process = None
        if process is None or process.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1024:
            stderr = self._safe_error(stderr_text)
            raise DASHDownloadError(f"FFmpeg 无法封装 Instagram DASH 视频{(': ' + stderr) if stderr else '。'}")
        return DASHResult()

    def _header_block(self) -> str:
        return "".join(f"{key}: {value}\r\n" for key, value in self.headers.items())

    @staticmethod
    def _safe_error(value: object) -> str:
        text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
        for token in text.split():
            if token.startswith(("http://", "https://")):
                parsed = urlsplit(token)
                text = text.replace(token, urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")))
        return text[:240]

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _terminate_active_process(self) -> None:
        process = self._active_process
        if process is not None and process.poll() is None:
            self._terminate(process)
        self._active_process = None

    @staticmethod
    def _remove_file(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def probe_media(path: Path, should_stop: Callable[[], bool] | None = None, deadline: float | None = None) -> dict[str, object]:
    ffprobe = find_ffprobe()
    if not ffprobe:
        return {}
    if should_stop and should_stop():
        raise DASHDownloadCancelled()
    command = [ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,bit_rate", "-of", "json", str(path)]
    process: subprocess.Popen[str] | None = None
    stdout_text = ""
    probe_deadline = time.monotonic() + 30
    if deadline is not None:
        probe_deadline = min(probe_deadline, deadline)
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=subprocess_creation_flags())
        while True:
            if should_stop and should_stop():
                DASHDownloader._terminate(process)
                raise DASHDownloadCancelled()
            remaining = probe_deadline - time.monotonic()
            if remaining <= 0:
                DASHDownloader._terminate(process)
                return {}
            try:
                stdout_text, _stderr_text = process.communicate(timeout=min(0.2, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode != 0:
            return {}
        data = json.loads(stdout_text or "{}")
    except DASHDownloadCancelled:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError):
        return {}
    finally:
        if process is not None and process.poll() is None:
            DASHDownloader._terminate(process)
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    return {
        "duration": _float_value((data.get("format") or {}).get("duration")),
        "width": _positive_int(video.get("width")),
        "height": _positive_int(video.get("height")),
        "bitrate": _positive_int(video.get("bit_rate")),
        "codec": str(video.get("codec_name") or ""),
        "has_audio": any(item.get("codec_type") == "audio" for item in streams),
    }
