from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

from src.utils.x import normalize_x_author


@dataclass(frozen=True)
class TaskConfig:
    keyword: str
    target_count: int
    output_root: Path
    min_width: int = 800
    min_height: int = 800
    interval_seconds: float = 1.0
    max_scrolls: int = 100
    retry_count: int = 3
    # ``mode`` was introduced by the first home-feed implementation. Keep it
    # for compatibility with existing callers and task.json files while using
    # the canonical ``collection_mode`` values for new code.
    mode: str = "keyword_search"
    collection_mode: str = ""
    platform: str = "pinterest"
    source_identifier: str = ""
    source_page_url: str = ""
    author_username: str = ""
    only_author_content: bool = True
    media_type: str = "image"

    def canonical_platform(self) -> str:
        raw = (self.platform or "pinterest").strip().lower()
        aliases = {"pinterest": "pinterest", "x": "x", "twitter": "x"}
        return aliases.get(raw, raw)

    def canonical_collection_mode(self) -> str:
        raw = (self.collection_mode or self.mode or "search").strip()
        aliases = {
            "keyword_search": "search",
            "home_feed": "home",
            "search": "search",
            "home": "home",
            "current_page": "current_page",
        }
        return aliases.get(raw, raw)

    def canonical_author(self) -> tuple[str, str]:
        return normalize_x_author(self.source_identifier or self.author_username or self.keyword)

    def canonical_media_type(self) -> str:
        raw = (self.media_type or "image").strip().lower()
        aliases = {
            "images": "image",
            "image_only": "image",
            "videos": "video",
            "video_only": "video",
            "image_video": "mixed",
            "images_videos": "mixed",
        }
        return aliases.get(raw, raw)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskConfig":
        """Load current and legacy task.json data without requiring new fields."""
        collection_mode = str(data.get("collection_mode") or data.get("mode") or "search")
        return cls(
            keyword=str(data.get("keyword") or ""),
            target_count=int(data.get("target_count", 0)),
            output_root=Path(str(data.get("output_root") or ".")),
            min_width=int(data.get("min_width", 800)),
            min_height=int(data.get("min_height", 800)),
            interval_seconds=float(data.get("interval_seconds", 1.0)),
            max_scrolls=int(data.get("max_scrolls", 100)),
            retry_count=int(data.get("retry_count", 3)),
            mode=str(data.get("mode") or "keyword_search"),
            collection_mode=collection_mode,
            platform=str(data.get("platform") or "pinterest"),
            source_identifier=str(data.get("source_identifier") or ""),
            source_page_url=str(data.get("source_page_url") or ""),
            author_username=str(data.get("author_username") or ""),
            only_author_content=bool(data.get("only_author_content", True)),
            media_type=str(data.get("media_type") or "image"),
        )

    def validate(self) -> None:
        platform = self.canonical_platform()
        collection_mode = self.canonical_collection_mode()
        supported_modes = {"search", "home", "current_page"} if platform == "pinterest" else {"author", "home"} if platform == "x" else set()
        if not supported_modes or collection_mode not in supported_modes:
            raise ValueError("unsupported collection mode")
        if platform == "pinterest" and collection_mode == "search" and not self.keyword.strip():
            raise ValueError("搜索关键词不能为空")
        if platform == "x":
            if self.canonical_media_type() not in {"image", "video", "mixed"}:
                raise ValueError("X 下载内容类型无效")
            if collection_mode == "author":
                self.canonical_author()
        elif self.canonical_media_type() != "image":
            raise ValueError("Pinterest 仅支持图片")
        if self.target_count < 1:
            raise ValueError("下载数量必须大于 0")
        if self.min_width < 1 or self.min_height < 1:
            raise ValueError("最小尺寸必须大于 0")
        if self.interval_seconds < 0:
            raise ValueError("下载间隔不能为负数")


@dataclass
class DownloadRecord:
    filename: str
    keyword: str
    pin_url: str
    image_url: str
    width: int | None = None
    height: int | None = None
    file_size: int | None = None
    image_format: str = ""
    downloaded_at: str = ""
    content_hash: str = ""
    status: str = ""
    reason: str = ""
    collection_mode: str = "search"
    source_page_url: str = ""
    platform: str = "pinterest"
    author_username: str = ""
    post_url: str = ""
    only_author_content: bool = True
    media_type: str = "image"
    media_url: str = ""
    duration: float | None = None
    video_width: int | None = None
    video_height: int | None = None
    bitrate: int | None = None

    def row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskStats:
    discovered: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    current_status: str = "等待开始"
    records: list[DownloadRecord] = field(default_factory=list)
