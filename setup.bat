@echo off
setlocal EnableDelayedExpansion
title Stock Warrior - Automated Setup
color 0B

echo ===================================================
echo       STOCK WARRIOR - AUTOMATED SETUP
echo ===================================================
echo.

:: 1. Admin Rights Check
echo [1/4] Checking for Administrator privileges...
net session >nul 2>&1
if %errorLevel% == 0 (
    echo [OK] Running as Administrator.
) else (
    color 0C
    echo [ERROR] This setup script must be run as Administrator.
    echo Please right-click setup.bat and select "Run as administrator".
    pause
    exit /b 1
)
echo.

:: 2. Google Chrome Check
echo [2/4] Checking for Google Chrome...
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    echo [OK] Google Chrome is installed.
) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    echo [OK] Google Chrome is installed.
) else if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" (
    echo [OK] Google Chrome is installed.
) else (
    echo [INFO] Google Chrome not found. Downloading installer...
    curl -L -o "%temp%\ChromeSetup.exe" "https://dl.google.com/chrome/install/latest/chrome_installer.exe"
    if exist "%temp%\ChromeSetup.exe" (
        echo [INFO] Installing Google Chrome...
        start /wait "" "%temp%\ChromeSetup.exe" /silent /install
        echo [OK] Google Chrome installed.
    ) else (
        echo [ERROR] Failed to download Chrome. Please install manually.
    )
)
echo.

:: 3. Python Check
echo [3/4] Checking for Python...
python --version >nul 2>&1
if %errorLevel% == 0 (
    echo [OK] Python is installed.
) else (
    echo [INFO] Python not found. Downloading Python 3.12 installer...
    curl -L -o "%temp%\python-installer.exe" "https://www.python.org/ftp/python/3.12.2/python-3.12.2-amd64.exe"
    if exist "%temp%\python-installer.exe" (
        echo [INFO] Installing Python 3.12...
        start /wait "" "%temp%\python-installer.exe" /quiet InstallAllUsers=1 PrependPath=1
        echo [OK] Python installed. 
        echo [WARNING] You may need to close and reopen your terminal after setup to use 'python'.
    ) else (
        echo [ERROR] Failed to download Python. Please install manually from python.org.
    )
)
echo.

:: 4. PIP Dependencies
echo [4/4] Installing required Python libraries...
:: Attempt to upgrade pip quietly
python -m pip install --upgrade pip >nul 2>&1

echo [INFO] Installing core libraries (Flask, YT-DLP, MoviePy, etc.)...
python -m pip install flask pyautogui requests psutil pyperclip aiogram praw moviepy yt-dlp

echo [INFO] Installing Google API libraries...
python -m pip install google-api-python-client google-auth google-auth-oauthlib

echo.
echo ===================================================
echo       SETUP COMPLETE!
echo ===================================================
echo Everything has been installed. 
echo.
echo NEXT STEPS:
echo 1. Copy '.env.example' to '.env' and fill in your secrets.
echo 2. Run 'python browser_bridge.py' to initialize the browser.
echo 3. Run 'python test_suite.py --quick' to verify the installation.
echo.
pause
