"""Isolated JSONL worker shared by external-Python and packaged helpers."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, unquote_plus, urlsplit


POST_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/(?:p|reel|reels)/[^/?#]+/?(?:[?#].*)?$",
    re.IGNORECASE,
)
AUTHOR_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9._]{1,30})/?(?:[?#].*)?$",
    re.IGNORECASE,
)
TAG_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/explore/tags/([^/?#]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
PINTEREST_SEARCH_URL_RE = re.compile(
    r"^https?://(?:www\.)?pinterest\.[^/]+/search/pins/?\?q=[^#]+(?:#.*)?$",
    re.IGNORECASE,
)
PINTEREST_PIN_URL_RE = re.compile(
    r"^https?://(?:www\.)?pinterest\.[^/]+/pin/[^/?#]+/?(?:[?#].*)?$",
    re.IGNORECASE,
)
PINTEREST_SHORT_URL_RE = re.compile(
    r"^https?://pin\.it/[^/?#]+/?(?:[?#].*)?$",
    re.IGNORECASE,
)
PINTEREST_USER_PINS_URL_RE = re.compile(
    r"^https?://(?:www\.)?pinterest\.[^/]+/(?!pin/|search/)([^/?#]+)/pins/?(?:[?#].*)?$",
    re.IGNORECASE,
)
PINTEREST_BOARD_URL_RE = re.compile(
    r"^https?://(?:www\.)?pinterest\.[^/]+/(?!pin/|search/)([^/?#]+)/(?!(?:pins|_created)/?$)([^/?#]+)/?(?:[?#].*)?$",
    re.IGNORECASE,
)
X_MEDIA_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/([A-Za-z0-9_]{1,15})/media/?(?:[?#].*)?$",
    re.IGNORECASE,
)
X_SEARCH_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x\.com|twitter\.com)/search/?\?(?:[^#]*&)?q=[^#]+",
    re.IGNORECASE,
)
PINTEREST_AUTH_ERROR_MARKERS = (
    "401",
    "403",
    "forbidden",
    "unauthorized",
    "challenge",
    "captcha",
    "checkpoint",
    "login required",
    "logged out",
    "redirect",
)


def _is_pinterest_auth_error(exc: BaseException) -> bool:
    """Recognize auth failures without exposing response/cookie contents."""
    parts = [str(exc), type(exc).__name__]
    for attribute in ("status_code", "status", "url"):
        value = getattr(exc, attribute, None)
        if value is not None:
            parts.append(str(value))
    text = " ".join(parts).casefold()
    return any(marker in text for marker in PINTEREST_AUTH_ERROR_MARKERS)


def _is_x_auth_error(exc: BaseException) -> bool:
    parts = [str(exc), type(exc).__name__]
    for attribute in ("status_code", "status", "url"):
        value = getattr(exc, attribute, None)
        if value is not None:
            parts.append(str(value))
    text = " ".join(parts).casefold()
    return any(marker in text for marker in PINTEREST_AUTH_ERROR_MARKERS + ("authorizationerror", "authrequired"))


def _configure_stdio_utf8() -> None:
    """Configure text streams when supported by Python/PyInstaller."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            continue


def _readline_utf8(stream: Any) -> str:
    """Read one UTF-8 protocol line, with a text-stream test fallback."""
    buffer = getattr(stream, "buffer", None)
    if buffer is not None:
        raw = buffer.readline()
        if not raw:
            return ""
        return raw.decode("utf-8")
    return stream.readline()


def _write(message: dict[str, Any]) -> None:
    line = json.dumps(message, ensure_ascii=False, default=str) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(line.encode("utf-8"))
        buffer.flush()
        return
    sys.stdout.write(line)
    sys.stdout.flush()


def _read_command() -> str | None:
    try:
        line = _readline_utf8(sys.stdin)
    except UnicodeError:
        return "invalid"
    if not line:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return "invalid"
    return str(value.get("type") or "") if isinstance(value, dict) else "invalid"


def _safe_date(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value or "")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _candidate_payload(
    post: dict[str, Any],
    file_data: dict[str, Any],
    source_hashtag: str = "",
    *,
    platform: str = "instagram",
    item_url: str = "",
    source_url: str = "",
    source_type: str = "",
    source_username: str = "",
    source_board_url: str = "",
) -> dict[str, Any]:
    canonical_platform = platform.casefold()
    if canonical_platform == "pinterest":
        return _pinterest_candidate_payload(
            post,
            file_data,
            item_url=item_url,
            source_url=source_url,
            source_type=source_type,
            source_username=source_username,
            source_board_url=source_board_url,
        )
    if canonical_platform in {"x", "twitter"}:
        return _x_candidate_payload(
            post,
            file_data,
            item_url=item_url,
            source_url=source_url,
            source_type=source_type,
            source_username=source_username,
        )
    media_type = "video" if file_data.get("video_url") else "image"
    shortcode = str(post.get("post_shortcode") or file_data.get("shortcode") or "")
    try:
        media_index = max(0, int(file_data.get("num", 1)) - 1)
    except (TypeError, ValueError):
        media_index = 0
    display_url = str(file_data.get("display_url") or "")
    video_url = str(file_data.get("video_url") or "")
    dash_manifest = file_data.get("_ytdl_manifest_data")
    hashtag = source_hashtag or str(post.get("tag") or "")
    metadata = {
        "platform": "instagram",
        "post_id": str(post.get("post_id") or ""),
        "shortcode": shortcode,
        "post_url": str(post.get("post_url") or ""),
        "username": str(post.get("username") or ""),
        "description": str(post.get("description") or ""),
        "caption": str(post.get("description") or ""),
        "date": _safe_date(post.get("post_date") or post.get("date")),
        "media_index": media_index,
        "media_type": media_type,
        "width": _safe_int(file_data.get("width")),
        "height": _safe_int(file_data.get("height")),
        "dash_manifest": dash_manifest,
        "hashtag": hashtag,
        "source_hashtag": hashtag,
    }
    return {
        "type": "candidate",
        "platform": "instagram",
        "media_type": media_type,
        "post_id": str(post.get("post_id") or ""),
        "shortcode": shortcode,
        "media_index": media_index,
        "media_identity": f"{shortcode}:{media_index}",
        "post_url": str(post.get("post_url") or ""),
        "username": str(post.get("username") or ""),
        "description": str(post.get("description") or ""),
        "caption": str(post.get("description") or ""),
        "date": _safe_date(post.get("post_date") or post.get("date")),
        "display_url": display_url,
        "video_url": video_url,
        "media_url": video_url or display_url,
        "width": _safe_int(file_data.get("width")),
        "height": _safe_int(file_data.get("height")),
        "thumbnail_url": display_url,
        "dash_manifest": dash_manifest,
        "hashtag": hashtag,
        "source_hashtag": hashtag,
        "metadata": metadata,
    }


def _pinterest_text(value: Any) -> str:
    return str(value or "").strip()


def _pinterest_pin_id(post: dict[str, Any]) -> str:
    for key in ("id", "pin_id", "post_id"):
        value = _pinterest_text(post.get(key))
        if value:
            return value
    match = re.search(r"/pin/([^/?#]+)", _pinterest_text(post.get("url") or post.get("pin_url")))
    return match.group(1) if match else ""


def _pinterest_post_url(post: dict[str, Any], pin_id: str) -> str:
    for key in ("pin_url", "post_url", "url"):
        value = _pinterest_text(post.get(key))
        if "/pin/" in value.casefold():
            return value
    return f"https://www.pinterest.com/pin/{pin_id}/" if pin_id else ""


def _pinterest_nested_url(value: Any) -> str:
    if isinstance(value, dict):
        return _pinterest_text(value.get("url") or value.get("src"))
    return _pinterest_text(value)


def _pinterest_image_urls(post: dict[str, Any]) -> list[str]:
    images = post.get("images")
    if not isinstance(images, dict):
        return []
    values: list[str] = []
    for name in ("orig", "original", "736x", "564x", "474x", "236x"):
        url = _pinterest_nested_url(images.get(name))
        if url and url not in values:
            values.append(url)
    return values


def _pinterest_video_urls(post: dict[str, Any]) -> list[str]:
    videos = post.get("videos")
    if not isinstance(videos, dict):
        return []
    values: list[str] = []
    for value in videos.values():
        url = _pinterest_nested_url(value)
        if url and url not in values:
            values.append(url)
    return values


def _pinterest_media_kind(post: dict[str, Any], file_data: dict[str, Any], item_url: str) -> str:
    extension = _pinterest_text(file_data.get("extension")).casefold()
    candidate_url = item_url or _pinterest_text(file_data.get("url") or file_data.get("video_url"))
    if file_data.get("_ytdl_manifest") or file_data.get("_ytdl_manifest_data"):
        return "video"
    if extension in {"mp4", "m3u8", "webm", "mov"}:
        return "video"
    if ".m3u8" in candidate_url.casefold() or any(candidate_url == value for value in _pinterest_video_urls(post)):
        return "video"
    return "image"


def _pinterest_fallback_urls(post: dict[str, Any], file_data: dict[str, Any], primary: str, media_type: str) -> list[str]:
    values: list[str] = []
    raw = file_data.get("_fallback") or file_data.get("fallback_urls") or ()
    if isinstance(raw, str):
        raw = (raw,)
    if isinstance(raw, (list, tuple)):
        for value in raw:
            url = _pinterest_nested_url(value)
            if url and url != primary and url not in values:
                values.append(url)
    candidates = _pinterest_video_urls(post) if media_type == "video" else _pinterest_image_urls(post)
    for url in candidates:
        if url and url != primary and url not in values:
            values.append(url)
    if "/originals/" in primary:
        variant = primary.replace("/originals/", "/736x/", 1)
        if variant != primary and variant not in values:
            values.append(variant)
    return values


def _pinterest_source_query(source_url: str) -> str:
    try:
        query = parse_qs(urlsplit(source_url).query).get("q", [""])[0]
    except (TypeError, ValueError):
        return ""
    return unquote_plus(str(query))


def _pinterest_candidate_payload(
    post: dict[str, Any],
    file_data: dict[str, Any],
    *,
    item_url: str = "",
    source_url: str = "",
    source_type: str = "",
    source_username: str = "",
    source_board_url: str = "",
) -> dict[str, Any] | None:
    media_type = _pinterest_media_kind(post, file_data, item_url)
    extension = _pinterest_text(file_data.get("extension")).casefold()
    if extension in {"mp3", "m4a", "wav", "aac", "txt", "json"}:
        return None
    pin_id = _pinterest_pin_id(post)
    media_index = max(0, (_safe_int(file_data.get("num")) or 1) - 1)
    post_url = _pinterest_post_url(post, pin_id)
    image_urls = _pinterest_image_urls(post)
    thumbnail_url = image_urls[0] if image_urls else ""
    raw_media_url = _pinterest_text(item_url or file_data.get("url") or file_data.get("video_url"))
    if raw_media_url.startswith("ytdl:"):
        raw_media_url = raw_media_url[5:]
    if media_type == "image" and not raw_media_url:
        raw_media_url = thumbnail_url
    width = _safe_int(file_data.get("width"))
    height = _safe_int(file_data.get("height"))
    duration = _safe_float(file_data.get("duration") or post.get("duration"))
    bitrate = _safe_int(file_data.get("bitrate"))
    author = post.get("pinner") or post.get("author") or {}
    if isinstance(author, dict):
        username = _pinterest_text(author.get("username") or author.get("name"))
    else:
        username = _pinterest_text(author)
    description = _pinterest_text(post.get("description") or post.get("closeup_description") or post.get("title"))
    board_data = post.get("board") if isinstance(post.get("board"), dict) else {}
    board = post.get("board") or post.get("board_name") or ""
    if isinstance(board, dict):
        board = board.get("name") or board.get("id") or ""
    source_board = _pinterest_text(board)
    owner_data = board_data.get("owner") if isinstance(board_data.get("owner"), dict) else {}
    source_owner = _pinterest_text(owner_data.get("username") or owner_data.get("name"))
    if source_type == "board" and not source_board_url:
        source_board_url = source_url
    fallback_urls = _pinterest_fallback_urls(post, file_data, raw_media_url, media_type)
    video_sources: list[dict[str, Any]] = []
    if media_type == "video" and raw_media_url:
        kind = "hls" if file_data.get("_ytdl_manifest") == "hls" or ".m3u8" in raw_media_url.casefold() else "mp4"
        video_sources.append({
            "url": raw_media_url,
            "width": width,
            "height": height,
            "duration": duration,
            "bitrate": bitrate,
            "kind": kind,
        })
        for fallback in fallback_urls:
            if fallback == raw_media_url:
                continue
            video_sources.append({
                "url": fallback,
                "width": width,
                "height": height,
                "duration": duration,
                "bitrate": bitrate,
                "kind": "hls" if ".m3u8" in fallback.casefold() else "mp4",
            })
    metadata = {
        "platform": "pinterest",
        "pin_id": pin_id,
        "pin_url": post_url,
        "source_url": source_url,
        "source_query": _pinterest_source_query(source_url),
        "source_type": source_type,
        "source_username": source_username,
        "source_board": source_board,
        "source_board_url": source_board_url,
        "source_owner": source_owner,
        "username": username,
        "board": str(board),
        "description": description,
        "closeup_description": _pinterest_text(post.get("closeup_description")),
        "media_index": media_index,
        "media_type": media_type,
        "width": width,
        "height": height,
        "duration": duration,
        "bitrate": bitrate,
    }
    return {
        "type": "candidate",
        "platform": "pinterest",
        "media_type": media_type,
        "post_id": pin_id,
        "pin_id": pin_id,
        "media_index": media_index,
        "media_identity": f"{pin_id}:{media_index}",
        "post_url": post_url,
        "pin_url": post_url,
        "username": username,
        "board": str(board),
        "description": description,
        "caption": description,
        "display_url": thumbnail_url if media_type == "video" else raw_media_url,
        "thumbnail_url": thumbnail_url,
        "media_url": raw_media_url,
        "video_url": raw_media_url if media_type == "video" else "",
        "fallback_urls": fallback_urls,
        "video_sources": video_sources,
        "width": width,
        "height": height,
        "duration": duration,
        "bitrate": bitrate,
        "metadata": metadata,
        "source_url": source_url,
        "source_query": _pinterest_source_query(source_url),
        "source_type": source_type,
        "source_username": source_username,
        "source_board": source_board,
        "source_board_url": source_board_url,
        "source_owner": source_owner,
    }


def _x_candidate_payload(
    post: dict[str, Any],
    file_data: dict[str, Any],
    *,
    item_url: str = "",
    source_url: str = "",
    source_type: str = "",
    source_username: str = "",
) -> dict[str, Any] | None:
    url = str(item_url or file_data.get("url") or "").strip()
    if not url or url.startswith("ytdl:"):
        return None
    extension = str(file_data.get("extension") or "").casefold()
    raw_type = str(file_data.get("type") or "").casefold()
    media_type = "video" if raw_type in {"video", "animated_gif", "gif"} or extension in {"mp4", "m3u8", "webm", "mov"} or ".m3u8" in url.casefold() else "image"
    tweet_id = str(file_data.get("tweet_id") or post.get("tweet_id") or file_data.get("id") or post.get("id") or "")
    media_index = max(0, (_safe_int(file_data.get("num")) or 1) - 1)
    # gallery-dl's transformed user uses ``name`` for the X handle and
    # ``nick`` for the display name.  Prefer the current Tweet's author;
    # ``user`` may refer to the extractor target or a quoted/source user.
    owner = file_data.get("author") or post.get("author") or {}
    if not isinstance(owner, dict):
        owner = {}
    user = file_data.get("user") or post.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    owner_username = str(
        owner.get("name") or owner.get("screen_name") or owner.get("username")
        or file_data.get("owner_username") or post.get("owner_username")
        or user.get("name") or user.get("screen_name") or user.get("username")
        or file_data.get("username") or post.get("username") or source_username or ""
    ).lstrip("@").strip()
    display_name = str(owner.get("nick") or owner.get("display_name") or user.get("nick") or "")
    username = owner_username
    user_id = str(owner.get("id") or owner.get("rest_id") or file_data.get("user_id") or post.get("user_id") or "")
    content = str(file_data.get("content") or post.get("content") or file_data.get("text") or post.get("text") or "")
    tweet_url = f"https://x.com/{username}/status/{tweet_id}" if username and tweet_id else ""
    width = _safe_int(file_data.get("width"))
    height = _safe_int(file_data.get("height"))
    duration = _safe_float(file_data.get("duration"))
    bitrate = _safe_int(file_data.get("bitrate"))
    fallback_urls: list[str] = []
    fallback = file_data.get("_fallback") or ()
    if isinstance(fallback, str):
        fallback = (fallback,)
    if isinstance(fallback, (list, tuple)):
        fallback_urls = [str(value) for value in fallback if value and str(value) != url]
    video_sources = []
    if media_type == "video":
        video_sources.append({
            "url": url,
            "width": width,
            "height": height,
            "duration": duration,
            "bitrate": bitrate,
            "kind": "hls" if ".m3u8" in url.casefold() else "mp4",
        })
    metadata = {
        "platform": "x", "tweet_id": tweet_id, "tweet_url": tweet_url,
        "username": username, "owner_username": owner_username, "display_name": display_name,
        "user_id": user_id, "content": content,
        "date": _safe_date(file_data.get("date") or post.get("date")),
        "language": str(file_data.get("lang") or post.get("lang") or ""),
        "hashtags": file_data.get("hashtags") or post.get("hashtags") or (),
        "mentions": file_data.get("mentions") or post.get("mentions") or (),
        "likes": _safe_int(file_data.get("favorite_count") or post.get("favorite_count")),
        "replies": _safe_int(file_data.get("reply_count") or post.get("reply_count")),
        "retweets": _safe_int(file_data.get("retweet_count") or post.get("retweet_count")),
        "quotes": _safe_int(file_data.get("quote_count") or post.get("quote_count")),
        "views": _safe_int(file_data.get("view_count") or post.get("view_count")),
        "sensitive": bool(file_data.get("sensitive") or post.get("sensitive")),
        "media_index": media_index, "media_type": media_type, "media_url": url,
        "width": width, "height": height, "duration": duration, "bitrate": bitrate,
        "alt_text": str(file_data.get("description") or ""),
        "source_type": source_type, "source_query": _x_source_query(source_url),
        "metadata_source": "gallery-dl",
    }
    return {
        "type": "candidate", "platform": "x", "media_type": media_type,
        "post_id": tweet_id, "tweet_id": tweet_id, "media_index": media_index,
        "media_identity": f"{tweet_id}:{media_index}", "post_url": tweet_url,
        "tweet_url": tweet_url, "username": username, "owner_username": owner_username,
        "display_name": display_name, "user_id": user_id,
        "description": content, "caption": content, "content": content,
        "date": metadata["date"], "display_url": url if media_type == "image" else "",
        "thumbnail_url": "", "media_url": url, "video_url": url if media_type == "video" else "",
        "fallback_urls": fallback_urls, "video_sources": video_sources,
        "width": width, "height": height, "duration": duration, "bitrate": bitrate,
        "alt_text": metadata["alt_text"], "source_url": source_url,
        "source_type": source_type, "source_query": metadata["source_query"],
        "source_username": source_username, "metadata": metadata,
    }


def _x_source_query(source_url: str) -> str:
    try:
        return unquote_plus(parse_qs(urlsplit(source_url).query).get("q", [""])[0])
    except (TypeError, ValueError):
        return ""


def _matching_items(
    items: Iterator[tuple[Any, str, dict[str, Any]]],
    media_type: str,
    source_hashtag: str = "",
    *,
    platform: str = "instagram",
    source_url: str = "",
    source_type: str = "",
    source_username: str = "",
    source_board_url: str = "",
) -> Iterator[dict[str, Any]]:
    post: dict[str, Any] = {}
    for kind, _url, file_data in items:
        if kind == 2:  # gallery_dl.extractor.message.Message.Directory
            post = file_data
            continue
        if kind != 3:  # gallery_dl.extractor.message.Message.Url
            continue
        payload = _candidate_payload(
            post,
            file_data,
            source_hashtag,
            platform=platform,
            item_url=_url,
            source_url=source_url,
            source_type=source_type,
            source_username=source_username,
            source_board_url=source_board_url,
        )
        if payload is not None and media_type in {"mixed", payload["media_type"]}:
            yield payload


def _pinterest_items(instance: Any, extractor_module: Any) -> Iterator[tuple[Any, str, dict[str, Any]]]:
    """Flatten pin.it queue messages without buffering a search iterator."""
    for item in instance.items():
        kind, item_url, file_data = item
        if kind == 1 and isinstance(file_data, dict):
            child_url = str(file_data.get("url") or item_url or "")
            child = extractor_module.find(child_url) if child_url else None
            if child is not None:
                child.initialize()
                yield from child.items()
            continue
        yield item


def run(start: dict[str, Any]) -> int:
    platform = str(start.get("platform") or "instagram").strip().lower()
    url = str(start.get("url") or "")
    profile_path = Path(str(start.get("profile_path") or ""))
    cookies = start.get("cookies")
    media_type = str(start.get("media_type") or "mixed").lower()
    source_type = str(start.get("source_type") or "").strip().lower()
    source_username = str(start.get("source_username") or "").strip()
    source_board_url = str(start.get("source_board_url") or "").strip()
    search_scope = str(start.get("search_scope") or "media").strip().lower()
    is_post = bool(POST_URL_RE.match(url))
    is_author = bool(AUTHOR_URL_RE.match(url))
    is_tag = bool(TAG_URL_RE.match(url))
    is_pinterest_search = bool(PINTEREST_SEARCH_URL_RE.match(url))
    is_pinterest_pin = bool(PINTEREST_PIN_URL_RE.match(url) or PINTEREST_SHORT_URL_RE.match(url))
    is_pinterest_user = bool(PINTEREST_USER_PINS_URL_RE.match(url))
    is_pinterest_board = bool(PINTEREST_BOARD_URL_RE.match(url))
    is_pinterest = platform == "pinterest"
    is_x = platform in {"x", "twitter"}
    is_x_author = bool(X_MEDIA_URL_RE.match(url))
    is_x_search = bool(X_SEARCH_URL_RE.match(url))
    if is_pinterest:
        is_post = is_pinterest_pin
        is_author = is_pinterest_user
        is_tag = is_pinterest_search
    if is_x:
        is_author = is_x_author
        is_tag = is_x_search
    if not is_post and not is_author and not is_tag and not is_pinterest_board:
        supported = "X Author or Search" if is_x else ("Pinterest Search, Single Pin, User, or Board" if is_pinterest else "Instagram Author, Hashtag, or Single Post/Reel")
        _write({"type": "error", "message": f"only {supported} URLs are supported"})
        return 2
    if media_type not in {"image", "video", "mixed"}:
        _write({"type": "error", "message": f"unsupported media_type: {media_type}"})
        return 2
    if not is_pinterest and not is_x and not profile_path.is_dir():
        _write({"type": "error", "message": "Instagram browser profile does not exist"})
        return 2
    if not is_pinterest and not is_x and (not isinstance(cookies, dict) or not cookies.get("sessionid")):
        _write({"type": "error", "code": "AUTH_REQUIRED", "message": "Instagram cookie loaded=no; sessionid 未提供"})
        return 2

    smc_root = Path(__file__).resolve().parents[2]
    if str(smc_root) not in sys.path:
        sys.path.insert(0, str(smc_root))
    root = Path(str(start.get("gallery_dl_root") or ""))
    if root.is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import gallery_dl
        from gallery_dl import config, extractor

        if is_pinterest:
            if isinstance(cookies, dict) and cookies:
                config.set(("extractor", "pinterest"), "cookies", {str(key): str(value) for key, value in cookies.items()})
            config.set(("extractor", "pinterest"), "videos", True)
            config.set(("extractor", "pinterest"), "audio", False)
            config.set(("extractor", "pinterest"), "sections", False)
        elif is_x:
            config.set(("extractor", "twitter"), "cookies", {str(key): str(value) for key, value in cookies.items()})
            config.set(("extractor", "twitter"), "videos", True)
            config.set(("extractor", "twitter"), "retweets", False)
            config.set(("extractor", "twitter"), "quoted", False)
            config.set(("extractor", "twitter"), "search-results", "top" if search_scope == "top" else "media")
        else:
            config.set(("extractor", "instagram"), "cookies", {str(key): str(value) for key, value in cookies.items()})
            config.set(("extractor", "instagram"), "videos", True)
            config.set(("extractor", "instagram"), "audio", False)
        extractor_url = url
        if is_author and not is_pinterest:
            extractor_url = url.rstrip("/") + "/posts/"
        instance = extractor.find(extractor_url)
        if is_pinterest:
            if is_post:
                expected_subcategories = {"pin", "pinit"}
            elif is_author:
                expected_subcategories = {"allpins"}
            elif is_pinterest_board:
                expected_subcategories = {"board"}
            else:
                expected_subcategories = {"search"}
            valid = instance is not None and instance.category == "pinterest" and instance.subcategory in expected_subcategories
            if not valid:
                mode = "Single Pin" if is_post else ("User" if is_author else ("Board" if is_pinterest_board else "Search"))
                _write({"type": "error", "message": f"gallery-dl did not resolve a Pinterest {mode} extractor"})
                return 2
        elif is_x:
            expected_subcategories = {"media"} if is_x_author else {"search"}
            valid = instance is not None and instance.category == "twitter" and instance.subcategory in expected_subcategories
            if not valid:
                mode = "Author" if is_x_author else "Search"
                _write({"type": "error", "message": f"gallery-dl did not resolve an X {mode} extractor"})
                return 2
        else:
            expected_subcategories = {"post", "reel"} if is_post else ({"posts"} if is_author else {"tag"})
            valid = instance is not None and instance.category == "instagram" and instance.subcategory in expected_subcategories
        if not valid:
            mode = "Single Post/Reel" if is_post else ("Author" if is_author else "Hashtag")
            _write({"type": "error", "message": f"gallery-dl did not resolve an Instagram {mode} extractor"})
            return 2
        instance.initialize()
        cookie_loaded = bool(
            instance.cookies_check(instance.cookies_names, instance.cookies_domain, True)
            if not is_pinterest and getattr(instance, "cookies_names", None)
            else isinstance(cookies, dict) and bool(cookies)
        )
        ready = {"type": "ready", "cookie_loaded": cookie_loaded}
        version = str(getattr(gallery_dl, "__version__", "") or "").strip()
        if version:
            ready["gallery_dl_version"] = version
        _write(ready)
        if not is_pinterest and not is_x and not cookie_loaded:
            _write({"type": "error", "code": "AUTH_REQUIRED", "message": "Instagram cookie loaded=no"})
            return 2
        pinterest_cookie_required = is_pinterest_search or is_pinterest_user or is_pinterest_board
        if pinterest_cookie_required and not cookie_loaded:
            auth_message = "Pinterest Search requires login" if is_pinterest_search else "Pinterest User/Board requires login"
            _write({"type": "error", "code": "AUTH_REQUIRED", "message": auth_message})
            return 2
        if is_x and (not cookie_loaded or not isinstance(cookies, dict) or not cookies.get("auth_token") or not cookies.get("ct0")):
            _write({"type": "error", "code": "AUTH_REQUIRED", "message": "X AUTH_REQUIRED"})
            return 2
        if is_pinterest:
            # SMC owns media-type filtering so skipped videos/images still advance
            # the same lazy iterator and do not affect target_count.
            candidates = _matching_items(
                _pinterest_items(instance, extractor),
                "mixed",
                platform="pinterest",
                source_url=url,
                source_type=source_type or (
                    "search" if is_pinterest_search else
                    "user" if is_pinterest_user else
                    "board" if is_pinterest_board else
                    "single_pin"
                ),
                source_username=source_username,
                source_board_url=source_board_url,
            )
        elif is_x:
            candidates = _matching_items(
                iter(instance.items()),
                "mixed",
                platform="x",
                source_url=url,
                source_type=source_type or ("author" if is_x_author else "search"),
                source_username=source_username,
            )
        else:
            try:
                response = instance.request(
                    "https://www.instagram.com/",
                    fatal=False,
                    retries=0,
                    interval=False,
                    expected=(401, 403),
                )
            except Exception as exc:
                message = str(exc).lower()
                if any(marker in message for marker in ("login", "challenge", "checkpoint", "401", "403")):
                    _write({"type": "error", "code": "AUTH_REQUIRED", "message": "Instagram auth invalid"})
                    return 2
                raise
            response_path = str(getattr(response, "url", "") or "").lower()
            if getattr(response, "status_code", 0) in {401, 403} or any(
                marker in response_path for marker in ("/accounts/login", "/challenge", "/checkpoint")
            ):
                _write({"type": "error", "code": "AUTH_REQUIRED", "message": "Instagram auth invalid"})
                return 2
            candidates = _matching_items(iter(instance.items()), "mixed", str(start.get("source_hashtag") or ""))
    except Exception as exc:
        if is_x and _is_x_auth_error(exc):
            _write({"type": "error", "code": "AUTH_REQUIRED", "message": "X AUTH_REQUIRED"})
            return 2
        if is_pinterest and _is_pinterest_auth_error(exc):
            auth_message = "Pinterest Search requires login" if is_pinterest_search else "Pinterest User/Board requires login"
            _write({"type": "error", "code": "AUTH_REQUIRED", "message": auth_message})
            return 2
        _write({"type": "error", "message": f"gallery-dl initialization failed: {type(exc).__name__}: {exc}"})
        return 1

    paused = False
    while True:
        command = _read_command()
        if command is None or command == "stop":
            return 0
        if command == "pause":
            paused = True
            continue
        if command == "resume":
            paused = False
            continue
        if command != "next":
            _write({"type": "error", "message": f"unexpected worker command: {command}"})
            return 2
        if paused:
            continue
        try:
            _write(next(candidates))
        except StopIteration:
            _write({"type": "exhausted"})
            return 0
        except Exception as exc:
            if is_x and _is_x_auth_error(exc):
                _write({"type": "error", "code": "AUTH_REQUIRED", "message": "X AUTH_REQUIRED"})
                return 2
            if is_pinterest and _is_pinterest_auth_error(exc):
                auth_message = "Pinterest Search requires login" if is_pinterest_search else "Pinterest User/Board requires login"
                _write({"type": "error", "code": "AUTH_REQUIRED", "message": auth_message})
                return 2
            _write({"type": "error", "message": f"gallery-dl extraction failed: {type(exc).__name__}: {exc}"})
            return 1


def main() -> int:
    _configure_stdio_utf8()
    try:
        line = _readline_utf8(sys.stdin)
    except UnicodeError as exc:
        _write({"type": "error", "message": f"invalid UTF-8 start JSON: {exc}"})
        return 2
    if not line:
        return 2
    try:
        start = json.loads(line)
    except json.JSONDecodeError as exc:
        _write({"type": "error", "message": f"invalid start JSON: {exc}"})
        return 2
    if not isinstance(start, dict) or start.get("type") != "start":
        _write({"type": "error", "message": "first worker message must be type=start"})
        return 2
    return run(start)


if __name__ == "__main__":
    raise SystemExit(main())
