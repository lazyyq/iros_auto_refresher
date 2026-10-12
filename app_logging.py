"""A single logging pipeline shared by GUI and rotating UTF-8 files."""
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
import threading

from PyQt5.QtCore import QObject, pyqtSignal


class LogBridge(QObject):
    message = pyqtSignal(str)


class SecretFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self._values = set()
        self._lock = threading.Lock()

    def add(self, value: str) -> None:
        if value:
            with self._lock:
                self._values.add(value)

    def redact(self, text: str) -> str:
        with self._lock:
            for value in sorted(self._values, key=len, reverse=True):
                text = text.replace(value, "[SESSION REDACTED]")
        return text


class SafeFormatter(logging.Formatter):
    def __init__(self, secrets: SecretFilter):
        super().__init__("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
        self.secrets = secrets

    def format(self, record):
        return self.secrets.redact(super().format(record))


class GuiHandler(logging.Handler):
    def __init__(self, bridge: LogBridge):
        super().__init__()
        self.bridge = bridge

    def emit(self, record):
        self.bridge.message.emit(self.format(record))


class FileHandler(TimedRotatingFileHandler):
    def __init__(self, path: Path, bridge: LogBridge):
        self.bridge = bridge
        self.reported = False
        super().__init__(path, when="midnight", backupCount=14, encoding="utf-8")

    def handleError(self, record):
        if not self.reported:
            self.reported = True
            self.bridge.message.emit("ERROR 로그 파일 기록 실패: 경로와 쓰기 권한을 확인하세요.")


def configure_logging(root: Path, bridge: LogBridge) -> tuple[logging.Logger, SecretFilter]:
    logger = logging.getLogger("iros")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    secrets = SecretFilter()
    formatter = SafeFormatter(secrets)
    gui = GuiHandler(bridge)
    gui.setFormatter(formatter)
    logger.addHandler(gui)
    try:
        path = root / "logs" / "iros_auto_refresher.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        disk = FileHandler(path, bridge)
        disk.setFormatter(formatter)
        logger.addHandler(disk)
    except OSError:
        logger.error("로그 파일을 열 수 없습니다. logs 폴더의 쓰기 권한을 확인하세요.")
    return logger, secrets
