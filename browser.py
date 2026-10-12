"""Selenium lifecycle. Each worker exclusively owns its WebDriver."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
from urllib.parse import urlparse

from PyQt5.QtCore import QThread, pyqtSignal
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from app_logging import SecretFilter
from drivers import Cancelled, DriverManager, check_cancel
from settings import COOKIE_FIELDS, COOKIE_NAME, Settings, atomic_write, read_session, resolve_path

SITE_URL = "https://www.iros.go.kr/"
USER_XPATH = '//*[@id="mf_wfm_potal_main_wf_header_login_user_name"]'
LOG = logging.getLogger("iros")


def create_driver(settings: Settings, binary: Path, driver_path: Path, headless: bool):
    options = webdriver.ChromeOptions() if settings.browser == "chrome" else webdriver.EdgeOptions()
    options.binary_location = str(binary)
    options.add_argument("--window-size=1440,1000")
    options.add_argument("--no-first-run")
    if headless:
        options.add_argument("--headless=new")
    service_type = ChromeService if settings.browser == "chrome" else EdgeService
    service = service_type(executable_path=str(driver_path), log_output=subprocess.DEVNULL)
    if os.name == "nt":
        service.creation_flags = subprocess.CREATE_NO_WINDOW
    constructor = webdriver.Chrome if settings.browser == "chrome" else webdriver.Edge
    try:
        driver = constructor(service=service, options=options)
    except Exception:
        service.stop()
        raise
    try:
        # Bound hung commands, including quit, rather than inheriting an unlimited timeout.
        driver.command_executor._client_config.timeout = 70
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
        return driver
    except Exception:
        driver.quit()
        raise


def trusted_host(driver) -> str:
    host = (urlparse(driver.current_url).hostname or "").lower()
    if host != "iros.go.kr" and not host.endswith(".iros.go.kr"):
        raise ValueError("브라우저가 등기소 도메인에 있지 않습니다.")
    return host


def injected_cookie(value: str, metadata: dict, host: str) -> dict:
    cookie = {"name": COOKIE_NAME, "value": value, "path": "/", "secure": True, "httpOnly": True}
    domain = metadata.get("domain", "")
    plain = domain.lstrip(".") if isinstance(domain, str) else ""
    if plain and (plain == "iros.go.kr" or plain.endswith(".iros.go.kr")) and (host == plain or host.endswith("." + plain)):
        cookie["domain"] = domain
        if isinstance(metadata.get("path"), str) and metadata["path"].startswith("/"):
            cookie["path"] = metadata["path"]
        for key in ("secure", "httpOnly"):
            if isinstance(metadata.get(key), bool):
                cookie[key] = metadata[key]
        if metadata.get("sameSite") in ("Strict", "Lax", "None"):
            cookie["sameSite"] = metadata["sameSite"]
    return cookie


def username(driver, cancel: threading.Event, timeout: float = 30) -> str:
    def visible_name(current):
        check_cancel(cancel)
        elements = current.find_elements(By.XPATH, USER_XPATH)
        for element in elements:
            if element.is_displayed() and (text := element.text.strip()):
                return text
        return False
    try:
        return WebDriverWait(driver, timeout, poll_frequency=0.25).until(visible_name)
    except TimeoutException:
        return ""


def capture_session(driver, settings: Settings, root: Path, cancel: threading.Event,
                    secrets: SecretFilter, name_timeout: float = 30) -> dict | None:
    check_cancel(cancel)
    trusted_host(driver)
    # Register before waiting for the name so later driver errors cannot expose it.
    initial = driver.get_cookie(COOKIE_NAME)
    if initial:
        secrets.add(initial.get("value", ""))
    name = username(driver, cancel, name_timeout)
    if name:
        LOG.info("[%s] 사용자 이름 요소 텍스트: %s", settings.browser, name)
    else:
        LOG.warning("[%s] 사용자 이름 미표시: 로그인 확인 실패", settings.browser)
    check_cancel(cancel)
    trusted_host(driver)
    cookie = driver.get_cookie(COOKIE_NAME)
    if not cookie or not cookie.get("value"):
        LOG.warning("[%s] PM10SESSIONID 없음; 기존 세션 파일 유지", settings.browser)
        return None
    secrets.add(cookie["value"])
    atomic_write(resolve_path(settings.session_file, root), cookie["value"])
    LOG.info("[%s] 세션 파일 저장 완료 (%s)", settings.browser,
             "사용자 이름 확인" if name else "사용자 이름 미확인·익명 세션일 수 있음")
    return {key: cookie[key] for key in COOKIE_FIELDS if key in cookie}


def refresh(driver, settings: Settings, root: Path, cancel: threading.Event,
            secrets: SecretFilter) -> dict | None:
    value = read_session(resolve_path(settings.session_file, root))
    secrets.add(value)
    check_cancel(cancel)
    driver.get(SITE_URL)
    check_cancel(cancel)
    host = trusted_host(driver)
    if value:
        driver.delete_cookie(COOKIE_NAME)
        driver.add_cookie(injected_cookie(value, settings.cookie_metadata, host))
        LOG.info("[%s] PM10SESSIONID 주입 완료", settings.browser)
        check_cancel(cancel)
        driver.get(SITE_URL)
    else:
        LOG.info("[%s] 세션 파일 없음 또는 빈 값; 쿠키 주입 없이 접속", settings.browser)
    return capture_session(driver, settings, root, cancel, secrets)


class BrowserWorker(QThread):
    ready = pyqtSignal(bool)
    metadata = pyqtSignal(dict)
    outcome = pyqtSignal(str)

    def __init__(self, settings: Settings, root: Path, secrets: SecretFilter, manual: bool, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.root = root
        self.secrets = secrets
        self.manual = manual
        self.cancel = threading.Event()
        self.commands = queue.Queue()

    def stop(self):
        self.cancel.set()
        self.commands.put("stop")

    def save_login(self):
        self.commands.put("save")

    def run(self):
        driver = None
        result = "수동 로그인 실패" if self.manual else "작업 실패; 다음 주기에 재시도"
        label = "수동 로그인" if self.manual else "자동 갱신"
        try:
            LOG.info("[%s] %s 시작", self.settings.browser, label)
            value = self.settings.chrome_exe if self.settings.browser == "chrome" else self.settings.edge_exe
            binary = resolve_path(value, self.root)
            path = DriverManager(self.root).ensure(self.settings.browser, binary, self.cancel)
            check_cancel(self.cancel)
            driver = create_driver(self.settings, binary, path, headless=not self.manual)
            if self.manual:
                driver.get(SITE_URL)
                trusted_host(driver)
                self.ready.emit(True)
                LOG.info("직접 로그인과 캡챠를 처리한 뒤 '로그인 완료·세션 저장'을 누르세요.")
                while True:
                    check_cancel(self.cancel)
                    try:
                        command = self.commands.get(timeout=1)
                    except queue.Empty:
                        if not driver.window_handles:
                            result = "수동 로그인 브라우저 닫힘"
                            break
                        continue
                    if command == "stop":
                        raise Cancelled()
                    self.ready.emit(False)
                    try:
                        metadata = capture_session(driver, self.settings, self.root, self.cancel, self.secrets)
                        if metadata is not None:
                            self.metadata.emit(metadata)
                            result = "수동 세션 저장 완료"
                            break
                    except Cancelled:
                        raise
                    except Exception:
                        LOG.exception("수동 세션 저장 실패; 브라우저에서 다시 확인하세요.")
                    self.ready.emit(True)
            else:
                metadata = refresh(driver, self.settings, self.root, self.cancel, self.secrets)
                if metadata is not None:
                    self.metadata.emit(metadata)
                    result = "자동 접속·세션 저장 완료"
                else:
                    result = "쿠키 없음; 다음 주기에 재시도"
        except Cancelled:
            result = "작업 중지"
            LOG.info("%s 중지", label)
        except Exception:
            LOG.exception("[%s] %s 실패", self.settings.browser, label)
        finally:
            self.ready.emit(False)
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    LOG.exception("브라우저 종료 실패")
                    try:
                        driver.service.stop()
                    except Exception:
                        LOG.error("드라이버 서비스 종료 실패")
            LOG.info("[%s] %s 종료: %s", self.settings.browser, label, result)
            self.outcome.emit(result)
