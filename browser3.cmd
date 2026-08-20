@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
where py.exe >nul 2>nul
if %ERRORLEVEL% equ 0 (
    py.exe -3 "%SCRIPT_DIR%launcher.py" %*
    exit /b %ERRORLEVEL%
)
where python.exe >nul 2>nul
if %ERRORLEVEL% equ 0 (
    python.exe "%SCRIPT_DIR%launcher.py" %*
    exit /b %ERRORLEVEL%
)
echo [Browser3] Python 3.7+ was not found on PATH.
echo Please install Python 3 (https://www.python.org/downloads/) or add it to PATH.
exit /b 1
