"""Small, side-effect-free Pinterest URL helpers."""

from __future__ import annotations

import re
from urllib.parse import quote, quote_plus, unquote, urlsplit


_PINTEREST_HOST_RE = re.compile(r"(?:^|\.)pinterest\.[^./]+$", re.IGNORECASE)
_PIN_IT_HOST = "pin.it"
_PINTEREST_RESERVED_USER_SEGMENTS = frozenset({"pin", "search", "login", "settings", "ideas", "business"})


def _is_pinterest_host(host: str) -> bool:
    host = (host or "").casefold().rstrip(".")
    return bool(_PINTEREST_HOST_RE.search(host) or host == _PIN_IT_HOST)


def pinterest_search_url(query: str) -> str:
    value = " ".join(str(query or "").split())
    if not value:
        raise ValueError("Pinterest 搜索关键词不能为空")
    return f"https://www.pinterest.com/search/pins/?q={quote_plus(value)}"


def normalize_pinterest_pin_url(value: str) -> str:
    """Validate a public Pin or pin.it URL while preserving its URL details."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Pinterest Pin 链接不能为空")
    candidate = raw if "://" in raw else f"https://{raw}"
    parts = urlsplit(candidate)
    if parts.scheme.casefold() not in {"http", "https"} or not _is_pinterest_host(parts.hostname or ""):
        raise ValueError("请输入 Pinterest Pin 链接")
    if (parts.hostname or "").casefold() == _PIN_IT_HOST:
        if not parts.path.strip("/"):
            raise ValueError("请输入有效的 Pinterest Pin 链接")
        return candidate
    if not re.match(r"^/pin/[^/?#]+/?$", parts.path, re.IGNORECASE):
        raise ValueError("请输入 Pinterest Pin 链接，例如：https://www.pinterest.com/pin/123/")
    return candidate


def pinterest_pin_id(value: str) -> str:
    parts = urlsplit(str(value or ""))
    match = re.match(r"^/pin/([^/?#]+)/?$", parts.path, re.IGNORECASE)
    return match.group(1) if match else ""


def _pinterest_web_url(value: str) -> tuple[str, object]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Pinterest 来源不能为空")
    candidate = raw if "://" in raw else f"https://{raw}"
    parts = urlsplit(candidate)
    if parts.scheme.casefold() not in {"http", "https"} or not _is_pinterest_host(parts.hostname or ""):
        raise ValueError("请输入 Pinterest 用户或 Board 链接")
    return candidate, parts


def normalize_pinterest_username(value: str) -> str:
    """Normalize a Pinterest username or supported user-page URL.

    User collection intentionally keeps the product meaning ``All Pins``.
    Pinterest's profile navigation may expose ``/_created/`` or ``/_saved/``;
    both are accepted as user input and normalized to the username only.
    """
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Pinterest 用户名不能为空")
    if "://" not in raw and "/" not in raw and "?" not in raw and "#" not in raw:
        username = raw.lstrip("@").strip()
    else:
        _, parts = _pinterest_web_url(raw)
        if (parts.hostname or "").casefold() == _PIN_IT_HOST:
            raise ValueError("请输入 Pinterest 用户主页或用户名")
        segments = [unquote(item) for item in parts.path.split("/") if item]
        if len(segments) == 2 and segments[1].casefold() in {"pins", "_created", "_saved"}:
            username = segments[0]
        elif len(segments) == 1:
            username = segments[0]
        else:
            raise ValueError("请输入 Pinterest 用户主页、用户名或用户 Pins 链接")
    if not username or username.casefold() in _PINTEREST_RESERVED_USER_SEGMENTS:
        raise ValueError("请输入有效的 Pinterest 用户名")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", username):
        raise ValueError("Pinterest 用户名格式无效")
    return username


def pinterest_user_url(username: str) -> str:
    normalized = normalize_pinterest_username(username)
    return f"https://www.pinterest.com/{quote(normalized, safe='._-')}/pins/"


def normalize_pinterest_board_url(value: str) -> str:
    """Validate and normalize a two-segment Pinterest Board URL."""
    _, parts = _pinterest_web_url(value)
    segments = [unquote(item) for item in parts.path.split("/") if item]
    if len(segments) != 2:
        raise ValueError("请输入 Pinterest Board 链接，例如：https://www.pinterest.com/username/board-name/")
    username, board = segments
    if username.casefold() in _PINTEREST_RESERVED_USER_SEGMENTS or board.casefold() in {
        "pin", "pins", "search", "_created", "login", "settings",
    }:
        raise ValueError("请输入有效的 Pinterest Board 链接")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", username) or not re.fullmatch(r"[^/?#]{1,200}", board):
        raise ValueError("Pinterest Board 链接格式无效")
    return (
        f"https://www.pinterest.com/{quote(username, safe='._-')}/"
        f"{quote(board, safe='._-')}/"
    )


def pinterest_board_parts(value: str) -> tuple[str, str]:
    normalized = normalize_pinterest_board_url(value)
    parts = [unquote(item) for item in urlsplit(normalized).path.split("/") if item]
    return parts[0], parts[1]
