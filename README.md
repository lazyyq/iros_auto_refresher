# 인터넷등기소 세션 자동 갱신

Windows용 PyQt5 GUI에서 Selenium으로 인터넷등기소에 접속하고, HTTPOnly 쿠키
`PM10SESSIONID` 값을 지정한 텍스트 파일에 저장합니다. 일반 브라우저 창에서
직접 로그인할 수 있으며, 자동 접속은 headless로 수행합니다.

## 설치와 실행

Windows x64, Python 3.10 이상, Chrome 115 이상 또는 Chromium 기반 Edge가 필요합니다.
기존 PyQt6 환경에서 전환할 때는 아래 명령으로 GUI 의존성을 교체합니다.

```powershell
.\.venv\Scripts\python.exe -m pip uninstall -y PyQt6 PyQt6-Qt6 PyQt6-sip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

설치 후에는 프로젝트 폴더의 `run.bat`을 더블클릭해 실행할 수도 있습니다.
실행 위치와 관계없이 같은 폴더의 `.venv\Scripts\python.exe`로 `main.py`를 실행합니다.
가상환경이 없거나 실행 오류가 발생하면 콘솔에서 내용을 확인할 수 있도록 대기합니다.

## EXE 패키징

Windows x64에서 프로젝트 폴더를 열고 같은 가상환경으로 빌드합니다.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm --onefile --windowed --name IrosAutoRefresher --collect-submodules selenium main.py
```

생성된 `dist\IrosAutoRefresher.exe`를 다른 PC로 복사해 실행합니다.
대상 PC에는 Chrome 또는 Edge가 설치되어 있어야 합니다.
설정·세션 파일·로그·드라이버는 EXE 폴더에 생성됩니다(세션 경로를 별도 지정한 경우 제외).

Selenium의 동적 import가 빌드에서 누락되지 않도록 하위 모듈을 명시적으로 포함합니다.
`No module named 'selenium.webdriver.edge.options'` 오류가 발생한 기존 EXE는
위 명령으로 다시 빌드한 EXE로 교체하세요. 기존 `.spec`으로 빌드하는 경우에도
`Analysis`의 `hiddenimports`에 `collect_submodules("selenium")`를 반영해야 합니다
(`from PyInstaller.utils.hooks import collect_submodules`).

## 사용 방법

1. Chrome 또는 Edge를 선택하고 해당 `chrome.exe` 또는 `msedge.exe` 경로를 지정합니다.
   일반 설치 위치에서 발견한 경로는 처음 실행할 때 자동으로 채웁니다.
2. 자동 접속 주기와 세션 출력 파일을 지정합니다. 기본값은 15분과 `session.txt`입니다.
3. **수동 로그인**을 누릅니다. 열린 브라우저에서 사이트의 로그인 메뉴를 사용하고,
   캡챠와 로그인을 직접 처리합니다.
4. 로그인 후 사이트 메인 화면으로 돌아와 **로그인 완료·세션 저장**을 누릅니다.
   사용자 이름 확인 결과와 쿠키 저장 결과를 기록하고 브라우저를 닫습니다.
   쿠키가 없거나 저장에 실패하면 창이 유지되므로 다시 시도할 수 있습니다.
5. **자동 갱신 시작**을 누르면 즉시 한 번 접속하고, 작업 완료 시점부터 설정한
   간격마다 다시 접속합니다. **중지**는 예약을 취소하고 진행 중 작업을 정리합니다.
6. **즉시 갱신**을 누르면 예약 시각을 기다리지 않고 바로 headless로 접속합니다.
   자동 갱신 중이면 기존 예약을 취소하고, 즉시 갱신 작업 완료 시각부터 설정한 주기를
   다시 셉니다. 자동 갱신이 꺼져 있으면 한 번만 실행합니다.
   수동 로그인·갱신·중지 처리 중에는 즉시 갱신 버튼을 사용할 수 없습니다.

앱 재시작 시 자동 갱신은 시작하지 않습니다. 자동 예약 대기 중 수동 로그인을
시작하면 예약을 잠시 멈추고, 수동 작업이 끝난 뒤 지정 주기로 재개합니다.
실행 중 설정을 바꾸려면 먼저 중지하고 작업 종료를 기다리세요.

## 드라이버 자동 관리

브라우저 EXE의 Windows 버전 리소스에서 전체 버전을 읽습니다. 사용자가 지정한
EXE를 실제 Selenium 브라우저 실행에도 사용합니다.

```text
프로젝트/
  main.py
  config.json
  session.txt
  chromedriver/<메이저버전>/chromedriver.exe
  edgedriver/<메이저버전>/msedgedriver.exe
  logs/iros_auto_refresher.log
```

Chrome은 [공식 Chrome for Testing 버전 정보](https://developer.chrome.com/docs/chromedriver/downloads/version-selection)에서
브라우저의 `MAJOR.MINOR.BUILD`에 맞는 win64 드라이버를 선택합니다. 해당 빌드가
없으면 같은 메이저의 공식 배포 버전을 시도하며 호환 여부는 브라우저 실행 시 확인합니다.

Edge는 해당 전체 버전의 공식 ZIP을 우선 요청합니다. 미배포(404)일 때 공식
`LATEST_RELEASE_<메이저>_WINDOWS` 정보를 조회하고, 앞 세 숫자가 일치할 때만
사용합니다. [Microsoft 호환 기준](https://learn.microsoft.com/en-us/microsoft-edge/webdriver-chromium/)을 따릅니다.

호환되는 기존 드라이버는 네트워크 요청 없이 재사용합니다. 동일 메이저 내 업데이트도
빌드 호환성을 확인하고 필요한 경우 교체합니다. 공식 HTTPS 배포처에서 다운로드하고
ZIP 속 EXE와 버전을 검증한 뒤 교체하므로 다운로드 실패 시 기존 파일은 유지됩니다.

최초 다운로드에는 인터넷 연결과 프로젝트 폴더 쓰기 권한이 필요합니다. 사용한
이전 메이저 폴더는 자동 삭제하지 않습니다. Selenium Manager의 별도 캐시는 사용하지 않습니다.

## 세션 동작

- 자동 접속마다 지정한 텍스트 파일을 다시 읽습니다. 외부에서 파일 내용을 바꾸면
  다음 작업에 반영됩니다. 파일에는 공백 없는 쿠키값 하나만 입력합니다.
- 파일이 없거나 비어 있으면 쿠키를 주입하지 않고 접속합니다. 파일 접근 오류는
  작업 실패로 처리합니다.
- 값이 있으면 등기소 도메인에 먼저 접속해 `PM10SESSIONID`를 주입한 뒤 재접속합니다.
  JavaScript의 `document.cookie` 대신 Selenium 쿠키 API를 사용하므로 HTTPOnly를 지원합니다.
- 접속 후 다음 요소의 **텍스트만** 기록합니다. 스크린샷은 만들지 않습니다.

```xpath
//*[@id="mf_wfm_potal_main_wf_header_login_user_name"]
```

- 사용자 이름이 보이지 않아도 쿠키가 있으면 파일을 갱신합니다. 따라서 저장된 값은
  익명 세션일 수도 있습니다. 쿠키가 없거나 작업이 실패하면 기존 파일을 유지합니다.
- 사용자 이름 확인 실패와 접속 실패는 로그에 남기고 다음 주기에 재시도합니다.
- 세션값만 출력 파일에 저장합니다. domain·path·secure·httpOnly·sameSite 속성은 설정에
  보관하지만 쿠키값은 설정에 복제하지 않습니다. 다른 쿠키·비밀번호는 저장하지 않습니다.
- 세션 텍스트와 설정 파일은 임시 파일 작성 후 교체해 저장합니다.

사이트 접속만으로 서버의 로그인 만료 시간이 연장되는지는 사이트 정책에 달려 있습니다.
로그인 후 headless 접속에서도 같은 이름이 표시되는지 확인해야 합니다. 다른 쿠키가
필요하거나 서버가 로그인을 만료시키면 수동 로그인을 다시 해야 합니다.

## 설정과 로그

설정은 Python으로 실행하면 `main.py` 옆 `config.json`에 저장합니다.
PyInstaller로 패키징한 EXE로 실행하면 EXE와 같은 폴더에 저장합니다
(`--onefile`, `--onedir` 모두 동일). 현재 작업 폴더와 관계없이 이 위치를 사용하며,
상대 경로인 세션 파일·드라이버 캐시·로그도 이 폴더 기준입니다.
EXE가 있는 폴더에 쓰기 권한이 필요합니다. 주기는 1~10080분을 지원합니다.

GUI와 `logs/iros_auto_refresher.log`에 시각·수준·브라우저·작업 결과·사용자 이름을
기록합니다. 자동 작업 완료 후와 수동 로그인 후 자동 예약 재개 시 다음 갱신 예정 시각을
`다음 자동 갱신 예정: YYYY-MM-DD HH:MM:SS (15분 후)` 형식으로 양쪽 로그에 한 번 기록합니다.
파일은 UTF-8로 이어 쓰고 자정 이후 첫 로그 발생 시 교체하며, 지난 로그는
14개 보관합니다. 예외 상세도 파일에 기록합니다. GUI는 최근 3000줄을 유지합니다.
수집·주입한 세션값은 오류 메시지에서도 가려집니다.

로그 파일을 쓰지 못하면 GUI에 알립니다. 세션 파일은 인증 정보이며 로그에는
사용자 이름이 포함되므로 외부 공유 시 내용을 확인하세요. 기본 런타임 파일·드라이버·로그는
`.gitignore`에 제외되어 있습니다. 별도로 지정한 세션 파일도 저장소에 올리지 마세요.

## 제한 시간과 종료

Selenium 작업과 다운로드는 작업 스레드에서 실행해 GUI가 응답하도록 합니다.
페이지 로딩은 60초, 사용자 이름 확인은 30초, WebDriver 명령은 70초,
드라이버 버전 실행은 10초 제한입니다. 다운로드 요청은 30초, 전체 다운로드는
180초 제한이며 제한 확인은 네트워크 읽기 사이에 수행합니다.

중지나 앱 종료는 진행 중 네트워크·WebDriver 명령을 강제로 끊지 않고 반환을 기다린 후
브라우저를 닫습니다. 수동 브라우저는 저장 버튼을 누르거나 중지할 때까지 유지합니다.

## 테스트와 구성

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

- `main.py`, `gui.py`: 엔트리포인트, GUI, 주기 예약
- `browser.py`: Selenium 접속, 쿠키 주입·추출, 작업 스레드
- `drivers.py`: EXE 버전 조회, 공식 드라이버 다운로드·검증
- `settings.py`, `app_logging.py`: 설정·파일 저장, GUI·파일 로그
- `tests/`: 실제 로그인이나 네트워크에 의존하지 않는 자동 검증

실제 사이트의 수동 로그인·캡챠, 로그인 이름 확인, 서버 만료 연장은 별도로 검증해야 합니다.
