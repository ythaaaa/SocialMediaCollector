from __future__ import annotations

import re
import os
from datetime import datetime
from pathlib import Path


def safe_folder_name(keyword: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", keyword).strip(" ._")
    return (cleaned or "pinterest")[:80]


def create_task_directory(root: Path, keyword: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = root / f"{safe_folder_name(keyword)}_{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "images").mkdir()
    return folder


def application_data_directory() -> Path:
    """A writable, stable location for browser state outside source/build folders."""
    diagnostics_root = os.environ.get("SMC_DIAGNOSTICS_HOME")
    if diagnostics_root:
        directory = Path(diagnostics_root).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    directory = base / "PinterestLocalDownloader"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def diagnostics_directory() -> Path:
    """Return the one directory used for diagnostic logs and runtime data."""
    configured = os.environ.get("SMC_DIAGNOSTICS_HOME")
    if configured:
        directory = Path(configured).expanduser().resolve()
    else:
        directory = application_data_directory() / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def browser_profile_directory() -> Path:
    """Return the stable app-owned profile shared by Pinterest and X."""
    directory = application_data_directory() / "browser-data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def instagram_browser_profile_directory() -> Path:
    """Return Instagram's separate persistent profile without copying data."""
    return application_data_directory() / "browser-data-instagram"


def x_browser_profile_directory() -> Path:
    """Return X's separate persistent profile without copying user data."""
    return application_data_directory() / "browser-data-x"


def profile_directory_for_platform(platform: str) -> Path:
    """Route each platform to its stable app-owned browser profile."""
    platform = (platform or "").strip().lower()
    if platform == "instagram":
        return instagram_browser_profile_directory()
    if platform == "x":
        return x_browser_profile_directory()
    return browser_profile_directory()
