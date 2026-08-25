from __future__ import annotations

import argparse
from pathlib import Path

from src.downloader import DownloadEngine
from src.models import TaskConfig
from src.utils.paths import browser_profile_directory

def main() -> None:
    parser = argparse.ArgumentParser(description="Social Media Asset Collector")
    parser.add_argument("keyword", nargs="?", default="")
    parser.add_argument("--platform", choices=("pinterest", "x"), default="pinterest")
    parser.add_argument("--mode", choices=("search", "home", "current_page", "author"), default="search")
    parser.add_argument("--media-type", choices=("image", "video", "mixed"), default="image")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-width", type=int, default=800)
    parser.add_argument("--min-height", type=int, default=800)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    config = TaskConfig(
        keyword=args.keyword if args.platform == "pinterest" else "",
        target_count=args.count,
        output_root=args.output,
        min_width=args.min_width,
        min_height=args.min_height,
        interval_seconds=args.interval,
        platform=args.platform,
        source_identifier=args.keyword if args.platform == "x" else "",
        collection_mode=args.mode,
        media_type=args.media_type if args.platform == "x" else "image",
    )
    engine = DownloadEngine(config, browser_profile_directory(), print)
    task_dir = engine.run()
    print(f"任务目录：{task_dir}")

if __name__ == "__main__": main()
