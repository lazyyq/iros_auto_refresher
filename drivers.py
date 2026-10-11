"""Official driver downloads with an entrypoint-local, version-checked cache."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from settings import atomic_write

LOG = logging.getLogger("iros")
VERSION_RE = re.compile(r"\b\d+\.\d+\.\d+\.\d+\b")
CFT = "https://googlechromelabs.github.io/chrome-for-testing/"
EDGE = "https://msedgedriver.microsoft.com/"
ALLOWED_HOSTS = {"googlechromelabs.github.io", "storage.googleapis.com", "msedgedriver.microsoft.com"}


class Cancelled(Exception):
    pass


def check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise Cancelled("작업이 취소되었습니다.")


def parse_version(text: str) -> str:
    match = VERSION_RE.search(text)
    if not match:
        raise ValueError("실행 파일의 버전을 확인할 수 없습니다.")
    return match.group()


def compatible(browser_version: str, driver_version: str) -> bool:
    return browser_version.split(".")[:3] == driver_version.split(".")[:3]


def exe_version(path: Path) -> str:
    """Read VS_FIXEDFILEINFO without launching the browser or consulting registry."""
    if os.name != "nt":
        raise OSError("브라우저 버전 조회는 Windows에서 지원합니다.")
    library = ctypes.WinDLL("version", use_last_error=True)
    library.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    library.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    library.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    library.GetFileVersionInfoW.restype = wintypes.BOOL
    library.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    library.VerQueryValueW.restype = wintypes.BOOL
    handle = wintypes.DWORD()
    size = library.GetFileVersionInfoSizeW(str(path), ctypes.byref(handle))
    if not size:
        raise OSError("EXE에 Windows 버전 정보가 없습니다.")
    buffer = ctypes.create_string_buffer(size)
    if not library.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not library.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)) or length.value < 24:
        raise OSError("EXE 버전 정보를 해석할 수 없습니다.")
    fields = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    if fields[0] != 0xFEEF04BD:
        raise OSError("EXE 버전 리소스가 올바르지 않습니다.")
    ms, ls = fields[2], fields[3]
    return f"{ms >> 16}.{ms & 65535}.{ls >> 16}.{ls & 65535}"


def driver_version(path: Path) -> str:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    result = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                            timeout=10, creationflags=flags, check=True)
    return parse_version(result.stdout)


def validate_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise ValueError("공식 HTTPS 드라이버 배포 주소가 아닙니다.")


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Downloader:
    def __init__(self, cancel: threading.Event):
        self.cancel = cancel
        self.deadline = time.monotonic() + 180
        self.opener = urllib.request.build_opener(OfficialRedirect())

    def fetch(self, url: str, destination: Path | None = None) -> bytes:
        validate_url(url)
        check_cancel(self.cancel)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("드라이버 다운로드 제한 시간(180초)을 초과했습니다.")
        request = urllib.request.Request(url, headers={"User-Agent": "iros-auto-refresher/1.0"})
        chunks, total = [], 0
        stream = destination.open("wb") if destination else None
        try:
            with self.opener.open(request, timeout=min(30, remaining)) as response:
                validate_url(response.url)
                while True:
                    check_cancel(self.cancel)
                    if time.monotonic() >= self.deadline:
                        raise TimeoutError("드라이버 다운로드 제한 시간을 초과했습니다.")
                    # read1 avoids waiting to fill an entire chunk on a slow connection.
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > (100 * 1024 * 1024 if destination else 20 * 1024 * 1024):
                        raise ValueError("다운로드 파일이 예상 크기를 초과했습니다.")
                    if stream:
                        stream.write(chunk)
                    else:
                        chunks.append(chunk)
        finally:
            if stream:
                stream.close()
        return b"".join(chunks)

    def json(self, url: str) -> dict:
        return json.loads(self.fetch(url).decode("utf-8"))


def chrome_release(version: str, downloader: Downloader) -> tuple[str, str, bool]:
    major = version.split(".")[0]
    if int(major) < 115:
        raise ValueError("Chrome 자동 다운로드는 버전 115 이상을 지원합니다.")
    build = ".".join(version.split(".")[:3])
    data = downloader.json(CFT + "latest-patch-versions-per-build-with-downloads.json")
    release = data["builds"].get(build)
    fallback = release is None
    if fallback:
        data = downloader.json(CFT + "latest-versions-per-milestone-with-downloads.json")
        release = data["milestones"].get(major)
    if not release or release["version"].split(".")[0] != major:
        raise ValueError("해당 Chrome 버전의 공식 드라이버가 아직 없습니다.")
    for item in release.get("downloads", {}).get("chromedriver", []):
        if item["platform"] == "win64":
            validate_url(item["url"])
            return release["version"], item["url"], fallback
    raise ValueError("win64 ChromeDriver 다운로드를 찾을 수 없습니다.")


def edge_latest(version: str, downloader: Downloader) -> str:
    raw = downloader.fetch(EDGE + f"LATEST_RELEASE_{version.split('.')[0]}_WINDOWS")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif b"\x00" in raw:
        text = raw.decode("utf-16-le")
    else:
        text = raw.decode("utf-8-sig")
    latest = parse_version(text)
    if not compatible(version, latest):
        raise ValueError("공식 EdgeDriver와 브라우저의 앞 세 버전 숫자가 일치하지 않습니다.")
    return latest


class DriverManager:
    def __init__(self, root: Path):
        self.root = root

    def ensure(self, browser: str, binary: Path, cancel: threading.Event) -> Path:
        check_cancel(cancel)
        version = exe_version(binary)
        LOG.info("[%s] 브라우저 버전: %s", browser, version)
        folder = self.root / ("chromedriver" if browser == "chrome" else "edgedriver") / version.split(".")[0]
        name = "chromedriver.exe" if browser == "chrome" else "msedgedriver.exe"
        target = folder / name
        manifest = folder / "driver.json"
        if target.is_file():
            try:
                cached = driver_version(target)
                fallback_cached = False
                if browser == "chrome" and manifest.is_file():
                    try:
                        data = json.loads(manifest.read_text(encoding="utf-8"))
                        fallback_cached = (data.get("browser_version") == version
                                           and data.get("driver_version") == cached
                                           and data.get("milestone_fallback") is True
                                           and cached.split(".")[0] == version.split(".")[0])
                    except (OSError, ValueError, AttributeError):
                        pass
                if compatible(version, cached) or fallback_cached:
                    LOG.info("[%s] 호환 드라이버 재사용: %s", browser, cached)
                    return target
                LOG.info("[%s] 드라이버 교체 필요: %s", browser, cached)
            except (OSError, ValueError, subprocess.SubprocessError):
                LOG.warning("[%s] 기존 드라이버 검증 실패; 다시 다운로드합니다.", browser)
        folder.mkdir(parents=True, exist_ok=True)
        downloader = Downloader(cancel)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=folder) as temporary:
            temporary = Path(temporary)
            archive = temporary / "driver.zip"
            LOG.info("[%s] 공식 드라이버 다운로드 시작", browser)
            fallback = False
            if browser == "chrome":
                expected, url, fallback = chrome_release(version, downloader)
                if fallback:
                    LOG.warning("[chrome] 해당 빌드 미배포; 같은 메이저 배포 버전을 시도합니다.")
                downloader.fetch(url, archive)
            else:
                # Exact versions remain available even when the major's latest build has changed.
                expected = version
                try:
                    downloader.fetch(EDGE + f"{expected}/edgedriver_win64.zip", archive)
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
                    expected = edge_latest(version, downloader)
                    downloader.fetch(EDGE + f"{expected}/edgedriver_win64.zip", archive)
            check_cancel(cancel)
            candidate = temporary / name
            with zipfile.ZipFile(archive) as zipped:
                matches = [info for info in zipped.infolist()
                           if Path(info.filename).name.lower() == name and not info.is_dir()]
                if len(matches) != 1 or matches[0].file_size > 100 * 1024 * 1024:
                    raise ValueError("ZIP에 유효한 드라이버 EXE 하나가 있어야 합니다.")
                # Never extract arbitrary archive paths.
                with zipped.open(matches[0]) as source, candidate.open("wb") as output:
                    while chunk := source.read(65536):
                        check_cancel(cancel)
                        output.write(chunk)
            actual = driver_version(candidate)
            if actual != expected or (not fallback and not compatible(version, actual)):
                raise ValueError("다운로드한 드라이버의 버전 검증에 실패했습니다.")
            check_cancel(cancel)
            os.replace(candidate, target)
            try:
                atomic_write(manifest, json.dumps({"browser_version": version,
                    "driver_version": actual, "milestone_fallback": fallback}, indent=2) + "\n")
            except OSError:
                LOG.warning("[%s] 드라이버 캐시 메타데이터 저장 실패", browser)
        LOG.info("[%s] 드라이버 저장 완료: %s", browser, actual)
        return target
