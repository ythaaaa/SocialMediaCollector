from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping


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
        )

    def validate(self) -> None:
        collection_mode = self.canonical_collection_mode()
        if collection_mode not in {"search", "home", "current_page"}:
            raise ValueError("unsupported collection mode")
        if collection_mode == "search" and not self.keyword.strip():
            raise ValueError("搜索关键词不能为空")
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
