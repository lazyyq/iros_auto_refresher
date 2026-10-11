import io
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import zipfile

from app_logging import SecretFilter, SafeFormatter
from browser import BrowserWorker, capture_session, injected_cookie, refresh
from drivers import Cancelled, DriverManager, chrome_release, edge_latest
from settings import ROOT, COOKIE_NAME, Settings, SettingsStore, atomic_write, read_session, resolve_path


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)


class SettingsTests(TemporaryTest):
    def test_relative_paths_and_round_trip(self):
        store = SettingsStore(self.root)
        settings = Settings(interval_minutes=7, session_file="out/세션.txt")
        store.save(settings)
        self.assertEqual(store.load(), settings)
        self.assertEqual(resolve_path(settings.session_file, self.root), self.root / "out/세션.txt")

    def test_empty_missing_bom_and_bad_session(self):
        path = self.root / "session.txt"
        self.assertEqual(read_session(path), "")
        path.write_text(" \n", encoding="utf-8")
        self.assertEqual(read_session(path), "")
        path.write_text("\ufefftoken\n", encoding="utf-8")
        self.assertEqual(read_session(path), "token")
        path.write_text("two tokens", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_session(path)

    def test_atomic_replace_failure_preserves_old(self):
        path = self.root / "session.txt"
        path.write_text("old", encoding="utf-8")
        with patch("settings.os.replace", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                atomic_write(path, "new")
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_invalid_interval_and_protected_output(self):
        for value in (0, -1, True, "15", 10081):
            with self.assertRaises(ValueError):
                Settings(interval_minutes=value).validate(self.root, browser_required=False)
        for name in ("config.json", "main.py", "logs/iros_auto_refresher.log", "chromedriver/153/driver.json"):
            with self.assertRaises(ValueError):
                Settings(session_file=name).validate(self.root, browser_required=False)


class SessionTests(TemporaryTest):
    def setUp(self):
        super().setUp()
        self.driver = MagicMock()
        self.driver.current_url = "https://www.iros.go.kr/"
        self.driver.get_cookie.return_value = {"name": COOKIE_NAME, "value": "new-token", "path": "/",
            "domain": "www.iros.go.kr", "httpOnly": True, "secure": True}
        self.settings = Settings()
        self.cancel = threading.Event()
        self.secrets = SecretFilter()

    def test_inject_before_second_visit_and_export_updated_cookie(self):
        atomic_write(self.root / "session.txt", "old-token")
        calls = []
        self.driver.get.side_effect = lambda url: calls.append("get")
        self.driver.add_cookie.side_effect = lambda cookie: calls.append(cookie["value"])
        with patch("browser.username", return_value="홍길동"):
            metadata = refresh(self.driver, self.settings, self.root, self.cancel, self.secrets)
        self.assertEqual(calls, ["get", "old-token", "get"])
        self.assertEqual(read_session(self.root / "session.txt"), "new-token")
        self.assertNotIn("value", metadata)
        self.assertNotIn("new-token", self.secrets.redact("new-token old-token"))

    def test_missing_and_empty_session_skip_injection_save_anonymous(self):
        for exists in (False, True):
            self.driver.reset_mock()
            if exists:
                atomic_write(self.root / "session.txt", "")
            with patch("browser.username", return_value=""):
                refresh(self.driver, self.settings, self.root, self.cancel, self.secrets)
            self.driver.add_cookie.assert_not_called()
            self.assertEqual(self.driver.get.call_count, 1)
            self.assertEqual(read_session(self.root / "session.txt"), "new-token")
            (self.root / "session.txt").unlink()

    def test_no_cookie_preserves_old(self):
        atomic_write(self.root / "session.txt", "old")
        self.driver.get_cookie.return_value = None
        with patch("browser.username", return_value=""):
            result = capture_session(self.driver, self.settings, self.root, self.cancel, self.secrets)
        self.assertIsNone(result)
        self.assertEqual(read_session(self.root / "session.txt"), "old")

    def test_navigation_failure_preserves_old(self):
        atomic_write(self.root / "session.txt", "old")
        self.driver.get.side_effect = RuntimeError("network")
        with self.assertRaises(RuntimeError):
            refresh(self.driver, self.settings, self.root, self.cancel, self.secrets)
        self.assertEqual(read_session(self.root / "session.txt"), "old")

    def test_foreign_domain_is_rejected(self):
        self.driver.current_url = "https://evil.example/"
        with self.assertRaises(ValueError):
            capture_session(self.driver, self.settings, self.root, self.cancel, self.secrets)
        self.assertFalse((self.root / "session.txt").exists())

    def test_metadata_cannot_inject_foreign_domain_or_expiry(self):
        cookie = injected_cookie("token", {"domain": ".evil.example", "expiry": 1}, "www.iros.go.kr")
        self.assertNotIn("domain", cookie)
        self.assertNotIn("expiry", cookie)
        self.assertTrue(cookie["httpOnly"])
        cookie = injected_cookie("token", {"domain": ".iros.go.kr", "sameSite": "Lax"}, "www.iros.go.kr")
        self.assertEqual(cookie["domain"], ".iros.go.kr")

    def test_cancel_does_not_save(self):
        self.cancel.set()
        with self.assertRaises(Cancelled):
            capture_session(self.driver, self.settings, self.root, self.cancel, self.secrets)
        self.assertFalse((self.root / "session.txt").exists())


class DriverTests(TemporaryTest):
    version = "153.0.1234.10"

    def cache(self):
        path = self.root / "chromedriver/153/chromedriver.exe"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"old")
        return path

    def download_zip(self, url, path):
        with zipfile.ZipFile(path, "w") as zipped:
            zipped.writestr("chromedriver-win64/chromedriver.exe", b"new")
            zipped.writestr("../../do-not-extract.txt", b"irrelevant")

    def test_compatible_cache_no_network(self):
        path = self.cache()
        with patch("drivers.exe_version", return_value=self.version), \
             patch("drivers.driver_version", return_value="153.0.1234.99"), \
             patch("drivers.Downloader") as downloader:
            result = DriverManager(self.root).ensure("chrome", Path("chrome.exe"), threading.Event())
        self.assertEqual(result, path)
        downloader.assert_not_called()

    def test_same_major_build_update_downloads_and_flattens_zip(self):
        path = self.cache()
        with patch("drivers.exe_version", return_value=self.version), \
             patch("drivers.driver_version", side_effect=["153.0.1200.1", self.version]), \
             patch("drivers.chrome_release", return_value=(self.version, "https://storage.googleapis.com/test.zip", False)), \
             patch("drivers.Downloader.fetch", side_effect=self.download_zip):
            DriverManager(self.root).ensure("chrome", Path("chrome.exe"), threading.Event())
        self.assertEqual(path.read_bytes(), b"new")
        self.assertFalse((self.root / "do-not-extract.txt").exists())
        self.assertFalse(list(path.parent.glob(".download-*")))

    def test_download_failure_preserves_existing(self):
        path = self.cache()
        with patch("drivers.exe_version", return_value=self.version), \
             patch("drivers.driver_version", return_value="153.0.1200.1"), \
             patch("drivers.chrome_release", return_value=(self.version, "https://storage.googleapis.com/test.zip", False)), \
             patch("drivers.Downloader.fetch", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                DriverManager(self.root).ensure("chrome", Path("chrome.exe"), threading.Event())
        self.assertEqual(path.read_bytes(), b"old")

    def test_download_version_mismatch_preserves_existing(self):
        path = self.cache()
        with patch("drivers.exe_version", return_value=self.version), \
             patch("drivers.driver_version", return_value="153.0.1200.1"), \
             patch("drivers.chrome_release", return_value=(self.version, "https://storage.googleapis.com/test.zip", False)), \
             patch("drivers.Downloader.fetch", side_effect=self.download_zip):
            with self.assertRaises(ValueError):
                DriverManager(self.root).ensure("chrome", Path("chrome.exe"), threading.Event())
        self.assertEqual(path.read_bytes(), b"old")

    def test_chrome_build_then_milestone_fallback(self):
        downloader = MagicMock()
        release = {"version": self.version, "downloads": {"chromedriver": [
            {"platform": "win64", "url": "https://storage.googleapis.com/test.zip"}]}}
        downloader.json.return_value = {"builds": {"153.0.1234": release}}
        self.assertFalse(chrome_release(self.version, downloader)[2])
        downloader.json.side_effect = [{"builds": {}}, {"milestones": {"153": release}}]
        self.assertTrue(chrome_release(self.version, downloader)[2])

    def test_invalid_zip_preserves_existing(self):
        path = self.cache()
        def invalid_zip(url, destination):
            destination.write_bytes(b"not a zip")
        with patch("drivers.exe_version", return_value=self.version), \
             patch("drivers.driver_version", return_value="153.0.1200.1"), \
             patch("drivers.chrome_release", return_value=(self.version, "https://storage.googleapis.com/test.zip", False)), \
             patch("drivers.Downloader.fetch", side_effect=invalid_zip):
            with self.assertRaises(zipfile.BadZipFile):
                DriverManager(self.root).ensure("chrome", Path("chrome.exe"), threading.Event())
        self.assertEqual(path.read_bytes(), b"old")
        self.assertFalse(list(path.parent.glob(".download-*")))

    def test_edge_latest_requires_three_matching_numbers(self):
        downloader = MagicMock()
        downloader.fetch.return_value = "153.0.1234.99".encode("utf-16")
        self.assertEqual(edge_latest(self.version, downloader), "153.0.1234.99")
        downloader.fetch.return_value = b"153.0.9999.99"
        with self.assertRaises(ValueError):
            edge_latest(self.version, downloader)


class LoggingTests(unittest.TestCase):
    def test_redacts_exception_and_message(self):
        secrets = SecretFilter()
        secrets.add("private-token")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(SafeFormatter(secrets))
        logger = logging.Logger("test")
        logger.addHandler(handler)
        try:
            raise RuntimeError("cookie=private-token")
        except RuntimeError:
            logger.exception("token=%s", "private-token")
        self.assertNotIn("private-token", stream.getvalue())
        self.assertIn("[SESSION REDACTED]", stream.getvalue())


class WorkerTests(TemporaryTest):
    def test_failed_refresh_still_closes_browser(self):
        driver = MagicMock()
        worker = BrowserWorker(Settings(), self.root, SecretFilter(), False)
        outcomes = []
        worker.outcome.connect(outcomes.append)
        with patch("browser.DriverManager.ensure", return_value=self.root / "driver.exe"), \
             patch("browser.create_driver", return_value=driver), \
             patch("browser.refresh", side_effect=RuntimeError("offline")):
            with self.assertLogs("iros", level="ERROR"):
                worker.run()
        driver.quit.assert_called_once()
        self.assertIn("재시도", outcomes[0])

    def test_manual_save_closes_browser_and_emits_metadata(self):
        driver = MagicMock()
        driver.current_url = "https://www.iros.go.kr/"
        worker = BrowserWorker(Settings(), self.root, SecretFilter(), True)
        worker.save_login()
        metadata = []
        worker.metadata.connect(metadata.append)
        with patch("browser.DriverManager.ensure", return_value=self.root / "driver.exe"), \
             patch("browser.create_driver", return_value=driver), \
             patch("browser.capture_session", return_value={"httpOnly": True}):
            worker.run()
        self.assertEqual(metadata, [{"httpOnly": True}])
        driver.quit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
