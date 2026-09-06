from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx
from src.utils.runtime import find_ffmpeg, find_ffprobe, subprocess_creation_flags


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
        self._active_process: subprocess.Popen[str] | None = None

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
                    self._active_process = process
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
                    if self._active_process is process:
                        self._active_process = None
                if process.returncode != 0 or not output_path.exists() or output_path.stat().st_size < 1024:
                    raise HLSDownloadError("FFmpeg 无法封装 X HLS 视频。")
            return HLSResult(duration=duration, width=width, height=height, bitrate=bitrate)
        finally:
            self._deadline = None

    def validate_output(self, output_path: Path, expected: HLSResult | None = None) -> HLSResult:
        """Validate an HLS output before the caller can count it as downloaded."""
        if self.should_stop():
            raise HLSDownloadCancelled()
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise HLSDownloadError("未检测到 FFmpeg，无法验证 HLS 视频完整性。")
        expected = expected or HLSResult()
        probe = {}
        ffprobe = find_ffprobe()
        if ffprobe:
            probe_command = [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(output_path),
            ]
            stdout, stderr, returncode = self._run_process(probe_command, 30.0)
            if returncode != 0 or stderr.strip():
                raise HLSDownloadError("HLS 完整性验证失败：ffprobe 无法识别输出。")
            try:
                probe = json.loads(stdout or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HLSDownloadError("HLS 完整性验证失败：ffprobe 返回无效结果。") from exc
            streams = probe.get("streams") if isinstance(probe, dict) else None
            video = next((item for item in streams or [] if item.get("codec_type") == "video"), None)
            if not video:
                raise HLSDownloadError("HLS 完整性验证失败：未发现 video stream。")
            format_data = probe.get("format") if isinstance(probe, dict) else {}
            duration = self._positive_float((format_data or {}).get("duration")) or self._positive_float(video.get("duration"))
            if duration is None:
                raise HLSDownloadError("HLS 完整性验证失败：视频 duration 无效。")
            width = self._positive_int(video.get("width")) or expected.width
            height = self._positive_int(video.get("height")) or expected.height
            bitrate = self._positive_int(video.get("bit_rate")) or expected.bitrate
        else:
            # The packaged LGPL runtime ships ffmpeg, while ffprobe is optional
            # on the host. The decode gate below remains authoritative when
            # ffprobe is unavailable; playlist metadata supplies duration.
            duration = expected.duration
            width, height, bitrate = expected.width, expected.height, expected.bitrate
            if duration is None or duration <= 0:
                raise HLSDownloadError("HLS 完整性验证失败：没有可用 duration。")

        decode_command = [
            ffmpeg,
            "-v",
            "error",
            "-xerror",
            "-i",
            str(output_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-f",
            "null",
            "-",
        ]
        _stdout, stderr, returncode = self._run_process(decode_command, 180.0)
        if returncode != 0 or stderr.strip():
            raise HLSDownloadError("HLS 完整性验证失败：音视频 decode 失败。")
        if self.should_stop():
            raise HLSDownloadCancelled()
        return HLSResult(duration=duration, width=width, height=height, bitrate=bitrate)

    def _run_process(self, command: list[str], timeout_seconds: float) -> tuple[str, str, int]:
        process: subprocess.Popen[str] | None = None
        deadline = time.monotonic() + timeout_seconds
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=subprocess_creation_flags(),
            )
            self._active_process = process
            while True:
                if self.should_stop():
                    self._terminate_process(process)
                    raise HLSDownloadCancelled()
                self.should_pause()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    raise HLSDownloadError("HLS 完整性验证超时。")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    return stdout or "", stderr or "", int(process.returncode or 0)
                except subprocess.TimeoutExpired:
                    continue
        except HLSDownloadCancelled:
            raise
        except HLSDownloadError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise HLSDownloadError("HLS 完整性验证进程启动失败。") from exc
        finally:
            if process is not None and process.poll() is None:
                self._terminate_process(process)
            if self._active_process is process:
                self._active_process = None

    @staticmethod
    def _positive_float(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

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

    def _get(self, url: str, extra_headers: dict[str, str] | None = None) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retry_count):
            if self.should_stop():
                raise HLSDownloadCancelled()
            if self._deadline is not None and time.monotonic() >= self._deadline:
                raise HLSDownloadError("X 视频 HLS 下载超时，已跳过当前候选。")
            self.should_pause()
            try:
                headers = dict(self.headers)
                headers.update(extra_headers or {})
                response = self.client.get(url, headers=headers)
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
        pending_byte_range: str | None = None
        resource_offsets: dict[str, int] = {}
        full_resource_cache: dict[str, bytes] = {}
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
                    absolute_url = urljoin(playlist_url, uri)
                    byte_range = self._parse_byte_range(attrs.get("BYTERANGE"), resource_offsets.get(absolute_url, 0))
                    local.write_bytes(self._download_resource(absolute_url, byte_range, full_resource_cache))
                    if byte_range:
                        resource_offsets[absolute_url] = byte_range[0] + byte_range[1]
                    segment_index += 1
                    output_lines.append(f'#EXT-X-MAP:URI="{self._local_uri(local)}"')
                    continue
            if line.startswith("#EXT-X-BYTERANGE:"):
                pending_byte_range = line.split(":", 1)[1].strip().strip('"')
                continue
            if line and not line.startswith("#"):
                raw_uri = line.strip()
                local = directory / f"part-{segment_index:05d}{self._suffix(raw_uri, '.m4s')}"
                absolute_url = urljoin(playlist_url, raw_uri)
                byte_range = self._parse_byte_range(pending_byte_range, resource_offsets.get(absolute_url, 0))
                local.write_bytes(self._download_resource(absolute_url, byte_range, full_resource_cache))
                if byte_range:
                    resource_offsets[absolute_url] = byte_range[0] + byte_range[1]
                segment_index += 1
                output_lines.append(self._local_uri(local))
                pending_byte_range = None
                continue
            output_lines.append(line)
        manifest = directory / filename
        manifest.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
        return manifest, duration or None

    def _download_resource(
        self,
        url: str,
        byte_range: tuple[int, int] | None,
        full_resource_cache: dict[str, bytes],
    ) -> bytes:
        if byte_range is None:
            return self._get(url).content
        start, length = byte_range
        if start < 0 or length <= 0:
            raise HLSDownloadError("HLS byte range 无效。")
        if url in full_resource_cache:
            content = full_resource_cache[url]
            end = start + length
            if len(content) < end:
                raise HLSDownloadError("HLS byte range 超出资源长度。")
            return content[start:end]
        response = self._get(
            url,
            {"Range": f"bytes={start}-{start + length - 1}"},
        )
        content = response.content
        if response.status_code == 206:
            if len(content) < length:
                raise HLSDownloadError("HLS byte range 返回内容不完整。")
            return content[:length]
        full_resource_cache[url] = content
        end = start + length
        if len(content) < end:
            raise HLSDownloadError("HLS byte range 超出资源长度。")
        return content[start:end]

    @staticmethod
    def _parse_byte_range(value: str | None, default_offset: int = 0) -> tuple[int, int] | None:
        if not value:
            return None
        parts = value.strip().strip('"').split("@", 1)
        try:
            length = int(parts[0])
            offset = int(parts[1]) if len(parts) == 2 else default_offset
        except (TypeError, ValueError):
            raise HLSDownloadError("HLS byte range 格式无效。")
        if length <= 0 or offset < 0:
            raise HLSDownloadError("HLS byte range 数值无效。")
        return offset, length

    @staticmethod
    def _suffix(uri: str, default: str) -> str:
        path = uri.split("?", 1)[0].lower()
        for suffix in (".m4s", ".cmfv", ".cmfa", ".mp4", ".ts", ".aac", ".m4a"):
            if path.endswith(suffix):
                return suffix
        return default

    @staticmethod
    def _local_uri(path: Path) -> str:
        return str(path).replace("\\", "/")
