from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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

    def validate(self) -> None:
        if not self.keyword.strip():
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
