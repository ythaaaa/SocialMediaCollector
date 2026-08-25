from __future__ import annotations

import shutil
import sys
from pathlib import Path


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


def resolve_ffmpeg() -> tuple[str | None, str | None]:
    """Resolve FFmpeg from bundled runtime resources, then PATH fallback.

    The resolver intentionally does not contain any machine-specific development
    paths. In a packaged build the bundled executable wins over PATH.
    """
    for candidate in _bundled_ffmpeg_candidates():
        if candidate.is_file():
            return str(candidate), "bundled"
    system_path = shutil.which("ffmpeg")
    if system_path:
        return system_path, "system-path"
    return None, None


def find_ffmpeg() -> str | None:
    path, _source = resolve_ffmpeg()
    return path


def ffmpeg_source() -> str | None:
    _path, source = resolve_ffmpeg()
    return source
