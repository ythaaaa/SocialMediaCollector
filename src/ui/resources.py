from __future__ import annotations

import ctypes
import sys
from pathlib import Path


WINDOWS_APP_USER_MODEL_ID = "SocialMediaCollector.Desktop"


def resource_path(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS) / "src" / "ui" / "resources"
    else:
        base = Path(__file__).resolve().parent / "resources"
    return str(base / relative_path)


def app_icon_path() -> str:
    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS) / "assets"
    else:
        base = Path(__file__).resolve().parents[2] / "assets"
    return str(base / "app_icon.ico")


def set_windows_app_user_model_id() -> None:
    """Give Windows a stable identity for taskbar grouping and the app icon."""
    if sys.platform != "win32":
        return
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
        ctypes.c_wchar_p(WINDOWS_APP_USER_MODEL_ID)
    )


def stylesheet() -> str:
    stylesheet_text = Path(resource_path("styles/app.qss")).read_text(encoding="utf-8")
    chevron = resource_path("icons/chevron-down.svg").replace("\\", "/")
    return stylesheet_text.replace("CHEVRON_DOWN_PATH", chevron)
