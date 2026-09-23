from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PIL import Image
from PySide6.QtCore import QItemSelectionModel, QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication, QFrame, QLabel, QTextEdit

from src.models import TaskConfig, TaskStats
from src.ui import main_window as main_window_module
from src.ui.main_window import MainWindow, NoWheelComboBox
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


def _new_task_card(window: MainWindow, title: str) -> QFrame:
    page = window.tabs.widget(0)
    assert page is not None
    content = page.widget()
    assert content is not None and content.layout() is not None
    for index in range(content.layout().count()):
        widget = content.layout().itemAt(index).widget()
        if isinstance(widget, QFrame) and any(
            label.text() == title and label.objectName() == "cardTitle"
            for label in widget.findChildren(QLabel)
        ):
            return widget
    raise AssertionError(f"{title} 卡片未找到")


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


def test_history_dedup_checkbox_is_in_advanced_settings_and_defaults_unchecked(window: MainWindow) -> None:
    assert window.skip_history_duplicates.text() == "跳过历史已下载素材"
    assert window.skip_history_duplicates.isChecked() is False
    assert window.skip_history_duplicates.parentWidget() is window.advanced_panel

    window.skip_history_duplicates.setChecked(True)
    assert window._task_config_from_form().skip_history_duplicates is True


def test_update_stats_displays_discovered_independently_of_download_result(window: MainWindow) -> None:
    window.update_stats(TaskStats(discovered=21, downloaded=1, skipped=20, duplicate_count=0, failed=0))

    assert window.found_block.value.text() == "21"
    assert window.success_block.value.text() == "1"
    assert window.skipped_block.value.text() == "20"
    assert window.fail_block.value.text() == "0"


def test_instagram_form_exposes_author_and_single_post_without_x_controls(window: MainWindow) -> None:
    window.platform.setCurrentIndex(2)
    assert window.platform.currentData() == "instagram"
    assert not window.search_scope_field.isVisible()
    assert not window.only_prompt.isVisible()

    window.keyword.setText("@alice")
    author = window._task_config_from_form()
    assert author.platform == "instagram"
    assert author.collection_mode == "author"
    assert author.source_identifier == "@alice"
    assert author.media_type == "mixed"

    window.mode.setCurrentIndex(1)
    assert window.source_label.text() == "Instagram 帖子 / Reel 链接"
    assert window.search_scope_field.isHidden()
    window.keyword.setText("https://www.instagram.com/p/ABC123/")
    post = window._task_config_from_form()
    assert post.collection_mode == "single_post"
    assert post.source_identifier.endswith("ABC123/")


def test_platform_combo_uses_registry_icons_and_enables_youtube(window: MainWindow) -> None:
    assert window.platform.count() == 4
    assert [window.platform.itemData(i) for i in range(window.platform.count())] == [
        "pinterest", "x", "instagram", "youtube",
    ]
    assert all(not window.platform.itemIcon(i).isNull() for i in range(window.platform.count()))

    youtube_item = window.platform.model().item(3)
    assert youtube_item is not None
    assert youtube_item.flags() & Qt.ItemFlag.ItemIsEnabled

    instagram_item = window.platform.model().item(2)
    assert instagram_item is not None
    assert instagram_item.flags() & Qt.ItemFlag.ItemIsEnabled

    for index in (0, 1, 2):
        item = window.platform.model().item(index)
        assert item is not None
        assert item.flags() & Qt.ItemFlag.ItemIsEnabled

    window.platform.setCurrentIndex(2)
    assert window.platform.currentData() == "instagram"
    assert [window.mode.itemData(i) for i in range(window.mode.count())] == ["author", "single_post", "hashtag"]
    assert [window.media_type.itemData(i) for i in range(window.media_type.count())] == ["image", "video", "mixed"]
    assert window.start.isEnabled()
    assert window.media_type.currentData() == "mixed"
    assert "Instagram" in window.mode_hint.text()


def test_instagram_hashtag_form_normalizes_input_and_exposes_distinct_mode(window: MainWindow) -> None:
    window.platform.setCurrentIndex(2)
    window.mode.setCurrentIndex(2)
    assert window.source_label.text() == "Hashtag / 标签"
    assert window.keyword.placeholderText() == "例如：aiart 或 #aiart"
    window.keyword.setText("#AiArt")

    config = window._task_config_from_form()

    assert config.collection_mode == "hashtag"
    assert config.hashtag == "aiart"
    assert config.source_identifier == "aiart"
    assert config.canonical_source_value == "aiart"


def test_pinterest_user_and_board_forms_keep_source_fields_and_legacy_modes(window: MainWindow) -> None:
    assert [window.mode.itemData(i) for i in range(window.mode.count())] == [
        "search", "single_post", "user", "board", "home", "current_page",
    ]
    window.mode.setCurrentIndex(2)
    window.keyword.setText("https://www.pinterest.com/alice/")
    user = window._task_config_from_form()
    assert user.collection_mode == "user"
    assert user.username.endswith("alice/")
    assert user.canonical_source_value == "alice"
    assert window.keyword.placeholderText() == "可粘贴用户名或 Pinterest 用户主页，软件会自动识别。"
    assert "All Pins" in window.mode_hint.text()

    window.mode.setCurrentIndex(3)
    window.keyword.setText("https://www.pinterest.com/alice/ai-art/")
    board = window._task_config_from_form()
    assert board.collection_mode == "board"
    assert board.board_url.endswith("/ai-art/")
    assert board.canonical_source_value.endswith("/alice/ai-art/")
    assert "Sections" in window.mode_hint.text()

    window.mode.setCurrentIndex(4)
    assert window.mode.currentData() == "home"
    assert not window.keyword.isVisible()
    window.mode.setCurrentIndex(5)
    assert window.mode.currentData() == "current_page"


def test_youtube_platform_shows_url_and_quality_controls(window: MainWindow) -> None:
    window.platform.setCurrentIndex(3)

    assert window.platform.currentData() == "youtube"
    assert window.mode.currentData() == "auto"
    assert not window.keyword.isHidden()
    assert not window.video_quality.isHidden()
    assert "YouTube" in window.source_label.text()


def test_youtube_search_form_uses_no_wheel_filters_and_normalizes_keywords(window: MainWindow) -> None:
    window.platform.setCurrentIndex(window.platform.findData("youtube"))
    window.mode.setCurrentIndex(window.mode.findData("youtube_search"))
    window.youtube_search_keywords.setPlainText("car restoration\n\nCAR restoration\nhouse renovation")
    window.youtube_search_max_duration.setCurrentIndex(window.youtube_search_max_duration.findData(300))
    window.youtube_search_date_range.setCurrentIndex(window.youtube_search_date_range.findData("30d"))

    assert window.keyword.isHidden()
    assert not window.youtube_search_keywords.isHidden()
    assert window.youtube_search_keywords in _new_task_card(window, "目标内容").findChildren(QTextEdit)
    for combo in (
        window.youtube_search_sort, window.youtube_search_content_type,
        window.youtube_search_max_duration, window.youtube_search_date_range,
    ):
        assert isinstance(combo, NoWheelComboBox)
    assert not _wheel_combo(window.youtube_search_max_duration, -120).isAccepted()
    assert window.youtube_search_max_duration.currentData() == 300
    config = window._task_config_from_form()
    assert config.youtube_search_keywords == ("car restoration", "house renovation")
    assert config.youtube_search_date_range == "30d"

    window.youtube_search_content_type.setCurrentIndex(window.youtube_search_content_type.findData("shorts"))
    assert window.youtube_search_content_type.currentData() == "shorts"
    assert window.youtube_search_max_duration.isHidden()
    assert not window.youtube_search_strict_shorts.isHidden()
    window.youtube_search_strict_shorts.setChecked(True)
    window.youtube_search_source.setCurrentIndex(window.youtube_search_source.findData("browser_shorts"))
    assert window._task_config_from_form().youtube_search_strict_shorts is True
    assert window._task_config_from_form().youtube_search_source == "browser_shorts"
    assert not _wheel_combo(window.youtube_search_content_type, -120).isAccepted()
    assert window.youtube_search_content_type.currentData() == "shorts"


def _wheel_combo(combo, delta: int) -> QWheelEvent:
    center = combo.rect().center()
    event = QWheelEvent(
        QPointF(center), QPointF(combo.mapToGlobal(center)), QPoint(), QPoint(0, delta),
        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate, False,
    )
    QApplication.sendEvent(combo, event)
    return event


def test_configuration_combos_ignore_wheel_without_losing_normal_selection(window: MainWindow) -> None:
    window.platform.setCurrentIndex(window.platform.findData("youtube"))
    window.video_quality.setCurrentIndex(window.video_quality.findData("best"))
    window.youtube_cookie_source.setCurrentIndex(window.youtube_cookie_source.findData("firefox"))

    for combo in (
        window.platform, window.mode, window.search_scope, window.media_type,
        window.video_quality, window.youtube_cookie_source,
    ):
        assert isinstance(combo, NoWheelComboBox)

    assert not _wheel_combo(window.video_quality, 120).isAccepted()
    assert not _wheel_combo(window.video_quality, -120).isAccepted()
    assert window.video_quality.currentData() == "best"
    assert not _wheel_combo(window.youtube_cookie_source, -120).isAccepted()
    assert window.youtube_cookie_source.currentData() == "firefox"

    # Wheel suppression only affects scrolling. The standard selection API and
    # popup remain available for ordinary click/keyboard selection.
    window.video_quality.showPopup()
    assert window.video_quality.view().isVisible()
    window.video_quality.hidePopup()
    window.video_quality.setCurrentIndex(window.video_quality.findData("720"))
    assert window.video_quality.currentData() == "720"


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


def test_task_failed_releases_ui_before_reporting_error(window: MainWindow, monkeypatch) -> None:
    errors: list[str] = []
    monkeypatch.setattr(window, "_show_task_error", errors.append)
    window._set_running(True)

    window.task_failed("gallery-dl extraction failed: AbortExtraction")

    assert errors == ["gallery-dl extraction failed: AbortExtraction"]
    assert not window._task_active
    assert window.status.text() == "任务失败"
    assert window.start.isEnabled()
    assert not window.pause.isEnabled()
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
