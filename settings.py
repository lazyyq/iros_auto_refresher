"""Entrypoint-relative settings and atomic session storage."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COOKIE_NAME = "PM10SESSIONID"
COOKIE_FIELDS = ("domain", "path", "secure", "httpOnly", "sameSite")


def resolve_path(value: str, root: Path = ROOT) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as stream:
            temp = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def discover_browser(browser: str) -> str:
    suffix = ("Google/Chrome/Application/chrome.exe" if browser == "chrome"
              else "Microsoft/Edge/Application/msedge.exe")
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(name)
        if base and (path := Path(base) / suffix).is_file():
            return str(path)
    return ""


@dataclass
class Settings:
    browser: str = "chrome"
    interval_minutes: int = 15
    chrome_exe: str = field(default_factory=lambda: discover_browser("chrome"))
    edge_exe: str = field(default_factory=lambda: discover_browser("edge"))
    session_file: str = "session.txt"
    cookie_metadata: dict = field(default_factory=dict)

    def validate(self, root: Path = ROOT, *, browser_required: bool = True) -> None:
        if self.browser not in ("chrome", "edge"):
            raise ValueError("브라우저는 Chrome 또는 Edge여야 합니다.")
        if type(self.interval_minutes) is not int or not 1 <= self.interval_minutes <= 10080:
            raise ValueError("접속 주기는 1~10080분의 정수여야 합니다.")
        if not isinstance(self.chrome_exe, str) or not isinstance(self.edge_exe, str):
            raise ValueError("브라우저 경로가 올바르지 않습니다.")
        if not isinstance(self.session_file, str) or not self.session_file.strip():
            raise ValueError("세션 출력 파일을 지정하세요.")
        output = resolve_path(self.session_file, root)
        if output.is_dir():
            raise ValueError("세션 출력 경로는 파일이어야 합니다.")
        # A session export must never overwrite application settings or code.
        protected = {root / name for name in ("config.json", "main.py", "settings.py",
                     "drivers.py", "browser.py", "gui.py", "README.md", "AGENTS.md", "requirements.txt")}
        browser_paths = {resolve_path(p, root) for p in (self.chrome_exe, self.edge_exe) if p}
        cache_roots = (root / "logs", root / "chromedriver", root / "edgedriver")
        if (output in protected | browser_paths or output.suffix.lower() in (".py", ".exe", ".dll")
                or any(output.is_relative_to(directory) for directory in cache_roots)):
            raise ValueError("세션 파일은 설정·코드·실행 파일과 다른 경로여야 합니다.")
        if not isinstance(self.cookie_metadata, dict):
            raise ValueError("쿠키 속성 설정이 올바르지 않습니다.")
        if browser_required:
            value = self.chrome_exe if self.browser == "chrome" else self.edge_exe
            expected = "chrome.exe" if self.browser == "chrome" else "msedge.exe"
            if not value or not resolve_path(value, root).is_file():
                raise ValueError(f"{expected} 파일을 지정하세요.")
            if resolve_path(value, root).name.lower() != expected:
                raise ValueError(f"선택한 브라우저의 {expected} 파일을 지정하세요.")


class SettingsStore:
    def __init__(self, root: Path = ROOT):
        self.root = root
        self.path = root / "config.json"

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("config.json은 JSON 객체여야 합니다.")
        settings = Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})
        settings.validate(self.root, browser_required=False)
        return settings

    def save(self, settings: Settings) -> None:
        settings.validate(self.root, browser_required=False)
        atomic_write(self.path, json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n")


def read_session(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        return ""
    if any(char.isspace() for char in value):
        raise ValueError("세션 파일에는 공백 없는 쿠키값 하나만 입력하세요.")
    return value
