from pathlib import Path

import pytest

from src.browser import BrowserSession, BrowserSessionManager, managed_chrome_command, regular_chrome_login_command
from src.browser import session as session_module


def test_profile_lock_markers_are_detected(tmp_path: Path) -> None:
    assert not BrowserSession.profile_is_locked(tmp_path)
    (tmp_path / "lockfile").write_text("test", encoding="ascii")
    assert BrowserSession.profile_is_locked(tmp_path)


def test_browser_profile_is_stable_and_outside_repository() -> None:
    from src.utils.paths import browser_profile_directory

    profile = browser_profile_directory()
    assert profile.name == "browser-data"
    assert profile.parent.name == "PinterestLocalDownloader"


def test_platform_profile_routing_keeps_pinterest_x_and_instagram_separate(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from src.utils.paths import profile_directory_for_platform

    assert profile_directory_for_platform("pinterest") == tmp_path / "PinterestLocalDownloader" / "browser-data"
    assert profile_directory_for_platform("x") == tmp_path / "PinterestLocalDownloader" / "browser-data-x"
    instagram = profile_directory_for_platform("instagram")
    assert instagram == tmp_path / "PinterestLocalDownloader" / "browser-data-instagram"
    assert not instagram.exists()


def test_regular_chrome_login_command_has_no_automation_flags(tmp_path: Path) -> None:
    command = regular_chrome_login_command("chrome.exe", tmp_path, "https://x.com/i/flow/login")
    assert command == ["chrome.exe", f"--user-data-dir={tmp_path}", "https://x.com/i/flow/login"]
    assert all("no-sandbox" not in argument for argument in command)
    assert all("remote-debugging" not in argument for argument in command)
    assert all("webdriver" not in argument for argument in command)


def test_managed_chrome_command_keeps_manual_login_separate_from_playwright(tmp_path: Path) -> None:
    command = managed_chrome_command("chrome.exe", tmp_path, 9222, "https://www.instagram.com/")
    assert command == [
        "chrome.exe",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={tmp_path}",
        "https://www.instagram.com/",
    ]
    assert all("--enable-automation" not in argument for argument in command)
    assert all("webdriver" not in argument for argument in command)


def test_manual_login_manager_uses_plain_chrome_without_cdp(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(session_module, "find_chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(session_module.subprocess, "Popen", lambda command, cwd: commands.append(command) or Process())
    clock = iter((0.0, 4.0))
    monkeypatch.setattr(session_module.time, "monotonic", lambda: next(clock))
    manager = BrowserSessionManager(tmp_path, lambda _: None)
    manager.open_manual_login_browser("https://www.instagram.com/")

    assert commands == [["chrome.exe", f"--user-data-dir={tmp_path}", "https://www.instagram.com/"]]
    assert manager.port is None
    assert not any("remote-debugging" in argument for argument in commands[0])


def test_collection_manager_adds_cdp_only_after_login_chrome_exits(monkeypatch, tmp_path: Path) -> None:
    commands: list[list[str]] = []

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(session_module, "find_chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(session_module.subprocess, "Popen", lambda command, cwd: commands.append(command) or Process())
    monkeypatch.setattr(session_module.BrowserSessionManager, "_endpoint_ready", lambda _self: True)
    monkeypatch.setattr(session_module.BrowserSessionManager, "_free_port", staticmethod(lambda: 9222))
    manager = BrowserSessionManager(tmp_path, lambda _: None)
    manager.open_collection_browser("https://www.instagram.com/")

    assert commands == [[
        "chrome.exe",
        "--remote-debugging-port=9222",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={tmp_path}",
        "https://www.instagram.com/",
    ]]


def test_collection_manager_rejects_running_login_chrome_without_killing_it(tmp_path: Path) -> None:
    class Process:
        def poll(self):
            return None

        def terminate(self):
            raise AssertionError("the login Chrome must be closed by the user")

    manager = BrowserSessionManager(tmp_path, lambda _: None)
    manager.login_process = Process()
    with pytest.raises(RuntimeError, match="仍在运行"):
        manager.open_collection_browser("https://www.instagram.com/")


def test_cdp_readiness_uses_direct_loopback_http(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, int, float]] = []

    class Response:
        status = 200
        reason = "OK"

        def read(self) -> bytes:
            return b"{}"

    class Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            calls.append((host, port, timeout))

        def request(self, method: str, path: str) -> None:
            assert (method, path) == ("GET", "/json/version")

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            return None

    monkeypatch.setattr(session_module, "HTTPConnection", Connection)
    manager = BrowserSessionManager(tmp_path, lambda _: None)
    manager.port = 9222

    assert manager._endpoint_ready() is True
    assert calls == [("127.0.0.1", 9222, 0.5)]


def test_instagram_collection_logs_startup_diagnostics(monkeypatch, tmp_path: Path) -> None:
    logs: list[str] = []

    class Process:
        pid = 1234

        def poll(self):
            return None

    monkeypatch.setattr(session_module, "find_chrome_path", lambda: "chrome.exe")
    monkeypatch.setattr(session_module.subprocess, "Popen", lambda command, cwd: Process())
    monkeypatch.setattr(session_module.BrowserSessionManager, "_endpoint_ready", lambda _self: True)
    monkeypatch.setattr(session_module.BrowserSessionManager, "_free_port", staticmethod(lambda: 9222))
    manager = BrowserSessionManager(tmp_path / "browser-data-instagram", logs.append)

    manager.open_collection_browser("https://www.instagram.com/")

    assert any("Instagram Chrome executable=chrome.exe" in message for message in logs)
    assert any("Instagram profile=" in message for message in logs)
    assert "Instagram CDP port=9222" in logs
    assert "Instagram Chrome pid=1234" in logs
    assert "Instagram CDP waiting..." in logs
    assert "Instagram CDP ready" in logs


def test_instagram_close_kills_owned_chrome_process_tree(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    class Process:
        pid = 4321

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_run(command, **_kwargs):
        calls.append(command)

    monkeypatch.setattr(session_module.subprocess, "run", fake_run)
    manager = BrowserSessionManager(tmp_path / "browser-data-instagram", lambda _: None)
    manager.collection_process = Process()

    manager.close()

    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"]]
