"""Validation and stable identity helpers for the supported public YouTube URLs."""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urlparse


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{6,}$")


def normalize_youtube_search_keywords(value: str | Iterable[object]) -> tuple[str, ...]:
    """Normalize newline-delimited search terms while preserving their order."""
    values = value.splitlines() if isinstance(value, str) else value
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        keyword = str(item or "").strip()
        marker = keyword.casefold()
        if keyword and marker not in seen:
            seen.add(marker)
            result.append(keyword)
    return tuple(result)


def youtube_url_kind(value: str) -> str:
    raw = value.strip()
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if host == "youtu.be" and path.strip("/"):
        return "single_video"
    if host not in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        raise ValueError("请输入有效的 YouTube URL")
    query = parse_qs(parsed.query)
    if path == "/playlist" and query.get("list"):
        return "playlist"
    if path.startswith("/shorts/") or path == "/watch" and query.get("v"):
        return "playlist" if query.get("list") else "single_video"
    if path.startswith("/@") or path.startswith("/channel/") or path.startswith("/c/") or path.startswith("/user/"):
        return "channel"
    raise ValueError("YouTube URL 必须是视频、Shorts、Playlist 或频道页面")


def youtube_video_id(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host == "youtu.be":
        candidate = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path.startswith("/shorts/"):
        candidate = parsed.path.split("/", 3)[2]
    else:
        candidate = parse_qs(parsed.query).get("v", [""])[0]
    return candidate if _VIDEO_ID.match(candidate) else ""
