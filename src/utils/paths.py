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
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    directory = base / "PinterestLocalDownloader"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def browser_profile_directory() -> Path:
    """Return the stable app-owned profile shared by Pinterest and X."""
    directory = application_data_directory() / "browser-data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
