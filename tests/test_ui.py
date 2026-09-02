from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication, QFrame, QLabel

from src.models import TaskConfig, TaskStats
from src.ui import main_window as main_window_module
from src.ui.main_window import MainWindow
from src.ui.resources import WINDOWS_APP_USER_MODEL_ID, app_icon_path
from src.downloader.engine import DownloadEngine
from src.storage import CollectorDatabase
from src.tasks import QueueTask


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MainWindow:
    from src.storage import QueueStore

    monkeypatch.setattr(main_window_module, "browser_profile_directory", lambda: tmp_path / "browser-data")
    monkeypatch.setattr(main_window_module, "QueueStore", lambda: QueueStore(tmp_path / "queue.json"))
    monkeypatch.setattr(
        main_window_module,
        "CollectorDatabase",
        lambda: CollectorDatabase(tmp_path / "collector.db"),
    )
    result = MainWindow()
    yield result
    result.deleteLater()


def _source_grid(window: MainWindow):
    for card in window.findChildren(QFrame):
        if card.layout() is not None and any(label.text() == "采集来源" for label in card.findChildren(QLabel)):
            for index in range(card.layout().count()):
                child_layout = card.layout().itemAt(index).layout()
                if child_layout is not None and child_layout.count() == 3:
                    return child_layout
    raise AssertionError("采集来源卡片未找到")


def _new_task_card_titles(window: MainWindow) -> list[str]:
    page = window.tabs.widget(0)
    assert page is not None
    content = page.widget()
    assert content is not None and content.layout() is not None
    titles: list[str] = []
    for index in range(content.layout().count()):
        widget = content.layout().itemAt(index).widget()
        if widget is None:
            continue
        title = next((label.text() for label in widget.findChildren(QLabel) if label.objectName() == "cardTitle"), "")
        if title:
            titles.append(title)
    return titles


def test_new_task_cards_follow_the_requested_vertical_order(window: MainWindow) -> None:
    assert _new_task_card_titles(window) == [
        "采集来源", "下载设置", "高级筛选", "目标内容", "操作", "下载进度", "实时记录",
    ]
    assert window.advanced_panel.isHidden()

    window.advanced_toggle.setChecked(True)
    window._toggle_advanced(True)
    assert not window.advanced_panel.isHidden()
    window.advanced_toggle.setChecked(False)
    window._toggle_advanced(False)
    assert window.advanced_panel.isHidden()


def test_source_card_always_has_three_fields_and_search_scope_moves_to_target(window: MainWindow) -> None:
    assert _source_grid(window).count() == 3

    window.platform.setCurrentIndex(1)  # X
    window.mode.setCurrentIndex(0)  # author
    assert window.search_scope_field.isHidden()
    assert _source_grid(window).count() == 3

    window.mode.setCurrentIndex(1)  # search
    assert not window.search_scope_field.isHidden()
    assert window.search_scope_field.parentWidget() is not None
    assert _source_grid(window).count() == 3

    window.mode.setCurrentIndex(0)
    assert window.search_scope_field.isHidden()
    assert _source_grid(window).count() == 3


def test_x_search_scope_still_writes_task_config(window: MainWindow) -> None:
    window.platform.setCurrentIndex(1)
    window.mode.setCurrentIndex(1)
    window.keyword.setText("cats")
    window.search_scope.setCurrentIndex(window.search_scope.findData("top"))

    config = window._task_config_from_form()

    assert config.search_scope == "top"
    assert config.canonical_search_scope() == "top"


def test_platform_combo_uses_registry_icons_and_disables_reserved_platforms(window: MainWindow) -> None:
    assert window.platform.count() == 4
    assert [window.platform.itemData(i) for i in range(window.platform.count())] == [
        "pinterest", "x", "instagram", "youtube",
    ]
    assert all(not window.platform.itemIcon(i).isNull() for i in range(window.platform.count()))

    for index in (2, 3):
        item = window.platform.model().item(index)
        assert item is not None
        assert not (item.flags() & Qt.ItemFlag.ItemIsEnabled)

    window.platform.setCurrentIndex(2)
    assert window.platform.currentData() == "instagram"
    assert [window.mode.itemData(i) for i in range(window.mode.count())] == ["author", "single_post"]
    assert [window.media_type.itemData(i) for i in range(window.media_type.count())] == ["image", "video", "mixed"]
    assert not window.start.isEnabled()
    assert "开发中" in window.mode_hint.text()


def test_reserved_platform_cannot_start_task(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        staticmethod(lambda _parent, _title, message: messages.append(message)),
    )

    config = TaskConfig(
        "",
        1,
        Path(window.database.path).parent,
        platform="instagram",
        collection_mode="author",
        source_identifier="future-account",
    )
    window._start_config(config)

    assert messages == ["该平台正在开发中，当前版本暂不支持实际采集。"]
    assert window.worker is None


def test_reserved_platform_cannot_start_queue(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        "warning",
        staticmethod(lambda _parent, _title, message: messages.append(message)),
    )
    task = QueueTask(
        TaskConfig(
            "",
            1,
            Path(window.database.path).parent,
            platform="youtube",
            collection_mode="search",
            source_identifier="future videos",
        )
    )
    window.queue_tasks[:] = [task]

    window.start_queue_tasks()

    assert messages == ["该平台正在开发中，当前版本暂不支持实际采集。"]
    assert window.queue_runner is None


def test_control_handlers_call_engine_and_follow_confirmed_states(window: MainWindow) -> None:
    calls: list[str] = []

    class FakeEngine:
        def pause(self) -> None:
            calls.append("pause")
            window._control_status_changed("paused")

        def resume(self) -> None:
            calls.append("resume")
            window._control_status_changed("running")

        def stop(self) -> None:
            calls.append("stop")

    worker = SimpleNamespace(engine=FakeEngine(), isRunning=lambda: True)
    window.worker = worker
    window._set_running(True)

    window.pause_task()
    assert calls == ["pause"]
    assert window.status.text() == "已暂停"
    assert not window.pause.isEnabled()
    assert window.resume.isEnabled()
    assert window.stop.isEnabled()

    window.resume_task()
    assert calls == ["pause", "resume"]
    assert window.status.text() == "运行中"
    assert window.pause.isEnabled()
    assert not window.resume.isEnabled()

    window.stop_task()
    assert calls == ["pause", "resume", "stop"]
    assert window.status.text() == "正在停止..."
    assert not window.pause.isEnabled()
    assert not window.resume.isEnabled()
    assert not window.stop.isEnabled()


def test_engine_control_methods_emit_confirmations(tmp_path: Path) -> None:
    engine = DownloadEngine(
        TaskConfig("keyword", 1, tmp_path / "output", interval_seconds=0),
        tmp_path / "profile",
        database=CollectorDatabase(tmp_path / "collector.db"),
    )
    events: list[str] = []
    engine.on_control_status = events.append

    engine.pause()
    engine.resume()
    engine.stop()

    assert events == ["paused", "running"]
    assert engine.stop_event.is_set()
    assert engine.pause_event.is_set()


def test_brand_icon_has_windows_sizes_and_stable_app_id() -> None:
    icon = Image.open(app_icon_path())

    assert sorted(icon.ico.sizes()) == [(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    assert WINDOWS_APP_USER_MODEL_ID == "SocialMediaCollector.Desktop"


def test_button_styles_include_pressed_and_disabled_feedback() -> None:
    qss = Path("src/ui/resources/styles/app.qss").read_text(encoding="utf-8")

    assert "QPushButton:pressed" in qss
    assert "QPushButton:disabled" in qss


def test_empty_history_shows_empty_state_and_disables_history_actions(window: MainWindow) -> None:
    assert window.history_table.isHidden()
    assert not window.history_empty.isHidden()
    assert not window.delete_history.isEnabled()
    assert not window.clear_history.isEnabled()
    assert not window.open_history_dir.isEnabled()
    assert not window.recollect_history.isEnabled()
    assert not window.retry_history.isEnabled()


def test_history_delete_supports_multiple_selected_rows(window: MainWindow) -> None:
    configs = []
    for name in ("one", "two"):
        config = TaskConfig(name, 1, Path(window.database.path).parent)
        output = Path(window.database.path).parent / f"{name}-task"
        output.mkdir()
        window.database.start_task(config, output)
        window.database.finish_task(config, TaskStats(downloaded=1), "completed", task_output_dir=output)
        configs.append(config)
    window._refresh_history()

    window.history_table.selectRow(0)
    index = window.history_table.model().index(1, 0)
    window.history_table.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    assert len(window._selected_history_ids()) == 2

    window.delete_selected_history()

    assert all(window.database.get_history(config.task_id) is None for config in configs)
    assert window.history_table.isHidden()


def test_running_history_delete_shows_protection_message(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    config = TaskConfig("running", 1, Path(window.database.path).parent)
    window.database.start_task(config, Path(window.database.path).parent / "running-task")
    window._refresh_history()
    window.history_table.selectRow(0)
    messages: list[str] = []
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", staticmethod(lambda _parent, _title, message: messages.append(message)))

    window.delete_selected_history()

    assert messages == ["运行中的任务不能删除历史记录，请先停止任务。"]
    assert window.database.get_history(config.task_id) is not None


def test_clear_history_uses_confirmation_dialog(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    config = TaskConfig("clear-me", 1, Path(window.database.path).parent)
    output = Path(window.database.path).parent / "clear-me-task"
    output.mkdir()
    window.database.start_task(config, output)
    window.database.finish_task(config, TaskStats(downloaded=1), "completed", task_output_dir=output)
    window._refresh_history()
    captured: dict[str, object] = {}

    class FakeMessageBox:
        class ButtonRole:
            RejectRole = "reject"
            AcceptRole = "accept"

        def __init__(self, _parent) -> None:
            self.confirm = None

        def setWindowTitle(self, title: str) -> None:
            captured["title"] = title

        def setText(self, text: str) -> None:
            captured["text"] = text

        def addButton(self, label: str, _role: str):
            button = object()
            captured.setdefault("buttons", []).append(label)
            if label == "确定清空":
                self.confirm = button
            return button

        def exec(self) -> None:
            return None

        def clickedButton(self):
            return self.confirm

    monkeypatch.setattr(main_window_module, "QMessageBox", FakeMessageBox)

    window.clear_all_history()

    assert captured["title"] == "清空全部历史"
    assert captured["buttons"] == ["取消", "确定清空"]
    assert "不会删除已下载的图片、视频或任务文件夹" in str(captured["text"])
    assert window.database.list_history() == []
    assert output.exists()


def test_failed_queue_task_can_be_deleted_from_ui_and_persists_to_queue_store(window: MainWindow) -> None:
    config = TaskConfig("failed", 20, Path(window.database.path).parent, platform="x", collection_mode="search", source_identifier="高跟鞋")
    task = QueueTask(config, status="failed")
    window.queue_tasks[:] = [task]
    window.queue_store.save(window.queue_tasks)
    window._refresh_queue_table()
    window.queue_table.selectRow(0)

    window.delete_selected_tasks()

    assert window.queue_tasks == []
    assert window.queue_table.rowCount() == 0
    assert window.queue_store.load() == []


def test_queue_ui_rejects_mixed_selection_with_running_task(window: MainWindow, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = [QueueTask(TaskConfig("failed", 1, Path(window.database.path).parent), status="failed"), QueueTask(TaskConfig("running", 1, Path(window.database.path).parent), status="running")]
    window.queue_tasks[:] = tasks
    window.queue_store.save(tasks)
    window._refresh_queue_table()
    window.queue_table.selectRow(0)
    index = window.queue_table.model().index(1, 0)
    window.queue_table.selectionModel().select(index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    messages: list[str] = []
    monkeypatch.setattr(main_window_module.QMessageBox, "warning", staticmethod(lambda _parent, _title, message: messages.append(message)))

    window.delete_selected_tasks()

    assert messages == ["运行中或暂停中的任务请先停止，再从队列中删除。"]
    assert [task.status for task in window.queue_tasks] == ["failed", "running"]
