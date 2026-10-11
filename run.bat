@echo off
setlocal

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo ERROR: Python was not found in .venv\Scripts.
    echo Create the virtual environment and install requirements.txt first.
    pause
    exit /b 1
)

pushd "%~dp0"
if errorlevel 1 exit /b 1

"%~dp0.venv\Scripts\python.exe" "%~dp0main.py"
set "app_exit=%errorlevel%"
popd

if not "%app_exit%"=="0" (
    echo ERROR: Application exited with code %app_exit%.
    pause
)

exit /b %app_exit%
