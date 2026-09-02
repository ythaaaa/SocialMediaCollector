from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from src.utils.x import normalize_x_author
from src.platforms import get_platform_spec


TASK_STATUSES = frozenset({"pending", "running", "paused", "completed", "failed", "stopped", "interrupted"})


def _looks_like_url(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(
        (
            "http://",
            "https://",
            "x.com/",
            "www.x.com/",
            "twitter.com/",
            "www.twitter.com/",
            "pinterest.com/",
            "www.pinterest.com/",
        )
    )


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
    search_scope: str = "media"
    only_prompt_posts: bool = False
    # V2 queue fields. Legacy fields above remain the source of truth for V1.
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "pending"

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

    @property
    def source_value(self) -> str:
        """Unified V2 source field while retaining legacy keyword/source fields."""
        return self.source_identifier or self.author_username or self.keyword

    @property
    def canonical_source_value(self) -> str:
        if self.canonical_platform() == "x" and self.canonical_collection_mode() == "author":
            return self.canonical_author()[0]
        return self.source_value.strip()

    @property
    def download_interval(self) -> float:
        return self.interval_seconds

    @property
    def save_dir(self) -> Path:
        return self.output_root

    @property
    def only_author_posts(self) -> bool:
        return self.only_author_content

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

    def canonical_search_scope(self) -> str:
        raw = (self.search_scope or "media").strip().lower()
        return raw or "media"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskConfig":
        """Load current and legacy task.json data without requiring new fields."""
        platform = str(data.get("platform") or "pinterest")
        source_value = str(data.get("source_value") or "")
        collection_mode = str(data.get("collection_mode") or data.get("mode") or "search")
        return cls(
            keyword=str(data.get("keyword") or (source_value if platform == "pinterest" else "")),
            target_count=int(data.get("target_count", 0)),
            output_root=Path(str(data.get("output_root") or data.get("save_dir") or ".")),
            min_width=int(data.get("min_width", 800)),
            min_height=int(data.get("min_height", 800)),
            interval_seconds=float(data.get("interval_seconds", data.get("download_interval", 1.0))),
            max_scrolls=int(data.get("max_scrolls", 100)),
            retry_count=int(data.get("retry_count", 3)),
            mode=str(data.get("mode") or "keyword_search"),
            collection_mode=collection_mode,
            platform=platform,
            source_identifier=str(data.get("source_identifier") or data.get("source_value") or ""),
            source_page_url=str(data.get("source_page_url") or ""),
            author_username=str(data.get("author_username") or ""),
            only_author_content=bool(data.get("only_author_content", data.get("only_author_posts", True))),
            media_type=str(data.get("media_type") or "image"),
            search_scope=str(data.get("search_scope") or "media"),
            only_prompt_posts=bool(data.get("only_prompt_posts", False)),
            task_id=str(data.get("task_id") or uuid4().hex),
            status=str(data.get("status") or "pending"),
        )

    def validate(self) -> None:
        platform = self.canonical_platform()
        collection_mode = self.canonical_collection_mode()
        spec = get_platform_spec(platform)
        if spec is None or not spec.enabled or collection_mode not in spec.collection_modes:
            raise ValueError("unsupported collection mode")
        if self.status not in TASK_STATUSES:
            raise ValueError("任务状态无效")
        if platform == "pinterest" and collection_mode == "search":
            if not self.keyword.strip():
                raise ValueError("搜索关键词不能为空")
            if _looks_like_url(self.keyword):
                raise ValueError("Pinterest 关键词搜索请输入搜索关键词，不要填写网页地址。")
        if platform == "x":
            if self.canonical_media_type() not in {"image", "video", "mixed"}:
                raise ValueError("X 下载内容类型无效")
            if collection_mode == "search" and self.canonical_search_scope() not in {"media", "top"}:
                raise ValueError("X 搜索范围无效")
            if collection_mode == "author":
                self.canonical_author()
            elif collection_mode == "search" and not self.source_value.strip():
                raise ValueError("X 搜索关键词不能为空")
        elif platform == "instagram":
            if self.canonical_media_type() not in set(spec.media_types):
                raise ValueError("Instagram 下载内容类型无效")
            if not self.source_value.strip():
                raise ValueError("Instagram 目标不能为空")
        elif self.canonical_media_type() not in {"image", "video", "mixed"}:
            raise ValueError("Pinterest 下载内容类型无效")
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
    post_id: str = ""
    post_text: str = ""
    prompt_detected: bool = False
    prompt: str = ""
    posted_at: str = ""
    collected_at: str = ""
    shortcode: str = ""
    caption: str = ""
    media_index: int = 0
    thumbnail_url: str = ""
    metadata_source: str = ""
    source: str = ""

    def row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskStats:
    discovered: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    duplicate_count: int = 0
    current_status: str = "等待开始"
    records: list[DownloadRecord] = field(default_factory=list)
