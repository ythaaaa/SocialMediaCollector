"""Manual Instagram discovery harness; it never downloads or mutates task data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.browser import BrowserSessionManager, InstagramCollector
from src.utils.paths import instagram_browser_profile_directory


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme and parsed.netloc else value[:160]


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Instagram post media without downloading.")
    parser.add_argument("--mode", choices=("author", "single_post"), required=True)
    parser.add_argument("--source", help="Instagram username/profile URL or post/Reel URL")
    parser.add_argument("--max-scrolls", type=int, default=3)
    parser.add_argument("--wanted", type=int, default=20)
    parser.add_argument("--media-type", choices=("image", "video", "mixed"), default="mixed")
    args = parser.parse_args()

    source = (args.source or input("Instagram 作者或帖子 URL：")).strip()
    profile = instagram_browser_profile_directory()
    manager = BrowserSessionManager(profile, print)
    collector = InstagramCollector(profile, print)
    try:
        if not manager.is_login_running() and not manager.is_running():
            manager.open_manual_login_browser("https://www.instagram.com/")
            input("请在普通 Chrome 中人工登录/完成验证并关闭该 Chrome，然后按 Enter 开始扫描；不会保存密码。")
        if manager.is_login_running():
            print("Instagram 登录 Chrome 仍在运行，请先关闭后再开始扫描。")
            return 1
        if not manager.is_running():
            manager.open_collection_browser("https://www.instagram.com/")
        if not manager.endpoint:
            print("BrowserSession endpoint 不可用。")
            return 1
        collector.connect_over_cdp(manager.endpoint)
        common = dict(
            wanted=args.wanted,
            max_scrolls=args.max_scrolls,
            should_stop=lambda: False,
            should_pause=lambda: None,
            on_found=lambda count: print(f"discovered_valid_media_count={count}"),
            on_candidate=lambda candidate: print(
                f"candidate identity={candidate.media_identity} type={candidate.media_type} "
                f"size={candidate.width}x{candidate.height} url={safe_url(candidate.media_url)}"
            ),
            media_type=args.media_type,
        )
        if args.mode == "author":
            candidates = collector.collect_author(source, **common)
        else:
            candidates = collector.collect_single_post(source, **common)
        print(f"summary candidates={len(candidates)}")
        print(f"diagnostics={collector.last_scan_diagnostics.to_dict()}")
        print(f"video_source_types={sorted(collector.last_video_source_types)}")
        return 0
    finally:
        # Detach/close the worker page only. The app-managed Chrome remains open
        # for the user's inspection and subsequent manual tests.
        collector.close()


if __name__ == "__main__":
    raise SystemExit(main())
