"""Small, side-effect-free Pinterest URL helpers."""

from __future__ import annotations

import re
from urllib.parse import quote_plus, urlsplit


_PINTEREST_HOST_RE = re.compile(r"(?:^|\.)pinterest\.[^./]+$", re.IGNORECASE)
_PIN_IT_HOST = "pin.it"


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
