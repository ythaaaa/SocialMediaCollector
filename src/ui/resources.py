from __future__ import annotations

import sys
from pathlib import Path


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


def stylesheet() -> str:
    stylesheet_text = Path(resource_path("styles/app.qss")).read_text(encoding="utf-8")
    chevron = resource_path("icons/chevron-down.svg").replace("\\", "/")
    return stylesheet_text.replace("CHEVRON_DOWN_PATH", chevron)
