"""Declarative platform capabilities used by the desktop UI.

Collectors remain platform-specific for now.  This registry only describes
the capabilities that can be presented by the UI and the future platforms
that are intentionally disabled during the V2.1 foundation phase.
"""

from dataclasses import dataclass


MODE_LABELS = {
    "search": "关键词搜索",
    "home": "首页推荐",
    "current_page": "当前页面",
    "author": "作者主页",
    "single_post": "单条链接",
    "channel": "频道",
    "single_video": "单条视频",
}

MEDIA_TYPE_LABELS = {
    "image": "仅图片",
    "video": "仅视频",
    "mixed": "图片 + 视频",
    "thumbnail": "仅封面",
    "video_thumbnail": "视频 + 封面",
}


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    label: str
    icon: str
    enabled: bool
    collection_modes: tuple[str, ...]
    media_types: tuple[str, ...]
    default_mode: str
    default_media_type: str


PLATFORM_REGISTRY: dict[str, PlatformSpec] = {
    "pinterest": PlatformSpec(
        key="pinterest",
        label="Pinterest",
        icon="icons/platforms/pinterest.svg",
        enabled=True,
        collection_modes=("search", "home", "current_page"),
        media_types=("image", "video", "mixed"),
        default_mode="search",
        default_media_type="image",
    ),
    "x": PlatformSpec(
        key="x",
        label="X（Twitter）",
        icon="icons/platforms/x.svg",
        enabled=True,
        collection_modes=("author", "search", "home"),
        media_types=("image", "video", "mixed"),
        default_mode="author",
        default_media_type="image",
    ),
    "instagram": PlatformSpec(
        key="instagram",
        label="Instagram · 开发中",
        icon="icons/platforms/instagram.svg",
        enabled=False,
        collection_modes=("author", "single_post"),
        media_types=("image", "video", "mixed"),
        default_mode="author",
        default_media_type="image",
    ),
    "youtube": PlatformSpec(
        key="youtube",
        label="YouTube · 开发中",
        icon="icons/platforms/youtube.svg",
        enabled=False,
        collection_modes=("search", "channel", "single_video"),
        media_types=("video", "thumbnail", "video_thumbnail"),
        default_mode="search",
        default_media_type="video",
    ),
}


def platform_specs() -> tuple[PlatformSpec, ...]:
    return tuple(PLATFORM_REGISTRY.values())


def get_platform_spec(key: str) -> PlatformSpec | None:
    return PLATFORM_REGISTRY.get((key or "").strip().lower())


def mode_label(key: str) -> str:
    return MODE_LABELS.get(key, key)


def media_type_label(key: str) -> str:
    return MEDIA_TYPE_LABELS.get(key, key)
