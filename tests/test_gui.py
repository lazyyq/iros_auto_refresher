import logging
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from app_logging import LogBridge, configure_logging
from gui import MainWindow
from settings import ROOT, Settings, SettingsStore

APP = QApplication.instance() or QApplication([])


class FakeWorker(QObject):
    ready = pyqtSignal(bool)
    metadata = pyqtSignal(dict)
    outcome = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, settings, root, secrets, manual, parent):
        super().__init__(parent)
        import threading
        self.cancel = threading.Event()
        self.manual = manual
        self.stopped = False
        self.captured = False

    def start(self):
        pass

    def stop(self):
        self.cancel.set()
        self.stopped = True

    def save_login(self):
        self.captured = True

    def finish(self):
        self.outcome.emit("테스트 완료")
        self.finished.emit()


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-gui-", dir=ROOT)
        self.root = Path(self.temp.name)
        binary = self.root / "chrome.exe"
        binary.write_bytes(b"test")
        SettingsStore(self.root).save(Settings(chrome_exe=str(binary)))
        self.patch = patch("gui.BrowserWorker", FakeWorker)
        self.patch.start()
        self.window = MainWindow(self.root)

    def tearDown(self):
        self.window.stop()
        if self.window.worker:
            self.window.worker.finish()
        self.window.close()
        APP.processEvents()
        self.patch.stop()
        self.temp.cleanup()

    def test_start_immediate_and_reschedule_after_completion(self):
        self.assertFalse(self.window.automatic)
        self.assertIsNone(self.window.worker)
        self.window.start_auto()
        self.assertIsNotNone(self.window.worker)
        self.assertFalse(self.window.worker.manual)
        self.assertFalse(self.window.fields.isEnabled())
        self.window.worker.finish()
        self.assertGreater(self.window.next_due, time.monotonic() + 899)
        gui_log = self.window.log_view.toPlainText()
        file_log = (self.root / "logs/iros_auto_refresher.log").read_text(encoding="utf-8")
        for text in (gui_log, file_log):
            self.assertRegex(text, r"다음 자동 갱신 예정: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \(15분 후\)")
            self.assertEqual(text.count("다음 자동 갱신 예정:"), 1)
        self.window.tick()
        self.window.tick()
        self.assertEqual(self.window.log_view.toPlainText().count("다음 자동 갱신 예정:"), 1)
        self.window.next_due = time.monotonic() - 1
        self.window.tick()
        self.assertIsNotNone(self.window.worker)

    def test_manual_pause_resume_and_save_button(self):
        self.window.start_auto()
        self.window.worker.finish()
        self.window.start_manual()
        worker = self.window.worker
        self.assertTrue(worker.manual)
        self.assertIsNone(self.window.next_due)
        worker.ready.emit(True)
        self.assertTrue(self.window.capture_button.isEnabled())
        self.window.capture_manual()
        self.assertTrue(worker.captured)
        self.assertFalse(self.window.capture_button.isEnabled())
        worker.metadata.emit({"domain": ".iros.go.kr", "httpOnly": True})
        self.assertTrue(SettingsStore(self.root).load().cookie_metadata["httpOnly"])
        worker.finish()
        self.assertTrue(self.window.automatic)
        self.assertIsNotNone(self.window.next_due)
        self.assertEqual(self.window.log_view.toPlainText().count("다음 자동 갱신 예정:"), 2)

    def test_refresh_now_replaces_schedule_from_completion(self):
        with patch("gui.time.monotonic", return_value=1000) as clock:
            self.window.start_auto()
            self.window.worker.finish()
            self.assertEqual(self.window.next_due, 1900)
            clock.return_value = 1300
            self.assertTrue(self.window.refresh_button.isEnabled())
            self.window.refresh_button.click()
            worker = self.window.worker
            self.assertIsNotNone(worker)
            self.assertFalse(worker.manual)
            self.assertIsNone(self.window.next_due)
            self.assertFalse(self.window.refresh_button.isEnabled())
            # The old deadline must not launch another worker during this refresh.
            clock.return_value = 1950
            self.window.tick()
            self.assertIs(self.window.worker, worker)
            worker.finish()
            self.assertTrue(self.window.automatic)
            self.assertEqual(self.window.next_due, 2850)
            self.assertTrue(self.window.refresh_button.isEnabled())
            clock.return_value = 2849
            self.window.tick()
            self.assertIsNone(self.window.worker)
            clock.return_value = 2850
            self.window.tick()
            self.assertIsNotNone(self.window.worker)

    def test_refresh_now_without_auto_runs_once_with_current_settings(self):
        self.window.interval.setValue(7)
        self.window.session.setText("new-session.txt")
        self.window.refresh_button.click()
        self.assertIsNotNone(self.window.worker)
        self.assertFalse(self.window.worker.manual)
        self.assertFalse(self.window.automatic)
        settings = SettingsStore(self.root).load()
        self.assertEqual(settings.interval_minutes, 7)
        self.assertEqual(settings.session_file, "new-session.txt")
        self.window.worker.finish()
        self.assertIsNone(self.window.next_due)
        self.assertTrue(self.window.refresh_button.isEnabled())

    def test_refresh_now_rejects_invalid_settings(self):
        self.window.chrome.setText("")
        with patch("gui.QMessageBox.warning") as warning:
            self.window.refresh_button.click()
        warning.assert_called_once()
        self.assertIsNone(self.window.worker)
        self.assertFalse(self.window.automatic)
        self.assertIsNone(self.window.next_due)

    def test_refresh_now_no_overlap_during_manual_refresh_and_stop(self):
        for manual in (True, False):
            with self.subTest(manual=manual):
                if manual:
                    self.window.start_manual()
                else:
                    self.window.refresh_now()
                worker = self.window.worker
                self.assertFalse(self.window.refresh_button.isEnabled())
                self.window.refresh_now()
                self.assertIs(self.window.worker, worker)
                self.window.stop()
                self.assertFalse(self.window.refresh_button.isEnabled())
                self.window.refresh_now()
                self.assertIs(self.window.worker, worker)
                self.assertTrue(worker.stopped)
                worker.finish()
                self.assertIsNone(self.window.next_due)
                self.assertTrue(self.window.refresh_button.isEnabled())

    def test_stop_during_refresh_now_cancels_rescheduling(self):
        self.window.start_auto()
        self.window.worker.finish()
        self.window.refresh_now()
        worker = self.window.worker
        self.window.stop()
        self.assertTrue(worker.stopped)
        worker.finish()
        self.assertFalse(self.window.automatic)
        self.assertIsNone(self.window.next_due)

    def test_stop_cancel_and_no_overlap(self):
        self.window.start_auto()
        worker = self.window.worker
        self.window.start_manual()
        self.window.tick()
        self.assertIs(self.window.worker, worker)
        self.window.stop()
        self.assertTrue(worker.stopped)
        worker.finish()
        self.assertFalse(self.window.automatic)
        self.assertIsNone(self.window.next_due)
        self.assertTrue(self.window.fields.isEnabled())
        self.assertNotIn("다음 자동 갱신 예정:", self.window.log_view.toPlainText())

    def test_close_waits_for_worker(self):
        self.window.show()
        self.window.start_auto()
        worker = self.window.worker
        self.window.close()
        self.assertTrue(self.window.closing)
        self.assertTrue(self.window.isVisible())
        self.assertTrue(worker.stopped)
        self.assertFalse(self.window.refresh_button.isEnabled())
        self.window.refresh_now()
        self.assertIs(self.window.worker, worker)
        worker.finish()
        self.assertFalse(self.window.isVisible())
        self.window.refresh_now()
        self.assertIsNone(self.window.worker)


class LogFileTests(unittest.TestCase):
    def test_gui_file_utf8_append_rotation_and_redaction(self):
        with tempfile.TemporaryDirectory(prefix=".test-log-", dir=ROOT) as directory:
            root = Path(directory)
            bridge = LogBridge()
            messages = []
            bridge.message.connect(messages.append)
            logger, secrets = configure_logging(root, bridge)
            path = root / "logs/iros_auto_refresher.log"
            secrets.add("private-session")
            logger.info("사용자 홍길동 private-session")
            text = path.read_text(encoding="utf-8")
            self.assertIn("홍길동", text)
            self.assertNotIn("private-session", text)
            self.assertIn(text.strip(), messages)
            logger, _ = configure_logging(root, bridge)
            logger.info("재시작")
            self.assertIn("홍길동", path.read_text(encoding="utf-8"))
            disk = next(h for h in logger.handlers if hasattr(h, "doRollover"))
            self.assertEqual(disk.backupCount, 14)
            self.assertEqual(disk.when, "MIDNIGHT")
            disk.doRollover()
            logger.info("교체 후")
            self.assertNotIn("홍길동", path.read_text(encoding="utf-8"))
            self.assertTrue(list(path.parent.glob("*.log.*")))
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()


if __name__ == "__main__":
    unittest.main()
