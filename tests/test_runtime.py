from pathlib import Path
import subprocess

from src.utils.runtime import (
    REQUIRED_SHARED_DLLS,
    bundled_ffmpeg_runtime_complete,
    ffmpeg_source,
    find_ffmpeg,
    resolve_ffmpeg,
    subprocess_creation_flags,
)


def test_bundled_ffmpeg_is_preferred_in_source_tree() -> None:
    path, source = resolve_ffmpeg()
    assert path is not None
    assert Path(path).name.lower() == "ffmpeg.exe"
    assert source == "bundled-lgpl"
    assert ffmpeg_source() == "bundled-lgpl"
    assert find_ffmpeg() == path
    assert bundled_ffmpeg_runtime_complete(Path(path))
    assert all((Path(path).parent / dll_name).is_file() for dll_name in REQUIRED_SHARED_DLLS)


def test_explicit_ffmpeg_candidate_override(monkeypatch, tmp_path: Path) -> None:
    candidate = tmp_path / "ffmpeg.exe"
    candidate.write_bytes(b"candidate")
    monkeypatch.setenv("SOCIAL_MEDIA_COLLECTOR_FFMPEG_PATH", str(candidate))
    path, source = resolve_ffmpeg()
    assert path == str(candidate)
    assert source == "bundled-lgpl-candidate"


def test_bundled_ffmpeg_is_the_lgpl_shared_build() -> None:
    path = find_ffmpeg()
    assert path is not None
    result = subprocess.run(
        [path, "-version"],
        check=True,
        capture_output=True,
        text=True,
    )
    version_info = result.stdout + result.stderr
    assert "n8.1.2-44-g7c533d0f86" in version_info
    assert "--enable-gpl" not in version_info
    assert "--enable-nonfree" not in version_info
    assert "--enable-shared" in version_info


def test_subprocess_creation_flags_hide_windows_console() -> None:
    assert subprocess_creation_flags() == getattr(subprocess, "CREATE_NO_WINDOW", 0)
