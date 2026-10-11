"""GUI entrypoint for IROS Auto Refresher."""
import sys

from PyQt6.QtWidgets import QApplication
from gui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("IROS Auto Refresher")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
