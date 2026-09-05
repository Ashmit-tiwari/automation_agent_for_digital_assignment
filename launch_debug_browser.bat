@echo off
set PORT=%1
if "%PORT%"=="" set PORT=9222

echo ========================================================
echo Launching Browser with Remote Debugging on Port %PORT%
echo ========================================================

:: If Brave is already running without debugging port, close it so port %PORT% can activate
tasklist /fi "imagename eq brave.exe" 2>nul | find /i "brave.exe" >nul
if not errorlevel 1 (
    echo [!] Brave is currently open without remote debugging.
    echo [*] Restarting Brave with remote debugging on port %PORT%...
    taskkill /F /IM brave.exe >nul 2>&1
    ping -n 3 127.0.0.1 >nul
)

set FLAGS=--remote-debugging-port=%PORT% --remote-allow-origins=* --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding

:: Check if Brave exists
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    echo Launching Brave on port %PORT%...
    start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" %FLAGS%
    goto :started
)

if exist "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" (
    echo Launching Brave on port %PORT%...
    start "" "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" %FLAGS%
    goto :started
)

:: Check if Chrome exists
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    echo Launching Google Chrome on port %PORT%...
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" %FLAGS%
    goto :started
)

if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    echo Launching Google Chrome on port %PORT%...
    start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" %FLAGS%
    goto :started
)

if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    echo Launching Google Chrome on port %PORT%...
    start "" "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" %FLAGS%
    goto :started
)

:: Check if Microsoft Edge exists
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    echo Launching Microsoft Edge on port %PORT%...
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" %FLAGS%
    goto :started
)

if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    echo Launching Microsoft Edge on port %PORT%...
    start "" "C:\Program Files\Microsoft\Edge\Application\msedge.exe" %FLAGS%
    goto :started
)

echo [!] Could not find Brave, Chrome, or Edge installation.
pause
exit /b 1

:started
echo [✓] Browser launched with remote debugging on port %PORT%.
echo Please open your ByteXL and Gemini tabs, then start the agent.
if "%2"=="" (
    pause
)

