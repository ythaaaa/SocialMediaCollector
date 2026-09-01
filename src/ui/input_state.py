from __future__ import annotations

from typing import Hashable


class ModeInputState:
    """Remember the single source input independently for each platform/mode."""

    def __init__(self) -> None:
        self._values: dict[Hashable, str] = {}
        self._active_key: Hashable | None = None

    def switch(self, key: Hashable, current_value: str) -> str:
        if self._active_key is not None:
            self._values[self._active_key] = current_value
        self._active_key = key
        return self._values.get(key, "")

    def remember(self, current_value: str) -> None:
        if self._active_key is not None:
            self._values[self._active_key] = current_value
