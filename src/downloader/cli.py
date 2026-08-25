from __future__ import annotations

import argparse
from pathlib import Path

from src.downloader import DownloadEngine
from src.models import TaskConfig
from src.utils.paths import application_data_directory

def main() -> None:
    parser = argparse.ArgumentParser(description="Pinterest Local Downloader")
    parser.add_argument("keyword", nargs="?", default="")
    parser.add_argument("--mode", choices=("search", "home", "current_page"), default="search")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-width", type=int, default=800)
    parser.add_argument("--min-height", type=int, default=800)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    config = TaskConfig(
        keyword=args.keyword,
        target_count=args.count,
        output_root=args.output,
        min_width=args.min_width,
        min_height=args.min_height,
        interval_seconds=args.interval,
        collection_mode=args.mode,
    )
    engine = DownloadEngine(config, application_data_directory() / "browser-data", print)
    task_dir = engine.run()
    print(f"任务目录：{task_dir}")

if __name__ == "__main__": main()
