from __future__ import annotations

from urllib.parse import quote, unquote, urlsplit


INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}


def normalize_hashtag(value: str) -> str:
    """Normalize a hashtag input or Instagram tag URL to its tag value."""
    raw = unquote((value or "").strip())
    if not raw:
        raise ValueError("Instagram Hashtag 不能为空")

    if "://" in raw or raw.lower().startswith(("www.instagram.com/", "instagram.com/")):
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower().rstrip(".") not in INSTAGRAM_HOSTS:
            raise ValueError("Instagram Hashtag 地址无效")
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) != 3 or parts[0].casefold() != "explore" or parts[1].casefold() != "tags":
            raise ValueError("请输入 Instagram /explore/tags/<tag>/ 地址")
        raw = parts[2]

    tag = raw.lstrip("#").strip()
    if not tag or any(char.isspace() or char in '<>:"/\\|?#*' for char in tag):
        raise ValueError("Instagram Hashtag 格式无效")
    return tag.casefold()


def instagram_hashtag_url(hashtag: str) -> str:
    normalized = normalize_hashtag(hashtag)
    return f"https://www.instagram.com/explore/tags/{quote(normalized, safe='._-')}/"
