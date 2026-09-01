from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx
from src.utils.runtime import find_ffmpeg, subprocess_creation_flags


class HLSDownloadError(RuntimeError):
    pass


class HLSDownloadCancelled(HLSDownloadError):
    pass


@dataclass(frozen=True)
class HLSResult:
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    bitrate: int | None = None


def parse_attributes(value: str) -> dict[str, str]:
    return {
        key: raw.strip('"')
        for key, raw in re.findall(r"([A-Z0-9-]+)=((?:\"[^\"]*\")|(?:[^,]*))", value)
    }


def select_variant(master_text: str, master_url: str) -> tuple[str, int | None, int | None, int | None, str | None]:
    lines = master_text.splitlines()
    variants: list[tuple[int, int, int, str, str | None]] = []
    audio_groups: dict[str, str] = {}
    for line in lines:
        if not line.startswith("#EXT-X-MEDIA:"):
            continue
        attrs = parse_attributes(line.split(":", 1)[1])
        if attrs.get("TYPE") == "AUDIO" and attrs.get("URI") and attrs.get("GROUP-ID"):
            audio_groups[attrs["GROUP-ID"]] = urljoin(master_url, attrs["URI"])
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:") or index + 1 >= len(lines):
            continue
        attrs = parse_attributes(line.split(":", 1)[1])
        resolution = attrs.get("RESOLUTION", "")
        width = height = 0
        if "x" in resolution.lower():
            raw_width, raw_height = resolution.lower().split("x", 1)
            if raw_width.isdigit() and raw_height.isdigit():
                width, height = int(raw_width), int(raw_height)
        bandwidth = int(attrs.get("BANDWIDTH", "0") or 0)
        variants.append((bandwidth, width * height, index, urljoin(master_url, lines[index + 1].strip()), audio_groups.get(attrs.get("AUDIO", ""))))
    if not variants:
        return master_url, None, None, None, None
    bandwidth, _area, _index, variant_url, audio_url = max(variants, key=lambda item: (item[1], item[0]))
    chosen = next(item for item in variants if item[3] == variant_url and item[0] == bandwidth)
    width = height = None
    attrs = parse_attributes(lines[chosen[2]].split(":", 1)[1])
    resolution = attrs.get("RESOLUTION", "")
    if "x" in resolution.lower():
        raw_width, raw_height = resolution.lower().split("x", 1)
        if raw_width.isdigit() and raw_height.isdigit():
            width, height = int(raw_width), int(raw_height)
    return variant_url, width, height, bandwidth or None, audio_url


class HLSDownloader:
    _MEDIA_DOWNLOAD_TIMEOUT_SECONDS = 180.0

    def __init__(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        retry_count: int,
        should_stop: Callable[[], bool],
        should_pause: Callable[[], None],
    ) -> None:
        self.client = client
        self.headers = headers
        self.retry_count = retry_count
        self.should_stop = should_stop
        self.should_pause = should_pause
        self._deadline: float | None = None

    def download(self, playlist_url: str, output_path: Path) -> HLSResult:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise HLSDownloadError("未检测到 FFmpeg，无法保存 X 视频；图片采集不受影响。")
        self._deadline = time.monotonic() + self._MEDIA_DOWNLOAD_TIMEOUT_SECONDS
        try:
            master_text = self._get_text(playlist_url)
            video_url, width, height, bitrate, audio_url = select_variant(master_text, playlist_url)
            with tempfile.TemporaryDirectory(prefix="x-hls-") as raw_temp:
                temp_dir = Path(raw_temp)
                video_manifest, duration = self._materialize_playlist(video_url, temp_dir / "video", "video.m3u8")
                audio_manifest = None
                if audio_url:
                    audio_manifest, _audio_duration = self._materialize_playlist(audio_url, temp_dir / "audio", "audio.m3u8")
                command = [
                    ffmpeg,
                    "-y",
                    "-loglevel",
                    "error",
                    "-allowed_extensions",
                    "ALL",
                    "-protocol_whitelist",
                    "file,crypto,data",
                    "-i",
                    str(video_manifest),
                ]
                if audio_manifest:
                    command.extend(["-i", str(audio_manifest)])
                command.extend(["-map", "0:v:0"])
                if audio_manifest:
                    command.extend(["-map", "1:a:0?"])
                command.extend(["-c", "copy", "-movflags", "+faststart", str(output_path)])
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    raise HLSDownloadError("X 视频 HLS 下载超时，已跳过当前候选。")
                ffmpeg_timeout = self._MEDIA_DOWNLOAD_TIMEOUT_SECONDS
                if self._deadline is not None:
                    ffmpeg_timeout = max(1.0, self._deadline - time.monotonic())
                process = None
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        creationflags=subprocess_creation_flags(),
                    )
                    deadline = time.monotonic() + ffmpeg_timeout
                    while True:
                        if self.should_stop():
                            self._terminate_process(process)
                            raise HLSDownloadCancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            self._terminate_process(process)
                            raise HLSDownloadError("X 视频 HLS 下载超时，已跳过当前候选。")
                        try:
                            process.communicate(timeout=min(0.2, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                except subprocess.SubprocessError as exc:
                    raise HLSDownloadError("FFmpeg 执行失败。") from exc
                finally:
                    if process is not None and process.poll() is None:
                        self._terminate_process(process)
                if process.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1024:
                    raise HLSDownloadError("FFmpeg 无法封装 X HLS 视频。")
            return HLSResult(duration=duration, width=width, height=height, bitrate=bitrate)
        finally:
            self._deadline = None

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _get_text(self, url: str) -> str:
        response = self._get(url)
        text = response.text.lstrip()
        if not text.startswith("#EXTM3U"):
            raise HLSDownloadError("X 视频播放列表不是有效的 HLS 内容。")
        return response.text

    def _get(self, url: str) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retry_count):
            if self.should_stop():
                raise HLSDownloadCancelled()
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise HLSDownloadError("X 视频 HLS 下载超时，已跳过当前候选。")
            self.should_pause()
            try:
                response = self.client.get(url, headers=self.headers)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt + 1 < self.retry_count:
                    continue
        raise HLSDownloadError(str(last_error or "HLS 请求失败"))

    def _materialize_playlist(self, playlist_url: str, directory: Path, filename: str) -> tuple[Path, float | None]:
        text = self._get_text(playlist_url)
        directory.mkdir(parents=True, exist_ok=True)
        output_lines: list[str] = []
        duration = 0.0
        segment_index = 0
        for line in text.splitlines():
            if line.startswith("#EXT-X-KEY:"):
                attrs = parse_attributes(line.split(":", 1)[1])
                if attrs.get("METHOD", "NONE") != "NONE":
                    raise HLSDownloadError("X 视频使用了暂不支持的加密 HLS。")
            if line.startswith("#EXTINF:"):
                raw_duration = line.split(":", 1)[1].split(",", 1)[0]
                try:
                    duration += float(raw_duration)
                except ValueError:
                    pass
            if line.startswith("#EXT-X-MAP:"):
                attrs = parse_attributes(line.split(":", 1)[1])
                uri = attrs.get("URI")
                if uri:
                    local = directory / f"part-{segment_index:05d}{self._suffix(uri, '.mp4')}"
                    local.write_bytes(self._get(urljoin(playlist_url, uri)).content)
                    segment_index += 1
                    output_lines.append(f'#EXT-X-MAP:URI="{self._local_uri(local)}"')
                    continue
            if line and not line.startswith("#"):
                raw_uri = line.strip()
                local = directory / f"part-{segment_index:05d}{self._suffix(raw_uri, '.m4s')}"
                local.write_bytes(self._get(urljoin(playlist_url, raw_uri)).content)
                segment_index += 1
                output_lines.append(self._local_uri(local))
                continue
            output_lines.append(line)
        manifest = directory / filename
        manifest.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
        return manifest, duration or None

    @staticmethod
    def _suffix(uri: str, default: str) -> str:
        path = uri.split("?", 1)[0].lower()
        for suffix in (".m4s", ".mp4", ".ts", ".aac", ".m4a"):
            if path.endswith(suffix):
                return suffix
        return default

    @staticmethod
    def _local_uri(path: Path) -> str:
        return str(path).replace("\\", "/")
