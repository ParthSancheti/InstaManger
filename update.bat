@echo off
echo [Auto-Updater] Waiting 3 seconds for python processes to exit gracefully...
timeout /t 3 /nobreak >nul

echo [Auto-Updater] Stopping Stock Warrior processes only...
:: taskkill /IM python.exe would kill EVERY python process on this machine.
:: Match on the command line so unrelated scripts survive the update.
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'main_orchestrator|browser_bridge|telegram_bot' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1
timeout /t 2 /nobreak >nul

echo [Auto-Updater] Pulling latest changes from GitHub...
git pull origin main > update_log.txt 2>&1
set GIT_EXIT_CODE=%errorlevel%

type update_log.txt
echo.

echo [Auto-Updater] Sending Telegram notification...
python update_helper.py %GIT_EXIT_CODE%

if %GIT_EXIT_CODE% neq 0 (
    echo [Auto-Updater] Aborting update due to git failure.
    pause
    exit /b %GIT_EXIT_CODE%
)

echo [Auto-Updater] Restarting orchestrator...
start cmd /k "python main_orchestrator.py"
