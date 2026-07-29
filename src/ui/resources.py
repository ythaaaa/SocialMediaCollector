from __future__ import annotations

import sys
from pathlib import Path


def resource_path(relative_path: str) -> str:
    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS) / "src" / "ui" / "resources"
    else:
        base = Path(__file__).resolve().parent / "resources"
    return str(base / relative_path)


def stylesheet() -> str:
    return Path(resource_path("styles/app.qss")).read_text(encoding="utf-8")
