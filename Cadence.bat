@echo off
REM Launch Cadence from source. Creates the virtual environment on first run.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo First run: creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv || goto :nopython
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

start "" ".venv\Scripts\pythonw.exe" -m cadence %*
exit /b 0

:nopython
echo Python 3.10+ is required and was not found on PATH.
echo Install it from https://python.org and run this again.
pause
exit /b 1
