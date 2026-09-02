from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from src.browser.session import BrowserSession, find_chrome_path
from src.utils.paths import instagram_browser_profile_directory
from src.utils.runtime import subprocess_creation_flags


COOKIE_NAMES = ("sessionid", "csrftoken", "ds_user_id")
COOKIE_STORE_PATH = "auth/instagram.cookies"
DEFAULT_BRIDGE_WORKER = Path(__file__).with_name("instagram_cookie_bridge_worker.py")


class InstagramAuthRequired(RuntimeError):
    """The browser profile has no usable Instagram session."""


def cookie_store_path() -> Path:
    from src.utils.paths import application_data_directory

    return application_data_directory() / COOKIE_STORE_PATH


def _dpapi(data: bytes, *, unprotect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Instagram cookie store requires Windows DPAPI")
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    source = ctypes.create_string_buffer(data)
    input_blob = Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_byte)))
    output_blob = Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    function = crypt32.CryptUnprotectData if unprotect else crypt32.CryptProtectData
    if not function(
        ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI cookie operation failed")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def save_instagram_cookies(cookies: dict[str, str], path: Path | None = None) -> Path:
    selected = {name: str(cookies[name]) for name in COOKIE_NAMES if cookies.get(name)}
    if not selected.get("sessionid"):
        raise InstagramAuthRequired("Instagram cookie loaded=no; sessionid 未找到")
    target = Path(path or cookie_store_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    protected = _dpapi(json.dumps(selected, separators=(",", ":")).encode("utf-8"), unprotect=False)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(protected)
    os.replace(temporary, target)
    return target


def load_instagram_cookies(path: Path | None = None) -> dict[str, str]:
    target = Path(path or cookie_store_path())
    try:
        data = json.loads(_dpapi(target.read_bytes(), unprotect=True).decode("utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError) as exc:
        raise InstagramAuthRequired("Instagram cookie bridge data unavailable") from exc
    if not isinstance(data, dict) or not data.get("sessionid"):
        raise InstagramAuthRequired("Instagram cookie loaded=no; sessionid 未找到")
    return {str(key): str(value) for key, value in data.items() if key in COOKIE_NAMES and value}


def _default_python() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path.home() / "Desktop" / "pinterest" / ".venv" / "Scripts" / "python.exe"


class InstagramCookieBridge:
    """Run one isolated Chrome/CDP cookie sync, then persist only DPAPI data."""

    def __init__(
        self,
        profile_path: Path | None = None,
        chrome_path: str | None = None,
        *,
        python_executable: Path | None = None,
        worker_script: Path | None = None,
    ) -> None:
        self.profile_path = Path(profile_path or instagram_browser_profile_directory())
        self.chrome_path = chrome_path or find_chrome_path()
        self.python_executable = Path(python_executable or _default_python())
        self.worker_script = Path(worker_script or DEFAULT_BRIDGE_WORKER)

    def sync(self) -> Path:
        if not self.chrome_path:
            raise RuntimeError("未找到本机 Google Chrome，无法同步 Instagram Cookie。")
        if BrowserSession.profile_is_locked(self.profile_path):
            raise RuntimeError("Instagram 登录 Profile 仍被 Chrome 占用，请先关闭 Login Chrome。")
        if not self.profile_path.is_dir():
            raise InstagramAuthRequired("Instagram browser profile does not exist")
        request = {
            "profile_path": str(self.profile_path),
            "chrome_path": self.chrome_path,
        }
        try:
            process = subprocess.Popen(
                [str(self.python_executable), str(self.worker_script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess_creation_flags(),
            )
            try:
                stdout, _stderr = process.communicate(json.dumps(request) + "\n", timeout=35)
            except subprocess.TimeoutExpired:
                self._terminate(process)
                raise RuntimeError("Instagram Cookie sync 超时。")
        except OSError as exc:
            raise RuntimeError(f"Instagram Cookie sync helper 启动失败：{exc}") from exc
        try:
            result = json.loads((stdout or "").splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("Instagram Cookie sync helper 返回无效结果。") from exc
        if not result.get("ok"):
            if result.get("code") == "AUTH_REQUIRED":
                raise InstagramAuthRequired(str(result.get("message") or "Instagram 登录已失效"))
            raise RuntimeError(str(result.get("message") or "Instagram Cookie sync 失败。"))
        cookies = result.get("cookies")
        if not isinstance(cookies, dict):
            raise InstagramAuthRequired("Instagram cookie loaded=no")
        return save_instagram_cookies(cookies)

    @staticmethod
    def _terminate(process: Any) -> None:
        if process.poll() is None:
            if os.name == "nt" and getattr(process, "pid", None):
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    creationflags=subprocess_creation_flags(),
                )
            else:
                process.terminate()
        try:
            process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            process.kill()
            process.wait(timeout=2)


def main() -> int:
    try:
        path = InstagramCookieBridge().sync()
    except InstagramAuthRequired as exc:
        print(f"Instagram AUTH_REQUIRED: {exc}")
        return 2
    except Exception as exc:
        print(f"Instagram Cookie sync failed: {exc}")
        return 1
    print(f"Instagram Cookie sync complete: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
