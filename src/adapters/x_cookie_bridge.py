"""X-only browser cookie sync and DPAPI-backed cookie store."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from src.adapters.instagram_cookie_bridge import _dpapi
from src.browser.session import BrowserSession, find_chrome_path
from src.utils.paths import profile_directory_for_platform


X_COOKIE_STORE_PATH = "auth/x.cookies"
X_REQUIRED_COOKIE_NAMES = frozenset({"auth_token", "ct0"})


class XAuthRequired(RuntimeError):
    """X collection has no usable authenticated cookie context."""


def x_cookie_store_path() -> Path:
    from src.utils.paths import application_data_directory

    return application_data_directory() / X_COOKIE_STORE_PATH


def _is_x_domain(domain: Any) -> bool:
    normalized = str(domain or "").strip().lstrip(".").casefold().rstrip(".")
    return normalized in {"x.com", "twitter.com"} or normalized.endswith((".x.com", ".twitter.com"))


def filter_x_cookies(raw_cookies: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Keep every non-empty X/Twitter cookie without exposing its value."""
    selected: dict[str, str] = {}
    for item in raw_cookies:
        if not _is_x_domain(item.get("domain")):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "")
        if name and value:
            selected[name] = value
    return selected


def x_cookie_presence(cookies: Mapping[str, Any]) -> tuple[bool, bool]:
    return bool(cookies.get("auth_token")), bool(cookies.get("ct0"))


def _require_x_auth(cookies: Mapping[str, Any]) -> None:
    auth_token, ct0 = x_cookie_presence(cookies)
    if not auth_token or not ct0:
        raise XAuthRequired("X AUTH_REQUIRED; 请先打开浏览器 / 登录并同步 Cookie。")


def save_x_cookies(cookies: Mapping[str, Any], path: Path | None = None) -> Path:
    selected = {str(name): str(value) for name, value in cookies.items() if name and value}
    _require_x_auth(selected)
    target = Path(path or x_cookie_store_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    protected = _dpapi(json.dumps(selected, separators=(",", ":"), ensure_ascii=False).encode("utf-8"), unprotect=False)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(protected)
    os.replace(temporary, target)
    return target


def load_x_cookies(path: Path | None = None) -> dict[str, str]:
    target = Path(path or x_cookie_store_path())
    try:
        data = json.loads(_dpapi(target.read_bytes(), unprotect=True).decode("utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError, UnicodeError) as exc:
        raise XAuthRequired("X AUTH_REQUIRED; Cookie store unavailable。") from exc
    if not isinstance(data, dict):
        raise XAuthRequired("X AUTH_REQUIRED; Cookie store invalid。")
    selected = {str(name): str(value) for name, value in data.items() if name and value}
    _require_x_auth(selected)
    return selected


class XCookieBridge:
    """Read the app-owned X profile and persist only DPAPI-protected cookies."""

    def __init__(
        self,
        profile_path: Path | None = None,
        chrome_path: str | None = None,
        *,
        cookies_reader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        self.profile_path = Path(profile_path or profile_directory_for_platform("x"))
        self.chrome_path = chrome_path or find_chrome_path()
        self.cookies_reader = cookies_reader
        self.cookie_count = 0
        self.auth_token_present = False
        self.ct0_present = False

    def _read_profile_cookies(self) -> list[dict[str, Any]]:
        if not self.profile_path.is_dir():
            raise XAuthRequired("X browser profile does not exist")
        if not self.chrome_path:
            raise RuntimeError("未找到本机 Google Chrome，无法同步 X Cookie。")
        try:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            try:
                context = playwright.chromium.launch_persistent_context(
                    str(self.profile_path), executable_path=self.chrome_path, headless=True
                )
                try:
                    return context.cookies(["https://x.com/", "https://twitter.com/"])
                finally:
                    context.close()
            finally:
                playwright.stop()
        except XAuthRequired:
            raise
        except Exception as exc:
            raise RuntimeError(f"X Cookie sync failed: {type(exc).__name__}") from exc

    def sync(self, path: Path | None = None) -> Path:
        if self.cookies_reader is not None:
            raw_cookies = list(self.cookies_reader())
        else:
            if BrowserSession.profile_is_locked(self.profile_path):
                raise RuntimeError("X 登录 Profile 仍被 Chrome 占用，请先关闭 Login Chrome。")
            raw_cookies = self._read_profile_cookies()
        selected = filter_x_cookies(raw_cookies)
        target = save_x_cookies(selected, path)
        self.cookie_count = len(selected)
        self.auth_token_present, self.ct0_present = x_cookie_presence(selected)
        return target
