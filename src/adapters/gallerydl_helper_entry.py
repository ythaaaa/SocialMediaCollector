"""Console entry point for the shared frozen gallery-dl JSONL helper."""

from __future__ import annotations

import json
import sys
from typing import Any

from src.adapters import gallerydl_worker


HELPER_NAME = "instagram-gallerydl-helper"
GALLERYDL_VERSION = "1.32.14"
GALLERYDL_COMMIT = "8b9a56d6b1a53bc13a80894625fd58b24de3063a"


def version_text() -> str:
    return "\n".join((
        HELPER_NAME,
        f"gallery-dl {GALLERYDL_VERSION}",
        f"gallery-dl commit {GALLERYDL_COMMIT}",
    ))


def _self_test_payload() -> dict[str, Any]:
    """Exercise the frozen dynamic extractor registry without network access."""
    from gallery_dl import extractor

    urls = {
        "post": "https://www.instagram.com/p/POST123/",
        "reel": "https://www.instagram.com/reel/REEL123/",
        "posts": "https://www.instagram.com/example/posts/",
        "tag": "https://www.instagram.com/explore/tags/aiart/",
        "twitter-media": "https://x.com/example/media",
        "twitter-search": "https://x.com/search?q=aiart",
    }
    resolved: dict[str, str] = {}
    for name, url in urls.items():
        instance = extractor.find(url)
        expected_category = "twitter" if name.startswith("twitter-") else "instagram"
        if instance is None or instance.category != expected_category:
            raise RuntimeError(f"{name} extractor unavailable")
        resolved[name] = str(instance.subcategory)
    return {
        "type": "self_test",
        "gallery_dl_version": GALLERYDL_VERSION,
        "extractors": resolved,
        "unicode": "测试 😂 📷 한국어 日本語",
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["--version"]:
        print(version_text())
        return 0
    if arguments == ["--self-test"]:
        try:
            gallerydl_worker._configure_stdio_utf8()
            gallerydl_worker._write(_self_test_payload())
            return 0
        except Exception as exc:
            gallerydl_worker._write({
                "type": "error",
                "message": f"helper self-test failed: {type(exc).__name__}: {exc}",
            })
            return 1
    return gallerydl_worker.main()


if __name__ == "__main__":
    raise SystemExit(main())
