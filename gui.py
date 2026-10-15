"""Qt controls and a monotonic scheduler; no Selenium calls on the GUI thread."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import logging
from pathlib import Path
import time

from PyQt5.QtCore import QTimer
from PyQt5.QtGui import QCloseEvent
from PyQt5.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from app_logging import LogBridge, configure_logging
from browser import BrowserWorker
from settings import ROOT, Settings, SettingsStore, resolve_path


class MainWindow(QMainWindow):
    def __init__(self, root: Path = ROOT):
        super().__init__()
        self.root = root
        self.store = SettingsStore(root)
        self.worker = None
        self.automatic = False
        self.next_due = None
        self.closing = False
        self.last_result = "대기"
        self.setWindowTitle("인터넷등기소 세션 자동 갱신")
        self.resize(920, 650)
        panel = QWidget()
        self.setCentralWidget(panel)
        layout = QVBoxLayout(panel)
        self.fields = QWidget()
        form = QFormLayout(self.fields)
        self.browser = QComboBox()
        self.browser.addItem("Chrome", "chrome")
        self.browser.addItem("Edge", "edge")
        form.addRow("브라우저", self.browser)
        self.chrome = self.path_row(form, "Chrome 실행 파일", "chrome.exe 파일 선택", False)
        self.edge = self.path_row(form, "Edge 실행 파일", "msedge.exe 파일 선택", False)
        self.interval = QSpinBox()
        self.interval.setRange(1, 10080)
        self.interval.setSuffix(" 분")
        form.addRow("자동 접속 주기", self.interval)
        self.session = self.path_row(form, "세션 출력 텍스트", "세션 출력 파일 선택", True)
        layout.addWidget(self.fields)
        buttons = QHBoxLayout()
        self.save_button = QPushButton("설정 저장")
        self.manual_button = QPushButton("수동 로그인")
        self.capture_button = QPushButton("로그인 완료·세션 저장")
        self.start_button = QPushButton("자동 갱신 시작")
        self.refresh_button = QPushButton("즉시 갱신")
        self.stop_button = QPushButton("중지")
        for button in (self.save_button, self.manual_button, self.capture_button,
                       self.start_button, self.refresh_button, self.stop_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status = QLabel("대기")
        self.next_label = QLabel("다음 실행: 없음")
        layout.addWidget(self.status)
        layout.addWidget(self.next_label)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(3000)
        layout.addWidget(self.log_view)
        self.bridge = LogBridge(self)
        self.bridge.message.connect(self.log_view.appendPlainText)
        self.log, self.secrets = configure_logging(root, self.bridge)
        try:
            self.settings = self.store.load()
        except (OSError, ValueError, TypeError):
            self.log.exception("설정 파일 읽기 실패; 기본값으로 시작합니다. 기존 파일은 유지됩니다.")
            self.settings = Settings()
        self.show_settings()
        self.save_button.clicked.connect(self.save_settings)
        self.manual_button.clicked.connect(self.start_manual)
        self.capture_button.clicked.connect(self.capture_manual)
        self.start_button.clicked.connect(self.start_auto)
        self.refresh_button.clicked.connect(self.refresh_now)
        self.stop_button.clicked.connect(self.stop)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.update_controls()
        self.log.info("프로그램 시작; 설정 복원 완료, 자동 갱신은 시작 버튼으로 실행합니다.")

    def path_row(self, form, label, title, save):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit()
        browse = QPushButton("찾아보기")
        layout.addWidget(edit)
        layout.addWidget(browse)
        def select():
            initial = str(resolve_path(edit.text(), self.root)) if edit.text() else str(self.root)
            if save:
                path, _ = QFileDialog.getSaveFileName(self, title, initial, "텍스트 파일 (*.txt);;모든 파일 (*)")
            else:
                path, _ = QFileDialog.getOpenFileName(self, title, initial, "실행 파일 (*.exe)")
            if path:
                edit.setText(path)
        browse.clicked.connect(select)
        form.addRow(label, row)
        return edit

    def show_settings(self):
        self.browser.setCurrentIndex(0 if self.settings.browser == "chrome" else 1)
        self.chrome.setText(self.settings.chrome_exe)
        self.edge.setText(self.settings.edge_exe)
        self.interval.setValue(self.settings.interval_minutes)
        self.session.setText(self.settings.session_file)

    def collect_settings(self, browser_required=True) -> bool:
        settings = Settings(browser=self.browser.currentData(), interval_minutes=self.interval.value(),
            chrome_exe=self.chrome.text().strip(), edge_exe=self.edge.text().strip(),
            session_file=self.session.text().strip(), cookie_metadata=deepcopy(self.settings.cookie_metadata))
        try:
            settings.validate(self.root, browser_required=browser_required)
            self.store.save(settings)
        except (OSError, ValueError, TypeError) as error:
            self.log.error("설정 저장·검증 실패: %s", error)
            QMessageBox.warning(self, "설정 확인", str(error))
            return False
        self.settings = settings
        return True

    def save_settings(self):
        if self.collect_settings(browser_required=False):
            self.log.info("설정 저장 완료")

    def update_controls(self):
        busy = self.worker is not None
        self.fields.setEnabled(not busy and not self.automatic and not self.closing)
        self.save_button.setEnabled(not busy and not self.automatic and not self.closing)
        self.start_button.setEnabled(not busy and not self.automatic and not self.closing)
        self.manual_button.setEnabled(not busy and not self.closing)
        self.refresh_button.setEnabled(not busy and not self.closing)
        self.stop_button.setEnabled((busy or self.automatic) and not self.closing)
        if not busy:
            self.capture_button.setEnabled(False)

    def start_auto(self):
        if self.worker is not None or self.automatic or not self.collect_settings():
            return
        self.automatic = True
        self.log.info("자동 갱신 예약 시작: %s분 주기", self.settings.interval_minutes)
        self.launch(False)

    def start_manual(self):
        if self.worker is not None:
            return
        if not self.automatic and not self.collect_settings():
            return
        if self.automatic:
            self.log.info("수동 로그인 동안 자동 갱신 예약 일시 중단")
        self.next_due = None
        self.launch(True)

    def refresh_now(self):
        if self.worker is not None or self.closing:
            return
        if not self.automatic and not self.collect_settings():
            return
        self.log.info("즉시 갱신 요청")
        self.launch(False)

    def launch(self, manual: bool):
        if self.worker is not None or self.closing:
            return
        self.next_due = None
        self.next_label.setText("다음 실행: 작업 완료 후 예약")
        self.status.setText("수동 로그인 준비·진행 중" if manual else "자동 갱신 진행 중")
        self.last_result = "작업 완료"
        worker = BrowserWorker(deepcopy(self.settings), self.root, self.secrets, manual, self)
        self.worker = worker
        worker.ready.connect(self.manual_ready)
        worker.metadata.connect(self.save_metadata)
        worker.outcome.connect(self.set_outcome)
        worker.finished.connect(self.worker_finished)
        self.update_controls()
        worker.start()

    def manual_ready(self, ready: bool):
        self.capture_button.setEnabled(ready and not self.closing and self.worker is not None
                                       and not self.worker.cancel.is_set())
        if ready:
            self.status.setText("브라우저에서 직접 로그인 후 완료 버튼을 누르세요.")

    def capture_manual(self):
        if self.worker is not None and self.worker.manual:
            self.capture_button.setEnabled(False)
            self.status.setText("사용자 이름 확인·세션 저장 중")
            self.worker.save_login()

    def save_metadata(self, metadata: dict):
        self.settings.cookie_metadata = metadata
        try:
            self.store.save(self.settings)
        except (OSError, ValueError):
            self.log.exception("쿠키 속성 설정 저장 실패 (세션 텍스트는 저장됨)")

    def set_outcome(self, result: str):
        self.last_result = result

    def worker_finished(self):
        worker = self.worker
        if worker is None:
            return
        manual = worker.manual
        self.worker = None
        worker.deleteLater()
        self.status.setText(self.last_result)
        if self.automatic and not self.closing:
            # Completion-based interval avoids burst retries after a long operation.
            self.next_due = time.monotonic() + self.settings.interval_minutes * 60
            if manual:
                self.log.info("수동 작업 종료; 자동 갱신 예약 재개")
            scheduled_at = datetime.now() + timedelta(seconds=self.next_due - time.monotonic())
            self.log.info("다음 자동 갱신 예정: %s (%s분 후)",
                          scheduled_at.strftime("%Y-%m-%d %H:%M:%S"), self.settings.interval_minutes)
        self.update_controls()
        self.tick()
        if self.closing:
            self.close()

    def tick(self):
        if self.next_due is None:
            if self.worker is None:
                self.next_label.setText("다음 실행: 없음")
            return
        remaining = max(0, self.next_due - time.monotonic())
        when = datetime.now() + timedelta(seconds=remaining)
        self.next_label.setText(f"다음 실행: {when:%Y-%m-%d %H:%M:%S} ({int(remaining)}초 후)")
        if remaining == 0 and self.automatic and self.worker is None:
            self.launch(False)

    def stop(self):
        self.automatic = False
        self.next_due = None
        if self.worker is not None:
            self.worker.stop()
            self.capture_button.setEnabled(False)
            self.status.setText("중지 요청됨; 진행 중 명령 완료 후 브라우저 정리")
        else:
            self.status.setText("중지")
        self.log.info("예약 취소·작업 중지 요청")
        self.update_controls()
        self.tick()

    def closeEvent(self, event: QCloseEvent):
        if self.worker is not None:
            event.ignore()
            self.closing = True
            self.stop()
            self.status.setText("종료 중; 브라우저 정리를 기다립니다.")
            return
        self.timer.stop()
        self.log.info("프로그램 종료")
        for handler in self.log.handlers[:]:
            self.log.removeHandler(handler)
            handler.close()
        event.accept()
