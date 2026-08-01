@echo off
setlocal

:: Locate Chrome
set CHROME_EXE=
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" set CHROME_EXE="C:\Program Files\Google\Chrome\Application\chrome.exe"
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" set CHROME_EXE="C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set CHROME_EXE="%LocalAppData%\Google\Chrome\Application\chrome.exe"

if "%CHROME_EXE%"=="" (
    echo [ERROR] Google Chrome not found.
    pause
    exit /b 1
)

:: Set paths relative to this script
set BASE_DIR=%~dp0
set PROFILE_DIR=%BASE_DIR%chrome-profile
set EXTENSION_DIR=%BASE_DIR%extension

echo Launching Automation Chrome Profile...

:: Launch Chrome with the same flags as browser_bridge.py
start "" %CHROME_EXE% --user-data-dir="%PROFILE_DIR%" --load-extension="%EXTENSION_DIR%" --disable-features=DisableLoadExtensionCommandLineSwitch --no-first-run --no-default-browser-check --restore-last-session=false chrome://extensions/
