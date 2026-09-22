from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


REQUIRED_SHARED_DLLS = (
    "avcodec-62.dll",
    "avdevice-62.dll",
    "avfilter-11.dll",
    "avformat-62.dll",
    "avutil-60.dll",
    "swresample-6.dll",
    "swscale-9.dll",
)


def runtime_root() -> Path:
    """Return the root containing bundled runtime resources."""
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[2]


def _bundled_ffmpeg_candidates() -> tuple[Path, ...]:
    root = runtime_root()
    return (
        root / "vendor" / "ffmpeg" / "ffmpeg.exe",
        root / "ffmpeg" / "ffmpeg.exe",
    )


def find_deno() -> str | None:
    """Locate the Deno runtime bundled with the app before falling back to PATH."""
    root = runtime_root()
    for candidate in (root / "tools" / "deno" / "deno.exe", root / "deno.exe"):
        if candidate.is_file():
            return str(candidate)
    return shutil.which("deno")


def bundled_ffmpeg_runtime_complete(executable: Path) -> bool:
    """Check the shared DLL set shipped beside the formal LGPL executable."""
    return executable.is_file() and all(
        (executable.parent / dll_name).is_file() for dll_name in REQUIRED_SHARED_DLLS
    )


def resolve_ffmpeg() -> tuple[str | None, str | None]:
    """Resolve FFmpeg from an explicit test candidate, bundled resources, or PATH.

    ``SOCIAL_MEDIA_COLLECTOR_FFMPEG_PATH`` is intentionally an explicit test
    hook. Normal packaged and source runs do not set it, so the bundled
    executable wins over PATH without any machine-specific development path.
    """
    override = os.environ.get("SOCIAL_MEDIA_COLLECTOR_FFMPEG_PATH")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return str(candidate), "bundled-lgpl-candidate"
    for candidate in _bundled_ffmpeg_candidates():
        if bundled_ffmpeg_runtime_complete(candidate):
            return str(candidate), "bundled-lgpl"
    system_path = shutil.which("ffmpeg")
    if system_path:
        return system_path, "system-path"
    return None, None


def find_ffmpeg() -> str | None:
    path, _source = resolve_ffmpeg()
    return path


def find_ffprobe() -> str | None:
    override = os.environ.get("SOCIAL_MEDIA_COLLECTOR_FFPROBE_PATH")
    if override and Path(override).is_file():
        return override
    for ffmpeg_path in _bundled_ffmpeg_candidates():
        candidate = ffmpeg_path.with_name("ffprobe.exe")
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffprobe")


def ffmpeg_source() -> str | None:
    _path, source = resolve_ffmpeg()
    return source


def subprocess_creation_flags() -> int:
    """Hide console windows for child processes on Windows."""
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
