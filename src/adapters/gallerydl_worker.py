"""Isolated JSONL worker for the opt-in Instagram gallery-dl adapter."""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator


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


def _candidate_payload(post: dict[str, Any], file_data: dict[str, Any], source_hashtag: str = "") -> dict[str, Any]:
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


def _matching_items(items: Iterator[tuple[Any, str, dict[str, Any]]], media_type: str, source_hashtag: str = "") -> Iterator[dict[str, Any]]:
    post: dict[str, Any] = {}
    for kind, _url, file_data in items:
        if kind == 2:  # gallery_dl.extractor.message.Message.Directory
            post = file_data
            continue
        if kind != 3:  # gallery_dl.extractor.message.Message.Url
            continue
        payload = _candidate_payload(post, file_data, source_hashtag)
        if media_type in {"mixed", payload["media_type"]}:
            yield payload


def run(start: dict[str, Any]) -> int:
    url = str(start.get("url") or "")
    profile_path = Path(str(start.get("profile_path") or ""))
    cookies = start.get("cookies")
    media_type = str(start.get("media_type") or "mixed").lower()
    is_post = bool(POST_URL_RE.match(url))
    is_author = bool(AUTHOR_URL_RE.match(url))
    is_tag = bool(TAG_URL_RE.match(url))
    if not is_post and not is_author and not is_tag:
        _write({"type": "error", "message": "only Instagram Author, Hashtag, or Single Post/Reel URLs are supported"})
        return 2
    if media_type not in {"image", "video", "mixed"}:
        _write({"type": "error", "message": f"unsupported media_type: {media_type}"})
        return 2
    if not profile_path.is_dir():
        _write({"type": "error", "message": "Instagram browser profile does not exist"})
        return 2
    if not isinstance(cookies, dict) or not cookies.get("sessionid"):
        _write({"type": "error", "code": "AUTH_REQUIRED", "message": "Instagram cookie loaded=no; sessionid 未提供"})
        return 2

    smc_root = Path(__file__).resolve().parents[2]
    if str(smc_root) not in sys.path:
        sys.path.insert(0, str(smc_root))
    root = Path(str(start.get("gallery_dl_root") or ""))
    if root.is_dir() and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from gallery_dl import config, extractor

        config.set(("extractor", "instagram"), "cookies", {str(key): str(value) for key, value in cookies.items()})
        config.set(("extractor", "instagram"), "videos", True)
        config.set(("extractor", "instagram"), "audio", False)
        extractor_url = url
        if is_author:
            extractor_url = url.rstrip("/") + "/posts/"
        instance = extractor.find(extractor_url)
        expected_subcategories = {"post", "reel"} if is_post else ({"posts"} if is_author else {"tag"})
        if instance is None or instance.category != "instagram" or instance.subcategory not in expected_subcategories:
            mode = "Single Post/Reel" if is_post else ("Author" if is_author else "Hashtag")
            _write({"type": "error", "message": f"gallery-dl did not resolve an Instagram {mode} extractor"})
            return 2
        instance.initialize()
        cookie_loaded = bool(instance.cookies_check(instance.cookies_names, instance.cookies_domain, True))
        _write({"type": "ready", "cookie_loaded": cookie_loaded})
        if not cookie_loaded:
            _write({"type": "error", "code": "AUTH_REQUIRED", "message": "Instagram cookie loaded=no"})
            return 2
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
        # SMC owns media-type filtering so skipped videos/images still advance
        # the same lazy iterator and do not affect target_count.
        candidates = _matching_items(iter(instance.items()), "mixed", str(start.get("source_hashtag") or ""))
    except Exception as exc:
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
