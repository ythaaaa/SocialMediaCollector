from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtCore import QThread, Signal

from src.downloader.engine import DownloadEngine
from src.storage import QueueStore
from src.tasks import QueueTask


class QueueRunner(QThread):
    task_status = Signal(str, str, str)
    task_started = Signal(str)
    stats = Signal(object)
    log = Signal(str)
    finished_queue = Signal()

    def __init__(self, tasks: list[QueueTask], profile_dir: Path, store: QueueStore) -> None:
        super().__init__()
        self.tasks = tasks
        self.profile_dir = profile_dir
        self.store = store
        self.current_engine: DownloadEngine | None = None
        self.current_task: QueueTask | None = None
        self._stop_requested = Event()
        self._continue_event = Event()
        self._continue_event.set()

    def pause(self) -> None:
        self._continue_event.clear()
        if self.current_engine:
            self.current_engine.pause()
        if self.current_task and self.current_task.status == "running":
            self._set_status(self.current_task, "paused")

    def resume(self) -> None:
        self._continue_event.set()
        if self.current_engine:
            self.current_engine.resume()
        if self.current_task and self.current_task.status == "paused":
            self._set_status(self.current_task, "running")

    def stop(self) -> None:
        self._stop_requested.set()
        self._continue_event.set()
        if self.current_engine:
            self.current_engine.stop()

    def _set_status(self, task: QueueTask, status: str, error: str = "") -> None:
        task.status = status
        task.error = error
        self.store.save(self.tasks)
        self.task_status.emit(task.task_id, status, error)

    def _stop_pending(self, start_index: int) -> None:
        for task in self.tasks[start_index:]:
            if task.status == "pending":
                self._set_status(task, "stopped")

    def run(self) -> None:
        try:
            for index, task in enumerate(self.tasks):
                if task.status != "pending":
                    continue
                self._continue_event.wait()
                if self._stop_requested.is_set():
                    self._stop_pending(index)
                    break
                self._set_status(task, "running")
                self.current_task = task
                try:
                    self.current_engine = DownloadEngine(task.config, self.profile_dir, self.log.emit, self.stats.emit)
                    # Attach the callback after construction so lightweight
                    # fake engines used by tests and future integrations keep
                    # the original four-argument constructor contract.
                    self.current_engine.on_task_dir = lambda path: self.task_started.emit(str(path))
                    self.current_engine.run()
                    if self._stop_requested.is_set():
                        self._set_status(task, "stopped")
                        self._stop_pending(index + 1)
                        break
                    self._set_status(task, "completed")
                except Exception as exc:
                    if self._stop_requested.is_set():
                        self._set_status(task, "stopped", str(exc))
                        self._stop_pending(index + 1)
                        break
                    self._set_status(task, "failed", str(exc))
                    self.log.emit(f"队列任务失败，继续下一个任务：{exc}")
                finally:
                    self.current_engine = None
                    self.current_task = None
            self.store.save(self.tasks)
        finally:
            self.finished_queue.emit()
