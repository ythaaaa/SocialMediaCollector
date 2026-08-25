from __future__ import annotations

import shutil
import os
from pathlib import Path
from typing import Callable

from playwright.sync_api import BrowserContext, Page, sync_playwright


def find_chrome_path() -> str | None:
    """Find the installed system Chrome, never a Playwright runtime binary."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        str(local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe") if local_app_data else None,
        r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def regular_chrome_login_command(chrome_path: str, profile_dir: Path, target_url: str) -> list[str]:
    """Build the deliberately minimal command used for human login."""
    return [chrome_path, f"--user-data-dir={Path(profile_dir)}", target_url]


class BrowserSession:
    """Own one visible, persistent Playwright browser profile."""

    _LOCK_NAMES = {"singletonlock", "singletoncookie", "singletonsocket", "lockfile", "lock"}

    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        self.profile_dir = Path(profile_dir)
        self.log = log
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def context(self) -> BrowserContext | None:
        return self._context

    @property
    def page(self) -> Page | None:
        return self._page

    @classmethod
    def profile_is_locked(cls, profile_dir: Path) -> bool:
        profile = Path(profile_dir)
        try:
            return any(item.name.lower() in cls._LOCK_NAMES for item in profile.iterdir())
        except FileNotFoundError:
            return False

    def open(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        if self.profile_is_locked(self.profile_dir):
            raise RuntimeError(
                "采集器专用浏览器 Profile 正在被另一个 Chrome 或采集器实例占用；"
                "请先关闭该浏览器窗口后再重试。"
            )

        self._playwright = sync_playwright().start()
        options: dict[str, object] = {
            "headless": False,
            "viewport": {"width": 1366, "height": 900},
        }
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), **options
            )
        except Exception as exc:
            chrome = self._find_chrome()
            if not chrome:
                self.close()
                raise RuntimeError(
                    "未找到 Playwright Chromium，也未找到本机 Chrome；"
                    "请执行 playwright install chromium"
                ) from exc
            self.log(f"Playwright Chromium 不可用，改用本机 Chrome：{chrome}")
            options["executable_path"] = chrome
            try:
                self._context = self._playwright.chromium.launch_persistent_context(
                    str(self.profile_dir), **options
                )
            except Exception:
                self.close()
                raise

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def close(self) -> None:
        context, playwright = self._context, self._playwright
        self._context = None
        self._page = None
        self._playwright = None
        if context:
            context.close()
        if playwright:
            playwright.stop()

    @staticmethod
    def _find_chrome() -> str | None:
        return find_chrome_path()
