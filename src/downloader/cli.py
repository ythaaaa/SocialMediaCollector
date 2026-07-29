from __future__ import annotations

import argparse
from pathlib import Path

from src.downloader import DownloadEngine
from src.models import TaskConfig

def main() -> None:
    parser = argparse.ArgumentParser(description="Pinterest Local Downloader")
    parser.add_argument("keyword")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--min-width", type=int, default=800)
    parser.add_argument("--min-height", type=int, default=800)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    config = TaskConfig(args.keyword, args.count, args.output, args.min_width, args.min_height, args.interval)
    engine = DownloadEngine(config, Path("browser-data"), print)
    task_dir = engine.run()
    print(f"任务目录：{task_dir}")

if __name__ == "__main__": main()
