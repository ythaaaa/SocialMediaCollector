from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit, urlunsplit

from PySide6.QtCore import QSettings, QSize, Qt, QThread, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTextEdit, QToolButton, QVBoxLayout, QWidget,
    QTabWidget,
)

from src.downloader import DownloadEngine
from src.downloader.queue_runner import QueueRunner
from src.browser.session import BrowserSession, BrowserSessionManager, find_chrome_path, regular_chrome_login_command
from src.models import TaskConfig, TaskStats
from src.platforms import get_platform_spec, media_type_label, mode_label, platform_specs
from src.storage import CollectorDatabase, QueueStore
from src.tasks import BatchInputError, QueueTask, build_batch_configs, remove_queue_tasks
from src.ui.input_state import ModeInputState
from src.ui.resources import app_icon_path, resource_path, set_windows_app_user_model_id, stylesheet
from src.utils.paths import application_data_directory, browser_profile_directory, profile_directory_for_platform
from src.utils.instagram import normalize_hashtag
from src.version import APP_VERSION


_RUNTIME_HOOKS_INSTALLED = False


def _sanitize_runtime_text(value: object) -> str:
    text = str(value or "")
    for token in re.findall(r"https?://[^\s)]+", text):
        parsed = urlsplit(token)
        safe = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        text = text.replace(token, safe)
    return text


def _runtime_log_path() -> Path:
    for root in (Path.cwd(), application_data_directory()):
        try:
            directory = root / "logs"
            directory.mkdir(parents=True, exist_ok=True)
            return directory / "runtime-crash.log"
        except OSError:
            continue
    return Path("runtime-crash.log")


def _write_runtime_exception(exc: BaseException, config: TaskConfig | None = None, context: dict[str, object] | None = None) -> None:
    details = dict(context or {})
    if config is not None:
        details.update(
            task_id=config.task_id,
            platform=config.canonical_platform(),
            collection_mode=config.canonical_collection_mode(),
        )
    details.setdefault("task_id", "")
    details.setdefault("platform", "")
    details.setdefault("collection_mode", "")
    lines = [
        f"timestamp={datetime.now().isoformat(timespec='seconds')}",
        f"thread={threading.current_thread().name}",
        f"task_id={_sanitize_runtime_text(details['task_id'])}",
        f"platform={_sanitize_runtime_text(details['platform'])}",
        f"collection_mode={_sanitize_runtime_text(details['collection_mode'])}",
        f"exception_type={type(exc).__name__}",
        f"message={_sanitize_runtime_text(exc)}",
        "traceback:",
        _sanitize_runtime_text("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))),
        "-" * 80,
    ]
    try:
        _runtime_log_path().open("a", encoding="utf-8").write("\n".join(lines) + "\n")
    except OSError:
        pass


def _install_runtime_exception_hooks() -> None:
    global _RUNTIME_HOOKS_INSTALLED
    if _RUNTIME_HOOKS_INSTALLED:
        return
    _RUNTIME_HOOKS_INSTALLED = True
    previous_sys_hook = sys.excepthook

    def sys_hook(exc_type, exc_value, exc_traceback) -> None:
        exception = exc_value if isinstance(exc_value, BaseException) else Exception(str(exc_value))
        _write_runtime_exception(exception)
        previous_sys_hook(exc_type, exc_value, exc_traceback)

    sys.excepthook = sys_hook
    previous_thread_hook = getattr(threading, "excepthook", None)
    if previous_thread_hook is not None:
        def thread_hook(args) -> None:
            _write_runtime_exception(args.exc_value)
            previous_thread_hook(args)

        threading.excepthook = thread_hook


class RuntimeApplication(QApplication):
    """Keep a callback exception from terminating the Qt GUI process."""

    def notify(self, receiver, event) -> bool:  # noqa: N802 - Qt API
        try:
            return super().notify(receiver, event)
        except BaseException as exc:
            _write_runtime_exception(exc)
            return False


class NoWheelMixin:
    """Keep page scrolling natural without changing a numeric field by accident."""

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt event API
        event.ignore()


class NoWheelSpinBox(NoWheelMixin, QSpinBox):
    pass


class NoWheelDoubleSpinBox(NoWheelMixin, QDoubleSpinBox):
    pass


class Worker(QThread):
    log = Signal(str)
    stats = Signal(object)
    task_started = Signal(str)
    control_status = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, config: TaskConfig, profile_dir: Path, browser_manager: BrowserSessionManager | None = None) -> None:
        super().__init__()
        self.engine = DownloadEngine(
            config,
            profile_dir,
            self.log.emit,
            self.stats.emit,
            lambda path: self.task_started.emit(str(path)),
            browser_manager=browser_manager,
        )
        self.engine.on_control_status = self.control_status.emit

    def run(self) -> None:
        try:
            self.done.emit(str(self.engine.run()))
        except BaseException as exc:
            _write_runtime_exception(exc, self.engine.config)
            try:
                self.failed.emit(str(exc) or type(exc).__name__)
            except BaseException as signal_exc:
                _write_runtime_exception(signal_exc, self.engine.config, {"callback": "Worker.failed"})


class ChromeLoginWorker(QThread):
    """Keep a normal system Chrome process open for manual login only."""

    log = Signal(str)
    failed = Signal(str)

    def __init__(self, browser_manager: BrowserSessionManager, target_url: str, platform: str) -> None:
        super().__init__()
        self.browser_manager = browser_manager
        self.target_url = target_url
        self.platform = platform

    def run(self) -> None:
        try:
            if self.platform == "instagram":
                self.browser_manager.open_manual_login_browser(self.target_url)
            else:
                self.browser_manager.open_browser(self.target_url)
            if self.platform == "instagram":
                self.log.emit("请在 Chrome 中完成 Instagram 登录。登录成功后请关闭该 Chrome 窗口，再返回采集器开始采集。程序不会操作登录页面。")
            else:
                self.log.emit("请在托管浏览器中人工登录 Pinterest / X，并导航到要采集的页面。")
            while not self.isInterruptionRequested():
                running = self.browser_manager.is_login_running() if self.platform == "instagram" else self.browser_manager.is_running()
                if not running:
                    break
                time.sleep(0.5)
        except BaseException as exc:
            _write_runtime_exception(
                exc,
                context={"platform": self.platform, "collection_mode": "login"},
            )
            try:
                self.failed.emit(str(exc) or type(exc).__name__)
            except BaseException as signal_exc:
                _write_runtime_exception(
                    signal_exc,
                    context={"platform": self.platform, "collection_mode": "login", "callback": "ChromeLoginWorker.failed"},
                )


class StatBlock(QFrame):
    def __init__(self, title: str, color: str) -> None:
        super().__init__()
        self.setObjectName("statBlock")
        self.setProperty("accent", color)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("statLabel")
        self.value = QLabel("0")
        self.value.setObjectName("statValue")
        self.value.setProperty("accent", color)
        layout.addWidget(self.title)
        layout.addWidget(self.value)

    def set_value(self, value: int) -> None:
        self.value.setText(str(value))


class MainWindow(QMainWindow):
    browser_log = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"社媒素材采集器 v{APP_VERSION}")
        self.setWindowIcon(QIcon(app_icon_path()))
        self.resize(1080, 800)
        self.setMinimumSize(920, 700)
        self.worker: Worker | None = None
        self.login_worker: ChromeLoginWorker | None = None
        self.last_task_dir: Path | None = None
        self.settings = QSettings("PinterestLocalDownloader", "SocialMediaCollector")
        self._input_state = ModeInputState()
        self.browser_log.connect(self.append_log, Qt.ConnectionType.QueuedConnection)
        self.browser_manager = BrowserSessionManager(browser_profile_directory(), self.browser_log.emit)
        self.queue_store = QueueStore()
        self.database = CollectorDatabase()
        self.queue_tasks: list[QueueTask] = []
        self.queue_runner: QueueRunner | None = None
        self._control_state = "idle"
        self._task_active = False

        self._create_controls()
        self._build_window()
        self._wire_actions()
        self._load_queue()
        self.database.mark_running_interrupted()
        self._refresh_history()
        self._set_running(False)

    def _create_controls(self) -> None:
        self.platform = QComboBox()
        self.platform.setIconSize(QSize(18, 18))
        self._populate_platforms()
        self.platform.setMinimumHeight(36)
        self.mode = QComboBox()
        self.mode.setMinimumHeight(36)
        self._search_scope_state = "media"
        self.search_scope = QComboBox()
        self.search_scope.addItem("媒体", "media")
        self.search_scope.addItem("热门", "top")
        self.search_scope.setMinimumHeight(36)
        self.search_scope_label = self._field_label("搜索范围")
        self.search_scope_field = self._parameter_field_with_label(self.search_scope_label, self.search_scope)
        self.media_type = QComboBox()
        self.media_type.addItem("仅图片", "image")
        self.media_type.addItem("仅视频", "video")
        self.media_type.addItem("图片 + 视频", "mixed")
        self.media_type.setMinimumHeight(36)
        self.media_type_label = self._field_label("下载内容类型")
        self.keyword = QLineEdit()
        self.keyword.setMinimumHeight(42)
        self.source_label = self._field_label("搜索关键词")
        self.current_page_info = QLabel()
        self.current_page_info.setObjectName("currentPageInfo")
        self.current_page_info.setWordWrap(True)
        self.current_page_info.setVisible(False)
        self.batch_mode = QCheckBox("批量任务")
        self.batch_input = QTextEdit()
        self.batch_input.setPlaceholderText("每行一个关键词或 X 作者，空行会忽略并自动去重")
        self.batch_input.setMinimumHeight(92)
        self.batch_input.setMaximumHeight(130)
        self.batch_input.setVisible(False)
        self.add_queue = QPushButton("添加到队列")
        self.add_queue.setObjectName("primaryButton")
        self.add_queue.setVisible(False)
        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("cardSubtitle")
        self.mode_hint.setWordWrap(True)
        self.only_author = QCheckBox("仅采集作者本人内容")
        self.only_author.setChecked(True)
        self.only_prompt = QCheckBox("仅采集疑似包含 Prompt 的帖子")
        self.only_prompt.setChecked(False)
        self.skip_history_duplicates = QCheckBox("跳过历史已下载素材")
        self.skip_history_duplicates.setChecked(False)
        self.skip_history_duplicates.setToolTip("勾选后使用历史数据库和历史 SHA-256 记录去重；默认允许重新下载。")

        self.count = self._spin_box(1, 10000, 100)
        self.min_width = self._spin_box(1, 10000, 800)
        self.min_height = self._spin_box(1, 10000, 800)
        self.interval = NoWheelDoubleSpinBox()
        self.interval.setRange(0, 60)
        self.interval.setValue(1)
        self.interval.setSingleStep(0.5)
        self.interval.setSuffix(" 秒")
        self.interval.setMinimumHeight(36)

        default_save_root = str(Path.home() / "Pinterest素材")
        saved_save_root = str(self.settings.value("save_root", "") or "").strip()
        self.output = QLineEdit(saved_save_root or default_save_root)
        self.output.setMinimumHeight(38)
        self.output.textChanged.connect(self._remember_save_root)
        self.choose = QPushButton("选择目录")
        self.choose.setIcon(QIcon(resource_path("icons/folder.svg")))

        self.start = QPushButton("开始下载")
        self.start.setObjectName("primaryButton")
        self.start.setMinimumHeight(44)
        self.pause = QPushButton("暂停")
        self.pause.setObjectName("secondaryButton")
        self.resume = QPushButton("继续")
        self.resume.setObjectName("secondaryButton")
        self.stop = QPushButton("停止")
        self.stop.setObjectName("secondaryButton")
        self.start_queue = QPushButton("开始队列")
        self.start_queue.setObjectName("primaryButton")
        self.login_browser = QPushButton("打开浏览器 / 登录")
        self.login_browser.setObjectName("secondaryButton")
        self.open_dir = QPushButton("打开保存目录")
        self.open_dir.setIcon(QIcon(resource_path("icons/folder.svg")))

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress_percent = QLabel("0%")
        self.progress_percent.setObjectName("progressPercent")
        self.status = QLabel("等待开始")
        self.status.setObjectName("statusText")
        self.found_block = StatBlock("已发现", "blue")
        self.success_block = StatBlock("下载成功", "green")
        self.skipped_block = StatBlock("已跳过", "orange")
        self.fail_block = StatBlock("下载失败", "red")

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(180)
        self.clear_log = QToolButton()
        self.clear_log.setText("清空日志")
        self.current_task = QLabel("当前任务文件夹：尚未创建")
        self.current_task.setObjectName("taskPath")
        self.current_task.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.queue_table = QTableWidget(0, 6)
        self.queue_table.setHorizontalHeaderLabels(["序号", "平台", "模式", "来源", "数量", "状态"])
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.queue_table.setMinimumHeight(150)
        self.delete_selected = QPushButton("删除选中任务")
        self.delete_selected.setObjectName("secondaryButton")
        self.clear_pending = QPushButton("清空等待任务")
        self.clear_pending.setObjectName("secondaryButton")
        self.clear_completed = QPushButton("清空已完成任务")
        self.clear_completed.setObjectName("secondaryButton")
        self.history_table = QTableWidget(0, 8)
        self.history_table.setHorizontalHeaderLabels(["时间", "平台", "模式", "来源", "目标", "成功", "重复", "状态"])
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.history_table.setMinimumHeight(170)
        self.history_empty = QLabel("暂无任务历史")
        self.history_empty.setObjectName("emptyState")
        self.history_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.history_empty.setMinimumHeight(110)
        self.refresh_history = QPushButton("刷新历史")
        self.delete_history = QPushButton("删除选中记录")
        self.delete_history.setObjectName("secondaryButton")
        self.clear_history = QPushButton("清空全部历史")
        self.clear_history.setObjectName("warningButton")
        self.open_history_dir = QPushButton("打开历史目录")
        self.recollect_history = QPushButton("重新采集")
        self.retry_history = QPushButton("重试失败任务")
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("高级筛选  ›")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setObjectName("sectionToggle")
        self.advanced_panel = QWidget()
        self.advanced_panel.setVisible(False)
        self._platform_changed(0)

    @staticmethod
    def _spin_box(minimum: int, maximum: int, value: int) -> QSpinBox:
        control = NoWheelSpinBox()
        control.setRange(minimum, maximum)
        control.setValue(value)
        control.setMinimumHeight(36)
        return control

    def _build_window(self) -> None:
        container = QWidget()
        container.setObjectName("page")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(34, 28, 34, 24)
        layout.setSpacing(18)
        layout.addLayout(self._build_header())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_new_task_page(), "新建任务")
        self.tabs.addTab(self._build_queue_page(), "任务队列")
        self.tabs.addTab(self._build_history_page(), "任务历史")
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(container)

    @staticmethod
    def _scroll_page(content: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        return scroll

    def _build_new_task_page(self) -> QScrollArea:
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 20, 12, 24)
        layout.setSpacing(16)
        layout.addWidget(self._build_source_card())
        layout.addWidget(self._build_download_card())
        layout.addWidget(self._build_advanced_card())
        layout.addWidget(self._build_target_card())
        layout.addWidget(self._build_actions_card())
        layout.addWidget(self._build_progress_card())
        layout.addWidget(self._build_log_card())
        layout.addLayout(self._build_footer())
        return self._scroll_page(content)

    def _build_queue_page(self) -> QScrollArea:
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 20, 12, 24)
        layout.addWidget(self._build_queue_card())
        layout.addStretch(1)
        return self._scroll_page(content)

    def _build_history_page(self) -> QScrollArea:
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 20, 12, 24)
        layout.addWidget(self._build_history_card())
        layout.addStretch(1)
        return self._scroll_page(content)

    def _build_header(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(4)
        title = QLabel("社媒素材采集器")
        title.setObjectName("pageTitle")
        subtitle = QLabel("配置任务、执行队列并追踪社交媒体素材采集历史")
        subtitle.setObjectName("pageSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        return layout

    def _build_settings_card(self) -> QFrame:
        card, layout = self._card("任务设置", "设置关键词、图片数量和本地保存位置")
        layout.addWidget(self._field_label("平台"))
        layout.addWidget(self.platform)
        layout.addWidget(self._field_label("采集模式"))
        layout.addWidget(self.mode)
        layout.addWidget(self.search_scope_label)
        layout.addWidget(self.search_scope)
        layout.addWidget(self.media_type_label)
        layout.addWidget(self.media_type)
        layout.addWidget(self.source_label)
        layout.addWidget(self.keyword)
        layout.addWidget(self.batch_mode)
        layout.addWidget(self.batch_input)
        layout.addWidget(self.add_queue)
        layout.addWidget(self.mode_hint)
        layout.addWidget(self.only_author)
        layout.addWidget(self.only_prompt)
        numbers = QGridLayout()
        numbers.setHorizontalSpacing(12)
        for column, (label, control) in enumerate((
            ("下载数量", self.count),
            ("最低宽度", self.min_width),
            ("最低高度", self.min_height),
            ("下载间隔", self.interval),
        )):
            numbers.addWidget(self._parameter_field(label, control), 0, column)
            numbers.setColumnStretch(column, 1)
        layout.addLayout(numbers)
        layout.addWidget(self._field_label("保存目录"))
        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(self.choose)
        output_widget = QWidget()
        output_widget.setLayout(output_row)
        layout.addWidget(output_widget)
        return card

    def _build_source_card(self) -> QFrame:
        card, layout = self._card("采集来源", "先选择平台和采集模式，再配置目标内容。")
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.addWidget(self._parameter_field("平台", self.platform), 0, 0)
        grid.addWidget(self._parameter_field("采集模式", self.mode), 0, 1)
        grid.addWidget(self._parameter_field_with_label(self.media_type_label, self.media_type), 0, 2)
        for column in range(3):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        return card

    @staticmethod
    def _parameter_field_with_label(label: QLabel, control: QWidget) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(label)
        layout.addWidget(control)
        return widget

    def _build_target_card(self) -> QFrame:
        card, layout = self._card("目标内容", "单任务填写一项；批量任务每行填写一个关键词或作者。")
        layout.addWidget(self.source_label)
        layout.addWidget(self.keyword)
        layout.addWidget(self.search_scope_field)
        layout.addWidget(self.current_page_info)
        self.batch_mode.setObjectName("modeSwitch")
        layout.addWidget(self.batch_mode)
        layout.addWidget(self.batch_input)
        layout.addWidget(self.add_queue)
        layout.addWidget(self.mode_hint)
        layout.addWidget(self.only_author)
        layout.addWidget(self.only_prompt)
        return card

    def _build_download_card(self) -> QFrame:
        card, layout = self._card("下载设置", "设置本次任务的目标数量和固定保存根目录。")
        grid = QGridLayout()
        grid.addWidget(self._parameter_field("下载数量", self.count), 0, 0)
        grid.setColumnStretch(0, 1)
        layout.addLayout(grid)
        layout.addWidget(self._field_label("保存目录"))
        output_row = QHBoxLayout()
        output_row.setSpacing(10)
        output_row.addWidget(self.output, 1)
        output_row.addWidget(self.choose)
        output_widget = QWidget()
        output_widget.setLayout(output_row)
        layout.addWidget(output_widget)
        return card

    def _build_advanced_card(self) -> QFrame:
        card, layout = self._card("高级筛选", "默认收起；用于控制素材尺寸与请求节奏。")
        layout.addWidget(self.advanced_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        advanced_layout = QGridLayout(self.advanced_panel)
        advanced_layout.setHorizontalSpacing(12)
        advanced_layout.addWidget(self._parameter_field("最低宽度", self.min_width), 0, 0)
        advanced_layout.addWidget(self._parameter_field("最低高度", self.min_height), 0, 1)
        advanced_layout.addWidget(self._parameter_field("下载间隔", self.interval), 0, 2)
        advanced_layout.addWidget(self.skip_history_duplicates, 1, 0, 1, 3)
        for column in range(3):
            advanced_layout.setColumnStretch(column, 1)
        layout.addWidget(self.advanced_panel)
        return card

    def _build_actions_card(self) -> QFrame:
        card, layout = self._card("操作", "任务运行期间，界面始终保持可响应")
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addWidget(self.start, 2)
        buttons.addWidget(self.pause)
        buttons.addWidget(self.resume)
        buttons.addWidget(self.stop)
        buttons.addWidget(self.login_browser)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return card

    def _build_queue_card(self) -> QFrame:
        card, layout = self._card("任务队列", "批量任务按顺序执行，同一时间只运行一个采集任务。")
        layout.addWidget(self.queue_table)
        actions = QHBoxLayout()
        actions.addWidget(self.delete_selected)
        actions.addWidget(self.clear_pending)
        actions.addWidget(self.clear_completed)
        actions.addStretch(1)
        actions.addWidget(self.start_queue)
        layout.addLayout(actions)
        return card

    def _build_history_card(self) -> QFrame:
        card, layout = self._card("任务历史", "记录单任务与队列任务；重新采集会创建新的任务记录。")
        layout.addWidget(self.history_table)
        layout.addWidget(self.history_empty)
        actions = QHBoxLayout()
        actions.addWidget(self.delete_history)
        actions.addWidget(self.clear_history)
        actions.addStretch(1)
        actions.addWidget(self.refresh_history)
        actions.addWidget(self.open_history_dir)
        actions.addWidget(self.recollect_history)
        actions.addWidget(self.retry_history)
        layout.addLayout(actions)
        return card

    def _build_progress_card(self) -> QFrame:
        card, layout = self._card("下载进度", "")
        top = QHBoxLayout()
        top.addWidget(self.status)
        top.addStretch(1)
        top.addWidget(self.progress_percent)
        layout.addLayout(top)
        layout.addWidget(self.progress)
        stats = QHBoxLayout()
        stats.setSpacing(10)
        for block in (self.found_block, self.success_block, self.skipped_block, self.fail_block):
            stats.addWidget(block, 1)
        layout.addLayout(stats)
        return card

    def _build_log_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(10)
        heading = QHBoxLayout()
        title = QLabel("实时记录")
        title.setObjectName("cardTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        heading.addWidget(self.clear_log)
        layout.addLayout(heading)
        subtitle = QLabel("下载记录仅显示在此处，清空不会删除本地文件或 CSV")
        subtitle.setObjectName("cardSubtitle")
        layout.addWidget(subtitle)
        layout.addWidget(self.log, 1)
        return card

    def _build_footer(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(12)
        layout.addWidget(self.current_task, 1)
        layout.addWidget(self.open_dir)
        return layout

    @staticmethod
    def _field_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("inlineLabel")
        return label

    def _parameter_field(self, text: str, control: QWidget) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(self._field_label(text))
        layout.addWidget(control)
        return widget

    @staticmethod
    def _card(title: str, subtitle: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 20)
        layout.setSpacing(14)
        title_label = QLabel(title)
        title_label.setObjectName("cardTitle")
        layout.addWidget(title_label)
        if subtitle:
            subtitle_label = QLabel(subtitle)
            subtitle_label.setObjectName("cardSubtitle")
            layout.addWidget(subtitle_label)
        return card, layout

    def _wire_actions(self) -> None:
        self.choose.clicked.connect(self.choose_dir)
        self.start.clicked.connect(self.start_task)
        self.pause.clicked.connect(self.pause_task)
        self.resume.clicked.connect(self.resume_task)
        self.stop.clicked.connect(self.stop_task)
        self.login_browser.clicked.connect(self.open_login_browser)
        self.open_dir.clicked.connect(self.open_output)
        self.clear_log.clicked.connect(self.log.clear)
        self.batch_mode.toggled.connect(self._toggle_batch_mode)
        self.add_queue.clicked.connect(self.add_batch_tasks)
        self.start_queue.clicked.connect(self.start_queue_tasks)
        self.delete_selected.clicked.connect(self.delete_selected_tasks)
        self.clear_pending.clicked.connect(self.clear_pending_tasks)
        self.clear_completed.clicked.connect(self.clear_completed_tasks)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        self.refresh_history.clicked.connect(self._refresh_history)
        self.delete_history.clicked.connect(self.delete_selected_history)
        self.clear_history.clicked.connect(self.clear_all_history)
        self.open_history_dir.clicked.connect(self.open_history_directory)
        self.recollect_history.clicked.connect(self.recollect_selected)
        self.retry_history.clicked.connect(self.retry_selected)
        self.history_table.itemSelectionChanged.connect(self._history_selection_changed)
        self.platform.currentIndexChanged.connect(self._platform_changed)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.search_scope.currentIndexChanged.connect(self._search_scope_changed)

    def _search_scope_changed(self, _index: int) -> None:
        if self.platform.currentData() == "x" and self.mode.currentData() == "search":
            self._search_scope_state = str(self.search_scope.currentData() or "media")

    def _populate_platforms(self) -> None:
        self.platform.clear()
        for index, spec in enumerate(platform_specs()):
            self.platform.addItem(QIcon(resource_path(spec.icon)), spec.label, spec.key)
            if not spec.enabled:
                item = self.platform.model().item(index)
                if item is not None:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)

    def _populate_platform_options(self, spec) -> None:
        self.mode.blockSignals(True)
        self.mode.clear()
        for collection_mode in spec.collection_modes:
            self.mode.addItem(mode_label(collection_mode), collection_mode)
        default_mode = self.mode.findData(spec.default_mode)
        self.mode.setCurrentIndex(default_mode if default_mode >= 0 else 0)
        self.mode.blockSignals(False)

        self.media_type.blockSignals(True)
        self.media_type.clear()
        for media_type in spec.media_types:
            self.media_type.addItem(media_type_label(media_type), media_type)
        default_media_type = self.media_type.findData(spec.default_media_type)
        self.media_type.setCurrentIndex(default_media_type if default_media_type >= 0 else 0)
        self.media_type.blockSignals(False)

    def _show_development_platform(self, spec) -> None:
        self.source_label.setVisible(False)
        self.keyword.setVisible(False)
        self.current_page_info.setVisible(False)
        self.search_scope_label.setVisible(False)
        self.search_scope.setVisible(False)
        self.search_scope_field.setVisible(False)
        self.only_author.setVisible(False)
        self.only_prompt.setVisible(False)
        self.batch_mode.setVisible(False)
        self.batch_input.setVisible(False)
        self.add_queue.setVisible(False)
        self.mode_hint.setText(f"{spec.label.split(' ·', 1)[0]} 正在开发中，当前版本暂不支持实际采集。")
        self.mode_hint.setVisible(True)
        self.mode.setEnabled(False)
        self.media_type.setEnabled(False)
        self._update_batch_controls()

    def _set_platform_controls_enabled(self, enabled: bool) -> None:
        editable = enabled and not self._task_active
        self.mode.setEnabled(editable)
        self.media_type.setEnabled(editable)
        self.start_queue.setEnabled(editable)
        self.login_browser.setEnabled(editable and not (self.login_worker and self.login_worker.isRunning()))

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_panel.setVisible(expanded)
        self.advanced_toggle.setText("高级筛选  ‹" if expanded else "高级筛选  ›")

    def _platform_changed(self, _index: int) -> None:
        self._input_state.remember(self.keyword.text())
        spec = get_platform_spec(str(self.platform.currentData() or ""))
        if spec is None:
            return
        self._populate_platform_options(spec)
        self._set_platform_controls_enabled(spec.enabled)
        self._mode_changed(self.mode.currentIndex())

    def _mode_changed(self, _index: int) -> None:
        platform = self.platform.currentData()
        mode = self.mode.currentData()
        spec = get_platform_spec(str(platform or ""))
        if spec is None:
            return
        restored_value = self._input_state.switch((platform, mode), self.keyword.text())
        self.keyword.blockSignals(True)
        self.keyword.setText(restored_value)
        self.keyword.blockSignals(False)
        self.current_page_info.setVisible(False)
        if not spec.enabled:
            self._show_development_platform(spec)
            return
        self._set_platform_controls_enabled(True)
        if platform == "instagram":
            author_mode = mode == "author"
            hashtag_mode = mode == "hashtag"
            self.source_label.setText(
                "作者用户名 / 主页地址" if author_mode
                else "Hashtag / 标签" if hashtag_mode
                else "Instagram 帖子 / Reel 链接"
            )
            self.source_label.setVisible(True)
            self.keyword.setVisible(True)
            self.keyword.setEnabled(True)
            self.keyword.setPlaceholderText(
                "username、@username 或 Instagram 作者主页地址" if author_mode
                else "例如：aiart 或 #aiart" if hashtag_mode
                else "https://www.instagram.com/p/.../ 或 /reel/.../"
            )
            self.mode_hint.setText(
                "从 Instagram 作者主页发现帖子和 Reels。" if author_mode
                else "从 Instagram Hashtag 标签页发现帖子和 Reels。" if hashtag_mode
                else "只处理指定的 Instagram 图片帖、轮播帖或 Reel。"
            )
            self.only_author.setVisible(False)
            self.only_prompt.setVisible(False)
            self.media_type_label.setVisible(True)
            self.media_type.setVisible(True)
            self.search_scope_label.setVisible(False)
            self.search_scope.setVisible(False)
            self.search_scope_field.setVisible(False)
            self._update_batch_controls()
            return
        if platform == "x":
            author_mode = mode == "author"
            search_mode = mode == "search"
            source_mode = author_mode or search_mode
            self.source_label.setText("作者用户名 / 主页地址" if author_mode else "搜索关键词")
            self.source_label.setVisible(source_mode)
            self.keyword.setVisible(source_mode)
            self.keyword.setEnabled(source_mode)
            self.keyword.setPlaceholderText(
                "@username 或 https://x.com/username" if author_mode else "输入 X 搜索关键词"
            )
            self.mode_hint.setText(
                "从该 X 作者主页向下采集图片内容。" if author_mode
                else "根据关键词打开 X 搜索结果并从媒体内容流开始采集。" if search_mode
                else "从 X 首页‘为你推荐’内容流向下采集。"
            )
            self.only_author.setVisible(author_mode)
            self.only_author.setEnabled(author_mode)
            self.only_prompt.setVisible(True)
            self.only_prompt.setEnabled(True)
            self.media_type_label.setVisible(True)
            self.media_type.setVisible(True)
            self.search_scope_label.setVisible(search_mode)
            self.search_scope.setVisible(search_mode)
            self.search_scope_field.setVisible(search_mode)
            if search_mode:
                self.search_scope.blockSignals(True)
                index = self.search_scope.findData(self._search_scope_state)
                if index >= 0:
                    self.search_scope.setCurrentIndex(index)
                self.search_scope.blockSignals(False)
            self._update_batch_controls()
            if not source_mode:
                self.source_label.setVisible(False)
                self.keyword.setVisible(False)
            return

        self.source_label.setText("搜索关键词")
        search = mode == "search"
        current_page = mode == "current_page"
        self.source_label.setVisible(search)
        self.keyword.setVisible(search)
        self.keyword.setEnabled(search)
        self.keyword.setPlaceholderText("输入 Pinterest 搜索关键词")
        if current_page:
            message = "当前页面模式\n请先打开 Pinterest 浏览器并进入目标页面，程序将从当前页面开始采集。"
            if self.browser_manager.is_running():
                try:
                    url = self.browser_manager.current_page_url()
                except Exception:
                    url = ""
                if url:
                    message += f"\n\n当前页面：\n{url}"
            self.current_page_info.setText(message)
            self.current_page_info.setVisible(True)
        else:
            self.mode_hint.setText(
                "根据关键词打开 Pinterest 搜索结果并从顶部开始采集。" if search
                else "从 Pinterest 首页推荐流开始采集，不需要关键词。"
            )
        self.only_author.setVisible(False)
        self.only_prompt.setVisible(False)
        self.media_type_label.setVisible(True)
        self.media_type.setVisible(True)
        self.search_scope_label.setVisible(False)
        self.search_scope.setVisible(False)
        self.search_scope_field.setVisible(False)
        self._update_batch_controls()
        if current_page:
            self.source_label.setVisible(False)
            self.keyword.setVisible(False)

    def _batch_supported(self) -> bool:
        spec = get_platform_spec(str(self.platform.currentData() or ""))
        return bool(spec and spec.enabled and (self.platform.currentData(), self.mode.currentData()) in {
            ("pinterest", "search"),
            ("x", "author"),
            ("x", "search"),
            ("instagram", "hashtag"),
        })

    def _has_disabled_pending_platform(self) -> bool:
        for task in self.queue_tasks:
            if task.status != "pending":
                continue
            spec = get_platform_spec(task.config.canonical_platform())
            if spec is None or not spec.enabled:
                return True
        return False

    def _update_batch_controls(self, running: bool = False) -> None:
        supported = self._batch_supported()
        if not supported and self.batch_mode.isChecked():
            self.batch_mode.blockSignals(True)
            self.batch_mode.setChecked(False)
            self.batch_mode.blockSignals(False)
        self.batch_mode.setVisible(supported)
        batch_enabled = supported and self.batch_mode.isChecked()
        self.keyword.setVisible(not batch_enabled)
        self.batch_input.setVisible(batch_enabled)
        self.add_queue.setVisible(batch_enabled)
        platform = get_platform_spec(str(self.platform.currentData() or ""))
        self.start.setEnabled(bool(platform and platform.enabled) and not running and not batch_enabled)
        self.batch_input.setEnabled(batch_enabled and not running)
        self.add_queue.setEnabled(batch_enabled and not running)

    def _toggle_batch_mode(self, _enabled: bool) -> None:
        self._update_batch_controls()

    def _load_queue(self) -> None:
        self.queue_tasks = self.queue_store.load()
        if self.queue_tasks:
            self.queue_store.save(self.queue_tasks)
        self._refresh_queue_table()

    def _refresh_queue_table(self) -> None:
        labels = {
            "pending": "等待",
            "running": "运行中",
            "paused": "已暂停",
            "completed": "已完成",
            "failed": "失败",
            "stopped": "已停止",
        }
        self.queue_table.setRowCount(len(self.queue_tasks))
        for row, task in enumerate(self.queue_tasks):
            values = [
                str(row + 1),
                task.config.canonical_platform(),
                task.config.canonical_collection_mode(),
                task.source_value,
                str(task.config.target_count),
                labels.get(task.status, task.status),
            ]
            for column, value in enumerate(values):
                self.queue_table.setItem(row, column, QTableWidgetItem(value))
        self.queue_table.resizeColumnsToContents()

    def _task_config_from_form(self) -> TaskConfig:
        platform = str(self.platform.currentData())
        mode = str(self.mode.currentData())
        source_text = self.keyword.text()
        is_search = mode == "search"
        hashtag = normalize_hashtag(source_text) if platform == "instagram" and mode == "hashtag" else ""
        return TaskConfig(
            keyword=source_text if is_search and platform == "pinterest" else "",
            target_count=self.count.value(),
            output_root=self._save_root(),
            min_width=self.min_width.value(),
            min_height=self.min_height.value(),
            interval_seconds=self.interval.value(),
            collection_mode=mode,
            platform=platform,
            source_identifier=hashtag if hashtag else source_text if platform in {"x", "instagram"} and mode in {"author", "search", "single_post"} else "",
            hashtag=hashtag,
            only_author_content=self.only_author.isChecked() if platform == "x" and mode == "author" else False,
            media_type=str(self.media_type.currentData()),
            search_scope=str(self.search_scope.currentData()) if platform == "x" and mode == "search" else "media",
            only_prompt_posts=self.only_prompt.isChecked() if platform == "x" else False,
            skip_history_duplicates=self.skip_history_duplicates.isChecked(),
        )

    def _save_root(self) -> Path:
        return Path(self.output.text()).expanduser()

    def _remember_save_root(self, value: str) -> None:
        if value.strip():
            self.settings.setValue("save_root", value.strip())
            self.settings.sync()

    def add_batch_tasks(self) -> None:
        try:
            configs = build_batch_configs(self._task_config_from_form(), self.batch_input.toPlainText())
            if not configs:
                raise ValueError("请至少输入一行有效的批量任务来源")
        except (BatchInputError, ValueError) as exc:
            QMessageBox.warning(self, "无法添加批量任务", str(exc))
            return
        self.queue_tasks.extend(QueueTask(config) for config in configs)
        self.queue_store.save(self.queue_tasks)
        self._refresh_queue_table()
        self.batch_input.clear()
        self.append_log(f"已添加 {len(configs)} 个批量任务。")

    def _selected_queue_tasks(self) -> list[QueueTask]:
        rows = sorted({index.row() for index in self.queue_table.selectionModel().selectedRows()})
        return [self.queue_tasks[row] for row in rows if 0 <= row < len(self.queue_tasks)]

    def delete_selected_tasks(self) -> None:
        selected = self._selected_queue_tasks()
        if not selected:
            QMessageBox.information(self, "任务队列", "请先选择要删除的任务。")
            return
        try:
            remaining = remove_queue_tasks(self.queue_tasks, {task.task_id for task in selected})
        except ValueError as exc:
            QMessageBox.warning(self, "无法删除任务", str(exc))
            return
        # Preserve the list object held by QueueRunner while removing items.
        self.queue_tasks[:] = remaining
        self.queue_store.save(self.queue_tasks)
        self._refresh_queue_table()
        self.append_log(f"已删除 {len(selected)} 个队列任务。")

    def clear_pending_tasks(self) -> None:
        pending_count = sum(task.status == "pending" for task in self.queue_tasks)
        if not pending_count:
            QMessageBox.information(self, "任务队列", "当前没有等待任务。")
            return
        answer = QMessageBox.question(
            self,
            "清空等待任务",
            "确定清空所有等待任务吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        # Keep running/completed/failed/stopped tasks and preserve Runner's
        # live list reference when this is clicked during queue execution.
        self.queue_tasks[:] = [task for task in self.queue_tasks if task.status != "pending"]
        self.queue_store.save(self.queue_tasks)
        self._refresh_queue_table()
        self.append_log(f"已清空 {pending_count} 个等待任务。")

    def clear_completed_tasks(self) -> None:
        if self.queue_runner and self.queue_runner.isRunning():
            return
        self.queue_tasks[:] = [task for task in self.queue_tasks if task.status != "completed"]
        self.queue_store.save(self.queue_tasks)
        self._refresh_queue_table()

    def start_queue_tasks(self) -> None:
        if self.queue_runner and self.queue_runner.isRunning():
            return
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "无法开始队列", "当前单任务仍在运行，请先停止当前任务。")
            return
        if not any(task.status == "pending" for task in self.queue_tasks):
            QMessageBox.information(self, "任务队列", "队列中没有等待执行的任务。")
            return
        if self._has_disabled_pending_platform():
            QMessageBox.warning(self, "无法开始队列", "该平台正在开发中，当前版本暂不支持实际采集。")
            return
        profile = browser_profile_directory()
        if not self.browser_manager.is_running() and BrowserSession.profile_is_locked(profile):
            QMessageBox.warning(self, "无法开始队列", "请先关闭登录浏览器，再开始采集。")
            return
        for task in self.queue_tasks:
            if task.status == "pending":
                task.config.output_root.mkdir(parents=True, exist_ok=True)
        self.queue_store.save(self.queue_tasks)
        self.queue_runner = QueueRunner(self.queue_tasks, profile, self.queue_store, self.browser_manager)
        self.queue_runner.log.connect(self.append_log)
        self.queue_runner.stats.connect(self.update_stats)
        self.queue_runner.task_started.connect(self.task_started)
        self.queue_runner.task_status.connect(self._queue_task_status)
        self.queue_runner.control_status.connect(self._control_status_changed)
        self.queue_runner.finished_queue.connect(self._queue_finished)
        self._set_running(True)
        self.append_log("开始按顺序执行任务队列。")
        self.queue_runner.start()

    def _queue_task_status(self, task_id: str, status: str, error: str) -> None:
        for task in self.queue_tasks:
            if task.task_id == task_id:
                task.status = status
                task.error = error
                break
        self._refresh_queue_table()

    def _queue_finished(self) -> None:
        was_stopping = self._control_state == "stopping"
        if was_stopping:
            self.status.setText("已停止")
        self.append_log("任务已停止" if was_stopping else "任务队列执行结束。")
        self._refresh_history()
        self.queue_runner = None
        self._set_running(False)

    def _refresh_history(self) -> None:
        labels = {
            "pending": "等待", "running": "运行中", "paused": "已暂停",
            "completed": "已完成", "failed": "失败", "stopped": "已停止",
            "interrupted": "中断（未自动恢复）",
        }
        rows = self.database.list_history()
        has_history = bool(rows)
        self.history_table.setVisible(has_history)
        self.history_empty.setVisible(not has_history)
        self.history_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = [
                str(row.get("finished_at") or row.get("started_at") or ""),
                str(row.get("platform") or ""),
                str(row.get("collection_mode") or ""),
                str(row.get("source_value") or ""),
                str(row.get("target_count") or 0),
                str(row.get("success_count") or 0),
                str(row.get("duplicate_count") or 0),
                labels.get(str(row.get("status") or ""), str(row.get("status") or "")),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, str(row.get("task_id") or ""))
                self.history_table.setItem(row_index, column, item)
        self.history_table.resizeColumnsToContents()
        self._history_selection_changed()

    def _selected_history_ids(self) -> list[str]:
        ids: list[str] = []
        for index in self.history_table.selectionModel().selectedRows():
            item = self.history_table.item(index.row(), 0)
            task_id = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
            if task_id and task_id not in ids:
                ids.append(task_id)
        return ids

    def _active_history_ids(self) -> set[str]:
        ids: set[str] = set()
        if self._task_active and self.worker:
            ids.add(self.worker.engine.config.task_id)
        if self._task_active and self.queue_runner and self.queue_runner.current_task:
            ids.add(self.queue_runner.current_task.task_id)
        return ids

    def delete_selected_history(self) -> None:
        task_ids = self._selected_history_ids()
        if not task_ids:
            QMessageBox.information(self, "任务历史", "请先选择要删除的历史记录。")
            return
        active_ids = self._active_history_ids()
        rows = [self.database.get_history(task_id) for task_id in task_ids]
        if any(
            row and (str(row.get("status") or "") == "running" or task_id in active_ids)
            for task_id, row in zip(task_ids, rows)
        ):
            QMessageBox.warning(self, "无法删除历史", "运行中的任务不能删除历史记录，请先停止任务。")
            return
        self.database.delete_task_history(task_ids)
        self._refresh_history()

    def clear_all_history(self) -> None:
        rows = self.database.list_history()
        if not rows:
            self._refresh_history()
            return
        active_ids = self._active_history_ids()
        if any(
            str(row.get("status") or "") == "running" or str(row.get("task_id") or "") in active_ids
            for row in rows
        ):
            QMessageBox.warning(self, "无法清空历史", "运行中的任务不能删除历史记录，请先停止任务。")
            return
        dialog = QMessageBox(self)
        dialog.setWindowTitle("清空全部历史")
        dialog.setText("确定清空全部任务历史吗？\n此操作只删除历史记录，不会删除已下载的图片、视频或任务文件夹。")
        cancel = dialog.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        confirm = dialog.addButton("确定清空", QMessageBox.ButtonRole.AcceptRole)
        dialog.exec()
        if dialog.clickedButton() is not confirm:
            return
        self.database.clear_task_history()
        self._refresh_history()

    def _selected_history_row(self) -> dict[str, object] | None:
        row_index = self.history_table.currentRow()
        if row_index < 0:
            return None
        item = self.history_table.item(row_index, 0)
        task_id = item.data(Qt.ItemDataRole.UserRole) if item else ""
        return self.database.get_history(str(task_id)) if task_id else None

    def _history_config(self, row: dict[str, object] | None) -> TaskConfig | None:
        if not row:
            return None
        return self.database.load_task_config(str(row.get("task_id") or ""))

    def open_history_directory(self) -> None:
        row = self._selected_history_row()
        target = Path(str(row.get("task_output_dir") or "")) if row else None
        if target and target.exists():
            os.startfile(str(target))

    def _start_history_task(self, retry_only: bool) -> None:
        row = self._selected_history_row()
        config = self._history_config(row)
        if not row or not config:
            QMessageBox.information(self, "任务历史", "请先选择一条任务历史记录。")
            return
        if retry_only and str(row.get("status") or "") not in {"failed", "stopped", "interrupted"}:
            QMessageBox.information(self, "任务历史", "只有失败、停止或中断的任务可以重试。")
            return
        config = replace(config, task_id=uuid4().hex, status="pending")
        self._start_config(config)

    def recollect_selected(self) -> None:
        self._start_history_task(False)

    def retry_selected(self) -> None:
        self._start_history_task(True)

    def _history_selection_changed(self) -> None:
        row = self._selected_history_row()
        task_ids = self._selected_history_ids()
        can_use_history = not self._task_active
        self.delete_history.setEnabled(bool(task_ids))
        self.open_history_dir.setEnabled(can_use_history and row is not None)
        self.recollect_history.setEnabled(can_use_history and row is not None)
        self.retry_history.setEnabled(can_use_history and bool(
            row and str(row.get("status") or "") in {"failed", "stopped", "interrupted"}
        ))
        self.clear_history.setEnabled(bool(self.database.list_history()))

    def choose_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择图片保存目录", self.output.text())
        if path:
            self.output.setText(path)

    def start_task(self) -> None:
        try:
            config = self._task_config_from_form()
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return
        self._start_config(config)

    def _start_config(self, config: TaskConfig) -> None:
        try:
            if self.queue_runner and self.queue_runner.isRunning():
                QMessageBox.warning(self, "无法开始", "任务队列正在运行，请先停止队列。")
                return
            spec = get_platform_spec(config.canonical_platform())
            if spec is None or not spec.enabled:
                QMessageBox.warning(self, "无法开始", "该平台正在开发中，当前版本暂不支持实际采集。")
                return
            config.validate()
            profile = profile_directory_for_platform(config.canonical_platform())
            try:
                self.browser_manager.set_profile_dir(profile)
            except RuntimeError as exc:
                QMessageBox.warning(self, "无法切换浏览器 Profile", str(exc))
                return
            if config.canonical_platform() == "instagram" and self.browser_manager.is_login_running():
                QMessageBox.warning(
                    self,
                    "无法开始",
                    "Instagram 登录浏览器仍在运行。请完成登录并关闭该浏览器后，再开始采集。",
                )
                return
            if config.canonical_collection_mode() == "current_page" and not self.browser_manager.is_running():
                QMessageBox.warning(self, "无法开始", "请先点击『打开浏览器 / 登录』，在 Pinterest 中打开需要采集的页面，然后开始下载。")
                return
            if not self.browser_manager.is_running() and BrowserSession.profile_is_locked(profile):
                QMessageBox.warning(self, "无法开始", "请先关闭登录浏览器，再开始采集。")
                return
            config.output_root.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return
        self.worker = Worker(config, profile, self.browser_manager)
        self.worker.log.connect(self.append_log)
        self.worker.stats.connect(self.update_stats)
        self.worker.task_started.connect(self.task_started)
        self.worker.control_status.connect(self._control_status_changed)
        self.worker.done.connect(self.task_done)
        self.worker.failed.connect(self.task_failed)
        self._set_running(True)
        self.append_log(f"开始任务：{config.canonical_platform()} / {config.canonical_source_value}")
        self.worker.start()

    def open_login_browser(self) -> None:
        if self.login_worker and self.login_worker.isRunning():
            return
        target_url = {
            "pinterest": "https://www.pinterest.com/",
            "x": "https://x.com/i/flow/login",
            "instagram": "https://www.instagram.com/",
        }.get(str(self.platform.currentData()), "https://www.pinterest.com/")
        platform = str(self.platform.currentData() or "")
        try:
            self.browser_manager.set_profile_dir(profile_directory_for_platform(platform))
        except RuntimeError as exc:
            QMessageBox.warning(self, "无法打开登录浏览器", str(exc))
            return
        self.login_worker = ChromeLoginWorker(self.browser_manager, target_url, platform)
        self.login_worker.log.connect(self.append_log)
        self.login_worker.failed.connect(lambda message: self.append_log(f"登录浏览器异常：{message}"))
        self.login_worker.finished.connect(lambda: self.login_browser.setEnabled(True))
        self.login_browser.setEnabled(False)
        self.append_log("正在打开采集器专用 Chrome（仅人工登录）……")
        self.login_worker.start()

    def pause_task(self) -> None:
        if not self._is_task_running() or self._control_state != "running":
            return
        self._set_control_state("pausing", "正在暂停...", "已请求暂停任务")
        if self.queue_runner and self.queue_runner.isRunning():
            self.queue_runner.pause()
        elif self.worker and self.worker.isRunning():
            self.worker.engine.pause()

    def resume_task(self) -> None:
        if not self._is_task_running() or self._control_state != "paused":
            return
        self._set_control_state("resuming", "正在继续...", "已请求继续任务")
        if self.queue_runner and self.queue_runner.isRunning():
            self.queue_runner.resume()
        elif self.worker and self.worker.isRunning():
            self.worker.engine.resume()

    def stop_task(self) -> None:
        if not self._is_task_running() or self._control_state == "stopping":
            return
        self._set_control_state("stopping", "正在停止...", "正在停止任务...")
        if self.queue_runner and self.queue_runner.isRunning():
            self.queue_runner.stop()
        elif self.worker and self.worker.isRunning():
            self.worker.engine.stop()

    def _is_task_running(self) -> bool:
        return self._task_active

    def _set_control_state(self, state: str, status: str | None = None, log: str | None = None) -> None:
        self._control_state = state
        if status:
            self.status.setText(status)
        if log:
            self.append_log(log)
        self._refresh_control_buttons()

    def _control_status_changed(self, status: str) -> None:
        if status == "paused":
            self._set_control_state("paused", "已暂停", "任务已暂停")
        elif status == "running":
            self._set_control_state("running", "运行中", "已继续任务")

    def update_stats(self, stats: TaskStats) -> None:
        if self._control_state not in {"pausing", "paused", "resuming", "stopping"}:
            self.status.setText(stats.current_status)
        self.found_block.set_value(stats.discovered)
        self.success_block.set_value(stats.downloaded)
        self.skipped_block.set_value(stats.skipped)
        self.fail_block.set_value(stats.failed)
        maximum = max(1, self.count.value())
        self.progress.setMaximum(maximum)
        self.progress.setValue(min(stats.downloaded, maximum))
        self.progress_percent.setText(f"{int((min(stats.downloaded, maximum) / maximum) * 100)}%")

    def append_log(self, message: str) -> None:
        escaped = html.escape(message)
        color = "#344054"
        if "失败" in message or "异常" in message:
            color = "#C2413B"
        elif "跳过" in message:
            color = "#B7791F"
        elif "已保存" in message or "完成" in message:
            color = "#25815B"
        self.log.append(f'<span style="color:{color};">{escaped}</span>')
        self.log.moveCursor(QTextCursor.MoveOperation.End)

    def task_done(self, directory: str) -> None:
        was_stopping = self._control_state == "stopping"
        if was_stopping:
            self.status.setText("已停止")
        self.task_started(directory)
        self._refresh_history()
        self.open_dir.setProperty("attention", True)
        self._refresh_style(self.open_dir)
        self.append_log("任务已停止" if was_stopping else f"任务结束：{directory}")
        self._set_running(False)

    def task_started(self, directory: str) -> None:
        self.last_task_dir = Path(directory)
        self.current_task.setText(f"当前任务文件夹：{directory}")
        self.open_dir.setProperty("attention", False)
        self._refresh_style(self.open_dir)

    def task_failed(self, message: str) -> None:
        self.append_log(f"任务异常：{message}")
        self._refresh_history()
        QMessageBox.critical(self, "任务异常", message)
        self._set_running(False)

    def open_output(self) -> None:
        target = self.last_task_dir or Path(self.output.text())
        if target.exists():
            os.startfile(str(target))

    def _set_running(self, running: bool) -> None:
        self._control_state = "running" if running else "idle"
        self._task_active = running
        if running:
            self.status.setText("运行中")
        self.start.setEnabled(not running)
        self.platform.setEnabled(not running)
        self.mode.setEnabled(not running)
        self.media_type.setEnabled(not running)
        self.batch_mode.setEnabled(not running)
        self.only_prompt.setEnabled(not running)
        self.skip_history_duplicates.setEnabled(not running)
        self.start_queue.setEnabled(not running)
        self.clear_completed.setEnabled(not running)
        self.refresh_history.setEnabled(not running)
        if running:
            self.keyword.setEnabled(False)
            self.only_author.setEnabled(False)
        else:
            self._platform_changed(self.platform.currentIndex())
            self.only_author.setEnabled(True)
            self._mode_changed(self.mode.currentIndex())
        self._update_batch_controls(running)
        self._refresh_control_buttons()
        self._history_selection_changed()
        spec = get_platform_spec(str(self.platform.currentData() or ""))
        self.login_browser.setEnabled(
            not running and bool(spec and spec.enabled)
            and not (self.login_worker and self.login_worker.isRunning())
        )
        if running:
            self.open_dir.setProperty("attention", False)
            self._refresh_style(self.open_dir)

    def _refresh_control_buttons(self) -> None:
        running = self._is_task_running()
        self.pause.setEnabled(running and self._control_state == "running")
        self.resume.setEnabled(running and self._control_state == "paused")
        self.stop.setEnabled(running and self._control_state != "stopping")

    @staticmethod
    def _refresh_style(widget: QWidget) -> None:
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(self, "任务正在运行", "是否停止任务并退出？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.engine.stop()
            self.worker.wait(5000)
        if self.queue_runner and self.queue_runner.isRunning():
            answer = QMessageBox.question(self, "任务队列正在运行", "是否停止队列并退出？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.queue_runner.stop()
            self.queue_runner.wait(5000)
        if self.login_worker and self.login_worker.isRunning():
            self.login_worker.requestInterruption()
            self.login_worker.wait(5000)
        self.browser_manager.close()
        event.accept()


def run() -> None:
    _install_runtime_exception_hooks()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    set_windows_app_user_model_id()
    app = RuntimeApplication(sys.argv)
    app.setWindowIcon(QIcon(app_icon_path()))
    app.setStyleSheet(stylesheet())
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window.keyword.setFocus)
    sys.exit(app.exec())
