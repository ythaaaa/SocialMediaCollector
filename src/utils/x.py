from __future__ import annotations

import re
from urllib.parse import urlparse


_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def normalize_x_author(value: str) -> tuple[str, str]:
    """Return (username, canonical profile URL) for an X author input."""
    raw = value.strip()
    if not raw:
        raise ValueError("X 作者用户名或主页地址不能为空")

    if raw.startswith(("https://", "http://")):
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise ValueError("请输入 x.com 或 twitter.com 的作者主页地址")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise ValueError("X 作者地址应为单个作者主页，例如 https://x.com/username")
        candidate = parts[0]
    else:
        candidate = raw.lstrip("@").strip().strip("/")
        if "/" in candidate:
            raise ValueError("X 用户名格式不正确")

    if not _USERNAME_RE.fullmatch(candidate):
        raise ValueError("X 用户名只能包含字母、数字和下划线，长度为 1-15 个字符")
    return candidate, f"https://x.com/{candidate}"
