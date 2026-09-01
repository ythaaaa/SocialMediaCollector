from __future__ import annotations

import shutil
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.request import urlopen

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
        self._browser = None
        self._connected_over_cdp = False

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

    def connect_over_cdp(self, endpoint: str) -> None:
        """Attach to an app-managed normal Chrome without creating a profile context."""
        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
            contexts = self._browser.contexts
            if not contexts:
                raise RuntimeError("托管浏览器没有可用的浏览器上下文。")
            self._context = contexts[0]
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._connected_over_cdp = True
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        context, playwright, browser, connected = self._context, self._playwright, self._browser, self._connected_over_cdp
        self._context = None
        self._page = None
        self._playwright = None
        self._browser = None
        self._connected_over_cdp = False
        if connected:
            if browser:
                # Playwright Python exposes ``close`` for a CDP connection;
                # it detaches the client without closing the user's Chrome.
                browser.close()
        elif context:
            context.close()
        if playwright:
            playwright.stop()

    @staticmethod
    def _find_chrome() -> str | None:
        return find_chrome_path()


class BrowserSessionManager:
    """Own the app-launched Chrome and expose one reusable profile endpoint."""

    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        self.profile_dir = Path(profile_dir)
        self.log = log
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @property
    def endpoint(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None

    def _endpoint_ready(self) -> bool:
        endpoint = self.endpoint
        if not endpoint:
            return False
        try:
            with urlopen(f"{endpoint}/json/version", timeout=0.5):
                return True
        except Exception:
            return False

    def is_running(self) -> bool:
        if self.process and self.process.poll() is not None:
            self.process = None
            self.port = None
            return False
        return bool(self.process and self._endpoint_ready())

    def open_browser(self, target_url: str) -> None:
        if self.is_running():
            return
        chrome_path = find_chrome_path()
        if not chrome_path:
            raise RuntimeError("未找到本机 Google Chrome，无法打开托管浏览器。")
        if BrowserSession.profile_is_locked(self.profile_dir):
            raise RuntimeError("请先关闭当前占用专用 Profile 的浏览器，再重试。")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.port = self._free_port()
        command = [
            chrome_path,
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            target_url,
        ]
        self.process = subprocess.Popen(command, cwd=str(Path(chrome_path).parent))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("托管 Chrome 启动后立即退出。")
            if self._endpoint_ready():
                self.log(f"托管 Chrome 已打开：{chrome_path}；请人工登录并导航到目标页面。")
                return
            time.sleep(0.2)
        self.close()
        raise RuntimeError("托管 Chrome 启动超时，请检查 Chrome 或专用 Profile 状态。")

    def connect_session(self, log: Callable[[str], None] | None = None) -> BrowserSession | None:
        if not self.is_running() or not self.endpoint:
            return None
        session = BrowserSession(self.profile_dir, log or self.log)
        session.connect_over_cdp(self.endpoint)
        return session

    def current_page_url(self) -> str:
        session = self.connect_session()
        if not session or not session.page:
            return ""
        try:
            return session.page.url
        finally:
            session.close()

    def close(self) -> None:
        process = self.process
        self.process = None
        self.port = None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
