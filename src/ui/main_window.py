from __future__ import annotations

import html
import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QProgressBar, QScrollArea, QSpinBox, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

from src.downloader import DownloadEngine
from src.browser.session import BrowserSession, find_chrome_path, regular_chrome_login_command
from src.models import TaskConfig, TaskStats
from src.ui.resources import app_icon_path, resource_path, stylesheet
from src.utils.paths import browser_profile_directory


class Worker(QThread):
    log = Signal(str)
    stats = Signal(object)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, config: TaskConfig, profile_dir: Path) -> None:
        super().__init__()
        self.engine = DownloadEngine(config, profile_dir, self.log.emit, self.stats.emit)

    def run(self) -> None:
        try:
            self.done.emit(str(self.engine.run()))
        except Exception as exc:
            self.failed.emit(str(exc))


class ChromeLoginWorker(QThread):
    """Keep a normal system Chrome process open for manual login only."""

    log = Signal(str)
    failed = Signal(str)

    def __init__(self, profile_dir: Path, target_url: str) -> None:
        super().__init__()
        self.profile_dir = profile_dir
        self.target_url = target_url
        self.process: subprocess.Popen[bytes] | None = None

    def run(self) -> None:
        try:
            chrome_path = find_chrome_path()
            if not chrome_path:
                raise RuntimeError("未找到本机 Google Chrome，无法打开普通登录浏览器。")
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            if BrowserSession.profile_is_locked(self.profile_dir):
                raise RuntimeError("请先关闭登录浏览器，再开始采集。")
            command = regular_chrome_login_command(chrome_path, self.profile_dir, self.target_url)
            self.process = subprocess.Popen(command, cwd=str(Path(chrome_path).parent))
            self.log.emit(
                f"普通 Chrome 已打开：{chrome_path}；专用 Profile：{self.profile_dir}；"
                "请人工登录 Pinterest / X，完成后直接关闭浏览器窗口。"
            )
            started_at = time.monotonic()
            while not self.isInterruptionRequested():
                process_exited = self.process.poll() is not None
                profile_locked = BrowserSession.profile_is_locked(self.profile_dir)
                if process_exited and not profile_locked and time.monotonic() - started_at > 2:
                    break
                time.sleep(0.5)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.process = None


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
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("社媒素材采集器")
        self.setWindowIcon(QIcon(app_icon_path()))
        self.resize(1080, 800)
        self.setMinimumSize(920, 700)
        self.worker: Worker | None = None
        self.login_worker: ChromeLoginWorker | None = None
        self.last_task_dir: Path | None = None

        self._create_controls()
        self._build_window()
        self._wire_actions()
        self._set_running(False)

    def _create_controls(self) -> None:
        self.platform = QComboBox()
        self.platform.addItem("Pinterest", "pinterest")
        self.platform.addItem("X（Twitter）", "x")
        self.platform.setMinimumHeight(36)
        self.mode = QComboBox()
        self.mode.setMinimumHeight(36)
        self.media_type = QComboBox()
        self.media_type.addItem("仅图片", "image")
        self.media_type.addItem("仅视频", "video")
        self.media_type.addItem("图片 + 视频", "mixed")
        self.media_type.setMinimumHeight(36)
        self.media_type_label = self._field_label("下载内容类型")
        self.keyword = QLineEdit()
        self.keyword.setMinimumHeight(42)
        self.source_label = self._field_label("搜索关键词")
        self.mode_hint = QLabel()
        self.mode_hint.setObjectName("cardSubtitle")
        self.mode_hint.setWordWrap(True)
        self.only_author = QCheckBox("仅采集作者本人内容")
        self.only_author.setChecked(True)

        self.count = self._spin_box(1, 10000, 100)
        self.min_width = self._spin_box(1, 10000, 800)
        self.min_height = self._spin_box(1, 10000, 800)
        self.interval = QDoubleSpinBox()
        self.interval.setRange(0, 60)
        self.interval.setValue(1)
        self.interval.setSingleStep(0.5)
        self.interval.setSuffix(" 秒")
        self.interval.setMinimumHeight(36)

        self.output = QLineEdit(str(Path.home() / "Pinterest素材"))
        self.output.setMinimumHeight(38)
        self.choose = QPushButton("选择目录")
        self.choose.setIcon(QIcon(resource_path("icons/folder.svg")))

        self.start = QPushButton("开始下载")
        self.start.setObjectName("primaryButton")
        self.start.setMinimumHeight(44)
        self.pause = QPushButton("暂停")
        self.resume = QPushButton("继续")
        self.stop = QPushButton("停止")
        self.login_browser = QPushButton("打开浏览器 / 登录")
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
        self._platform_changed(0)

    @staticmethod
    def _spin_box(minimum: int, maximum: int, value: int) -> QSpinBox:
        control = QSpinBox()
        control.setRange(minimum, maximum)
        control.setValue(value)
        control.setMinimumHeight(36)
        return control

    def _build_window(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container = QWidget()
        container.setObjectName("page")
        layout = QVBoxLayout(container)
        layout.setContentsMargins(34, 28, 34, 24)
        layout.setSpacing(18)
        layout.addLayout(self._build_header())
        layout.addWidget(self._build_settings_card())
        layout.addWidget(self._build_actions_card())
        layout.addWidget(self._build_progress_card())
        layout.addWidget(self._build_log_card(), 1)
        layout.addLayout(self._build_footer())
        scroll.setWidget(container)
        self.setCentralWidget(scroll)

    def _build_header(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(4)
        title = QLabel("社媒素材采集器")
        title.setObjectName("pageTitle")
        subtitle = QLabel("批量采集并保存高质量社交媒体图片素材到本地")
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
        layout.addWidget(self.media_type_label)
        layout.addWidget(self.media_type)
        layout.addWidget(self.source_label)
        layout.addWidget(self.keyword)
        layout.addWidget(self.mode_hint)
        layout.addWidget(self.only_author)
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
        self.platform.currentIndexChanged.connect(self._platform_changed)
        self.mode.currentIndexChanged.connect(self._mode_changed)

    def _platform_changed(self, _index: int) -> None:
        platform = self.platform.currentData()
        self.mode.blockSignals(True)
        self.mode.clear()
        if platform == "x":
            self.mode.addItem("作者主页", "author")
            self.mode.addItem("首页推荐", "home")
        else:
            self.mode.addItem("关键词搜索", "search")
            self.mode.addItem("首页推荐", "home")
            self.mode.addItem("当前页面", "current_page")
        self.mode.blockSignals(False)
        self._mode_changed(self.mode.currentIndex())

    def _mode_changed(self, _index: int) -> None:
        platform = self.platform.currentData()
        mode = self.mode.currentData()
        if platform == "x":
            author_mode = mode == "author"
            self.source_label.setText("作者用户名 / 主页地址")
            self.source_label.setVisible(author_mode)
            self.keyword.setVisible(author_mode)
            self.keyword.setEnabled(author_mode)
            self.keyword.setPlaceholderText("@username 或 https://x.com/username")
            self.mode_hint.setText("从该 X 作者主页向下采集图片内容。" if author_mode else "从 X 首页‘为你推荐’内容流向下采集。")
            self.only_author.setVisible(author_mode)
            self.only_author.setEnabled(author_mode)
            self.media_type_label.setVisible(True)
            self.media_type.setVisible(True)
            return
        self.source_label.setText("搜索关键词")
        self.source_label.setVisible(True)
        self.keyword.setVisible(True)
        search = mode == "search"
        self.keyword.setEnabled(search)
        self.keyword.setPlaceholderText("输入 Pinterest 搜索关键词" if search else "此模式不需要关键词")
        self.mode_hint.setText({
            "search": "根据关键词打开 Pinterest 搜索结果并从顶部开始采集。",
            "home": "从 Pinterest 首页推荐流开始采集，不需要关键词。",
            "current_page": "请先在 Pinterest 浏览器中打开目标页面，再点击开始下载；程序不会自动切换页面。",
        }.get(mode, ""))
        self.only_author.setVisible(False)
        self.media_type_label.setVisible(False)
        self.media_type.setVisible(False)

    def choose_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择图片保存目录", self.output.text())
        if path:
            self.output.setText(path)

    def start_task(self) -> None:
        try:
            platform = str(self.platform.currentData())
            mode = str(self.mode.currentData())
            config = TaskConfig(
                keyword=self.keyword.text() if platform == "pinterest" else "",
                target_count=self.count.value(),
                output_root=Path(self.output.text()),
                min_width=self.min_width.value(),
                min_height=self.min_height.value(),
                interval_seconds=self.interval.value(),
                collection_mode=mode,
                platform=platform,
                source_identifier=self.keyword.text() if platform == "x" and mode == "author" else "",
                only_author_content=self.only_author.isChecked() if mode == "author" else False,
                media_type=str(self.media_type.currentData()) if platform == "x" else "image",
            )
            config.validate()
            profile = browser_profile_directory()
            if BrowserSession.profile_is_locked(profile):
                QMessageBox.warning(self, "无法开始", "请先关闭登录浏览器，再开始采集。")
                return
            config.output_root.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "无法开始", str(exc))
            return
        self.worker = Worker(config, profile)
        self.worker.log.connect(self.append_log)
        self.worker.stats.connect(self.update_stats)
        self.worker.done.connect(self.task_done)
        self.worker.failed.connect(self.task_failed)
        self._set_running(True)
        self.worker.start()

    def open_login_browser(self) -> None:
        if self.login_worker and self.login_worker.isRunning():
            return
        profile = browser_profile_directory()
        if BrowserSession.profile_is_locked(profile):
            QMessageBox.warning(self, "无法打开登录浏览器", "请先关闭当前占用专用 Profile 的浏览器，再重试。")
            return
        self.login_worker = ChromeLoginWorker(profile, "https://x.com/i/flow/login")
        self.login_worker.log.connect(self.append_log)
        self.login_worker.failed.connect(lambda message: self.append_log(f"登录浏览器异常：{message}"))
        self.login_worker.finished.connect(lambda: self.login_browser.setEnabled(True))
        self.login_browser.setEnabled(False)
        self.append_log("正在打开采集器专用浏览器……")
        self.login_worker.start()

    def pause_task(self) -> None:
        if self.worker:
            self.worker.engine.pause()
            self.append_log("任务已暂停。")
            self.status.setText("任务已暂停")

    def resume_task(self) -> None:
        if self.worker:
            self.worker.engine.resume()
            self.append_log("任务继续执行。")

    def stop_task(self) -> None:
        if self.worker:
            self.worker.engine.stop()
            self.append_log("正在安全停止任务……")

    def update_stats(self, stats: TaskStats) -> None:
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
        self.last_task_dir = Path(directory)
        self.current_task.setText(f"当前任务文件夹：{directory}")
        self.open_dir.setProperty("attention", True)
        self._refresh_style(self.open_dir)
        self.append_log(f"任务结束：{directory}")
        self._set_running(False)

    def task_failed(self, message: str) -> None:
        self.append_log(f"任务异常：{message}")
        QMessageBox.critical(self, "任务异常", message)
        self._set_running(False)

    def open_output(self) -> None:
        target = self.last_task_dir or Path(self.output.text())
        if target.exists():
            os.startfile(str(target))

    def _set_running(self, running: bool) -> None:
        self.start.setEnabled(not running)
        self.platform.setEnabled(not running)
        self.mode.setEnabled(not running)
        self.media_type.setEnabled(not running)
        if running:
            self.keyword.setEnabled(False)
            self.only_author.setEnabled(False)
        else:
            self._platform_changed(self.platform.currentIndex())
            self.only_author.setEnabled(True)
            self._mode_changed(self.mode.currentIndex())
        self.pause.setEnabled(running)
        self.resume.setEnabled(running)
        self.stop.setEnabled(running)
        self.login_browser.setEnabled(not running and not (self.login_worker and self.login_worker.isRunning()))
        if running:
            self.open_dir.setProperty("attention", False)
            self._refresh_style(self.open_dir)

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
        if self.login_worker and self.login_worker.isRunning():
            self.login_worker.requestInterruption()
            self.login_worker.wait(5000)
        event.accept()


def run() -> None:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps)
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(app_icon_path()))
    app.setStyleSheet(stylesheet())
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window.keyword.setFocus)
    sys.exit(app.exec())
