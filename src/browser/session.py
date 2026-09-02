from __future__ import annotations

import shutil
import os
import socket
import subprocess
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Callable

from playwright.sync_api import BrowserContext, Page, sync_playwright
from src.utils.runtime import subprocess_creation_flags


def find_chrome_path() -> str | None:
    """Find the installed system Chrome, never a Playwright runtime binary."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        str(local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe") if local_app_data else None,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        shutil.which("chrome.exe"),
        shutil.which("chrome"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), None)


def regular_chrome_login_command(chrome_path: str, profile_dir: Path, target_url: str) -> list[str]:
    """Build the deliberately minimal command used for human login."""
    return [chrome_path, f"--user-data-dir={Path(profile_dir)}", target_url]


def managed_chrome_command(chrome_path: str, profile_dir: Path, port: int, target_url: str) -> list[str]:
    """Build the normal Chrome command that can later be attached to over CDP.

    The browser is still launched directly by the operating system.  No
    Playwright automation flags, login actions, or credential handling are
    added to this command.
    """
    command = regular_chrome_login_command(chrome_path, profile_dir, target_url)
    command[1:1] = [
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
    ]
    return command


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
    """Own the app-launched Chrome processes and one reusable profile.

    Instagram has an intentionally separate lifecycle: its login process is
    ordinary headed Chrome with no debugging endpoint, while its collection
    process is started later with CDP enabled.
    """

    def __init__(self, profile_dir: Path, log: Callable[[str], None]) -> None:
        self.profile_dir = Path(profile_dir)
        self.log = log
        self.login_process: subprocess.Popen[bytes] | None = None
        self.collection_process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None
        self.chrome_path: str | None = None
        self._cdp_last_error = ""

    def _is_instagram_profile(self) -> bool:
        return self.profile_dir.name.casefold() == "browser-data-instagram"

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @property
    def endpoint(self) -> str | None:
        return f"http://127.0.0.1:{self.port}" if self.port else None

    def _endpoint_ready(self) -> bool:
        if not self.port:
            return False
        connection: HTTPConnection | None = None
        try:
            # Use a direct loopback connection. urllib's proxy auto-detection
            # can route 127.0.0.1 through HTTP_PROXY and turn a healthy CDP
            # endpoint into a false 502 readiness failure.
            connection = HTTPConnection("127.0.0.1", self.port, timeout=0.5)
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            response.read()
            if 200 <= response.status < 300:
                self._cdp_last_error = ""
                return True
            self._cdp_last_error = f"HTTP {response.status} {response.reason}".strip()
        except Exception as exc:
            self._cdp_last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if connection is not None:
                connection.close()
        return False

    def _log_instagram(self, message: str) -> None:
        if self._is_instagram_profile():
            self.log(f"Instagram {message}")

    def _terminate_owned_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if self._is_instagram_profile() and os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess_creation_flags(),
                check=False,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def is_running(self) -> bool:
        """Return whether the CDP-enabled collection Chrome is running."""
        if self.collection_process and self.collection_process.poll() is not None:
            self.collection_process = None
            self.port = None
            return False
        return bool(self.collection_process and self._endpoint_ready())

    def is_login_running(self) -> bool:
        if self.login_process and self.login_process.poll() is not None:
            self.login_process = None
            return False
        return bool(self.login_process)

    def set_profile_dir(self, profile_dir: Path) -> None:
        """Select a profile only while this manager owns no live Chrome."""
        target = Path(profile_dir)
        if target == self.profile_dir:
            return
        if self.is_login_running() or self.is_running():
            raise RuntimeError("当前浏览器仍在运行，不能切换浏览器 Profile。")
        self.profile_dir = target

    def open_manual_login_browser(self, target_url: str) -> None:
        """Open the app-owned profile in ordinary system Chrome for human login.

        This method deliberately has no CDP flags and does not create a
        Playwright object.  The user owns the entire login interaction.
        """
        if self.is_login_running():
            self.log("Instagram 登录 Chrome 已在运行，继续使用当前 Profile。")
            return
        if self.is_running():
            raise RuntimeError("采集浏览器正在运行，请先结束当前任务后再打开 Instagram 登录浏览器。")
        chrome_path = find_chrome_path()
        if not chrome_path:
            raise RuntimeError("未找到本机 Google Chrome，无法打开托管浏览器。")
        if BrowserSession.profile_is_locked(self.profile_dir):
            raise RuntimeError("请先关闭当前占用专用 Profile 的浏览器，再重试。")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.chrome_path = chrome_path
        command = regular_chrome_login_command(chrome_path, self.profile_dir, target_url)
        self.log(f"人工登录 Chrome 启动参数（无 CDP）：{subprocess.list2cmdline(command)}")
        self.login_process = subprocess.Popen(command, cwd=str(Path(chrome_path).parent))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.login_process.poll() is not None:
                self.login_process = None
                raise RuntimeError("人工登录 Chrome 启动后立即退出。")
            time.sleep(0.2)
        self.log(f"人工登录 Chrome 已打开：{chrome_path}；请人工完成 Instagram 登录后关闭该 Chrome。")

    def open_manual_browser(self, target_url: str) -> None:
        """Compatibility alias for callers that already request manual Chrome."""
        self.open_manual_login_browser(target_url)

    def open_collection_browser(self, target_url: str) -> None:
        """Start the same profile in CDP-enabled Chrome for collection only."""
        if self.is_running():
            return
        if self.is_login_running():
            raise RuntimeError("Instagram 登录浏览器仍在运行。请完成登录并关闭该浏览器后，再开始采集。")
        chrome_path = find_chrome_path()
        if not chrome_path:
            raise RuntimeError("未找到本机 Google Chrome，无法启动采集浏览器。")
        if BrowserSession.profile_is_locked(self.profile_dir):
            raise RuntimeError("请先关闭当前占用专用 Profile 的浏览器，再开始采集。")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.port = self._free_port()
        self.chrome_path = chrome_path
        command = managed_chrome_command(chrome_path, self.profile_dir, self.port, target_url)
        self._log_instagram(f"Chrome executable={chrome_path}")
        self._log_instagram(f"profile={self.profile_dir}")
        self._log_instagram(f"CDP port={self.port}")
        self.log(f"采集 Chrome 启动参数（仅采集阶段启用 CDP）：{subprocess.list2cmdline(command)}")
        try:
            self.collection_process = subprocess.Popen(command, cwd=str(Path(chrome_path).parent))
        except OSError as exc:
            self._log_instagram(f"Chrome process exited=True error={type(exc).__name__}: {exc}")
            self.collection_process = None
            self.port = None
            raise RuntimeError(f"Instagram Chrome 启动失败：{exc}") from exc
        self._log_instagram(f"Chrome pid={getattr(self.collection_process, 'pid', None)}")
        self._log_instagram("CDP waiting...")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if self.collection_process.poll() is not None:
                exit_code = self.collection_process.poll()
                self._log_instagram(f"Chrome process exited=True code={exit_code}")
                self.collection_process = None
                self.port = None
                raise RuntimeError("采集 Chrome 启动后立即退出。")
            if self._endpoint_ready():
                self._log_instagram("CDP ready")
                self.log(f"采集 Chrome 已打开：{chrome_path}；现在才连接 CDP/Playwright。")
                return
            time.sleep(0.2)
        self._log_instagram(f"Chrome process exited={self.collection_process.poll() is not None}")
        self._log_instagram(f"CDP timeout reason={self._cdp_last_error or '127.0.0.1 CDP endpoint 未返回 ready'}")
        self.close()
        raise RuntimeError("采集 Chrome 启动超时，请检查 Chrome 或专用 Profile 状态。")

    def open_browser(self, target_url: str) -> None:
        """Backward-compatible CDP browser entry point for Pinterest/X."""
        self.open_collection_browser(target_url)

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
        login_process = self.login_process
        collection_process = self.collection_process
        self.login_process = None
        self.collection_process = None
        self.port = None
        self.chrome_path = None
        for process in (login_process, collection_process):
            if process and process.poll() is None:
                self._terminate_owned_process(process)
