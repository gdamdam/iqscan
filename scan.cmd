@echo off
setlocal
set "SCAN_DIR=%~dp0"
set "SCAN_DIR=%SCAN_DIR:~0,-1%"
set "PY=%SCAN_DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    py -3 -m venv "%SCAN_DIR%\.venv" 2>nul || python -m venv "%SCAN_DIR%\.venv" || exit /b 1
)
"%PY%" -c "import iqscan" >nul 2>&1 || "%PY%" -m pip install -e "%SCAN_DIR%" 1>&2 || exit /b 1
if /i "%~1"=="meteor" (
    for %%A in (%*) do (
        if /i "%%~A"=="--video" (
            "%PY%" -c "import PIL, skyfield, tzdata" >nul 2>&1 || "%PY%" -m pip install -e "%SCAN_DIR%[video]" 1>&2 || exit /b 1
        )
    )
)
"%PY%" -m iqscan %*
exit /b %ERRORLEVEL%
