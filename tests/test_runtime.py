from pathlib import Path

from src.utils.runtime import ffmpeg_source, find_ffmpeg, resolve_ffmpeg


def test_bundled_ffmpeg_is_preferred_in_source_tree() -> None:
    path, source = resolve_ffmpeg()
    assert path is not None
    assert Path(path).name.lower() == "ffmpeg.exe"
    assert source == "bundled"
    assert ffmpeg_source() == "bundled"
    assert find_ffmpeg() == path
