from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QCloseEvent, QTextCursor
from PySide6.QtWidgets import (QApplication, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QProgressBar, QSpinBox, QTextEdit, QVBoxLayout, QWidget)

from src.downloader import DownloadEngine
from src.models import TaskConfig, TaskStats


class Worker(QThread):
    log = Signal(str)
    stats = Signal(object)
    done = Signal(str)
    failed = Signal(str)
    def __init__(self, config: TaskConfig, profile_dir: Path) -> None:
        super().__init__(); self.engine = DownloadEngine(config, profile_dir, self.log.emit, self.stats.emit)
    def run(self) -> None:
        try: self.done.emit(str(self.engine.run()))
        except Exception as exc: self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__(); self.setWindowTitle("Pinterest Local Downloader"); self.resize(820, 650)
        self.worker: Worker | None = None; self.last_task_dir: Path | None = None
        root = QWidget(); layout = QVBoxLayout(root); form = QFormLayout()
        self.keyword = QLineEdit(); self.keyword.setPlaceholderText("例如：东方古风美女")
        self.count = QSpinBox(); self.count.setRange(1, 10000); self.count.setValue(100)
        self.output = QLineEdit(str(Path.home() / "Pinterest素材")); choose = QPushButton("选择目录"); choose.clicked.connect(self.choose_dir)
        output_row = QHBoxLayout(); output_row.addWidget(self.output); output_row.addWidget(choose)
        self.min_width = QSpinBox(); self.min_width.setRange(1, 10000); self.min_width.setValue(800)
        self.min_height = QSpinBox(); self.min_height.setRange(1, 10000); self.min_height.setValue(800)
        self.interval = QDoubleSpinBox(); self.interval.setRange(0, 60); self.interval.setValue(1); self.interval.setSuffix(" 秒")
        form.addRow("搜索关键词", self.keyword); form.addRow("下载数量", self.count); form.addRow("保存目录", output_row); form.addRow("最低宽度", self.min_width); form.addRow("最低高度", self.min_height); form.addRow("下载间隔", self.interval)
        box = QGroupBox("任务设置"); box.setLayout(form); layout.addWidget(box)
        buttons = QHBoxLayout(); self.start = QPushButton("开始下载"); self.pause = QPushButton("暂停"); self.resume = QPushButton("继续"); self.stop = QPushButton("停止"); self.open_dir = QPushButton("打开保存目录")
        for button in (self.start, self.pause, self.resume, self.stop, self.open_dir): buttons.addWidget(button)
        self.start.clicked.connect(self.start_task); self.pause.clicked.connect(self.pause_task); self.resume.clicked.connect(self.resume_task); self.stop.clicked.connect(self.stop_task); self.open_dir.clicked.connect(self.open_output)
        layout.addLayout(buttons); self.progress = QProgressBar(); layout.addWidget(self.progress)
        stats_layout = QGridLayout(); self.status = QLabel("等待开始"); self.found = QLabel("已发现：0"); self.success = QLabel("成功：0"); self.skipped = QLabel("跳过：0"); self.fail = QLabel("失败：0")
        for i, widget in enumerate((self.status, self.found, self.success, self.skipped, self.fail)): stats_layout.addWidget(widget, 0, i)
        layout.addLayout(stats_layout); self.log = QTextEdit(); self.log.setReadOnly(True); layout.addWidget(self.log, 1); self.setCentralWidget(root); self._set_running(False)
    def choose_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择图片保存目录", self.output.text())
        if path: self.output.setText(path)
    def start_task(self) -> None:
        try:
            config = TaskConfig(self.keyword.text(), self.count.value(), Path(self.output.text()), self.min_width.value(), self.min_height.value(), self.interval.value()); config.validate(); config.output_root.mkdir(parents=True, exist_ok=True)
        except (ValueError, OSError) as exc: QMessageBox.warning(self, "无法开始", str(exc)); return
        profile = Path(__file__).resolve().parents[2] / "browser-data"
        self.worker = Worker(config, profile); self.worker.log.connect(self.append_log); self.worker.stats.connect(self.update_stats); self.worker.done.connect(self.task_done); self.worker.failed.connect(self.task_failed); self._set_running(True); self.worker.start()
    def pause_task(self) -> None:
        if self.worker: self.worker.engine.pause(); self.append_log("任务已暂停。"); self.status.setText("已暂停")
    def resume_task(self) -> None:
        if self.worker: self.worker.engine.resume(); self.append_log("任务继续执行。")
    def stop_task(self) -> None:
        if self.worker: self.worker.engine.stop(); self.append_log("正在安全停止任务……")
    def update_stats(self, stats: TaskStats) -> None:
        self.status.setText(stats.current_status); self.found.setText(f"已发现：{stats.discovered}"); self.success.setText(f"成功：{stats.downloaded}"); self.skipped.setText(f"跳过：{stats.skipped}"); self.fail.setText(f"失败：{stats.failed}")
        self.progress.setMaximum(max(1, self.count.value())); self.progress.setValue(min(stats.downloaded, self.count.value()))
    def append_log(self, message: str) -> None:
        self.log.append(message); self.log.moveCursor(QTextCursor.MoveOperation.End)
    def task_done(self, directory: str) -> None:
        self.last_task_dir = Path(directory); self.append_log(f"任务结束：{directory}"); self._set_running(False)
    def task_failed(self, message: str) -> None:
        self.append_log(f"任务异常：{message}"); QMessageBox.critical(self, "任务异常", message); self._set_running(False)
    def open_output(self) -> None:
        target = self.last_task_dir or Path(self.output.text())
        if target.exists(): __import__("os").startfile(str(target))
    def _set_running(self, running: bool) -> None:
        self.start.setEnabled(not running); self.pause.setEnabled(running); self.resume.setEnabled(running); self.stop.setEnabled(running)
    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker and self.worker.isRunning():
            answer = QMessageBox.question(self, "任务正在运行", "是否停止任务并退出？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes: event.ignore(); return
            self.worker.engine.stop(); self.worker.wait(5000)
        event.accept()

def run() -> None:
    app = QApplication(sys.argv); window = MainWindow(); window.show(); sys.exit(app.exec())
