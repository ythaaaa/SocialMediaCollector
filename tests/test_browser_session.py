from pathlib import Path

from src.browser import BrowserSession, regular_chrome_login_command


def test_profile_lock_markers_are_detected(tmp_path: Path) -> None:
    assert not BrowserSession.profile_is_locked(tmp_path)
    (tmp_path / "lockfile").write_text("test", encoding="ascii")
    assert BrowserSession.profile_is_locked(tmp_path)


def test_browser_profile_is_stable_and_outside_repository() -> None:
    from src.utils.paths import browser_profile_directory

    profile = browser_profile_directory()
    assert profile.name == "browser-data"
    assert profile.parent.name == "PinterestLocalDownloader"


def test_regular_chrome_login_command_has_no_automation_flags(tmp_path: Path) -> None:
    command = regular_chrome_login_command("chrome.exe", tmp_path, "https://x.com/i/flow/login")
    assert command == ["chrome.exe", f"--user-data-dir={tmp_path}", "https://x.com/i/flow/login"]
    assert all("no-sandbox" not in argument for argument in command)
    assert all("remote-debugging" not in argument for argument in command)
    assert all("webdriver" not in argument for argument in command)
