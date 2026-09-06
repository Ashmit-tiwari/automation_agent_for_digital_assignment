@echo off
set PORT=%1
if "%PORT%"=="" set PORT=9222
set PREF=%2

echo ========================================================
echo Launching Automation Browser with Remote Debugging
echo Port: %PORT%
echo Profile: %USERPROFILE%\.bytexl_profile
echo ========================================================

set USER_DATA_DIR=%USERPROFILE%\.bytexl_profile
set FLAGS=--remote-debugging-port=%PORT% --remote-allow-origins=* --user-data-dir="%USER_DATA_DIR%" --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes --disable-ipc-flooding-protection --disable-hang-monitor
set URLS=https://www.bytexl.app https://gemini.google.com

:: 1. If user explicitly specified browser (e.g. chrome, edge, brave)
if /i "%PREF%"=="chrome" goto :try_chrome
if /i "%PREF%"=="edge" goto :try_edge
if /i "%PREF%"=="brave" goto :try_brave

:: 2. Auto-detect: Check which browser is currently running on this PC
tasklist /fi "imagename eq chrome.exe" 2>nul | find /i "chrome.exe" >nul
if not errorlevel 1 (
    echo [*] Detected Google Chrome currently running. Using Chrome...
    goto :try_chrome
)

tasklist /fi "imagename eq msedge.exe" 2>nul | find /i "msedge.exe" >nul
if not errorlevel 1 (
    echo [*] Detected Microsoft Edge currently running. Using Edge...
    goto :try_edge
)

tasklist /fi "imagename eq brave.exe" 2>nul | find /i "brave.exe" >nul
if not errorlevel 1 (
    echo [*] Detected Brave currently running. Using Brave...
    goto :try_brave
)

:: 3. If none currently running, try Chrome -> Edge -> Brave in order
:try_chrome
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    echo Launching Google Chrome on port %PORT%...
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" %FLAGS% %URLS%
    goto :started
)
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    echo Launching Google Chrome on port %PORT%...
    start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" %FLAGS% %URLS%
    goto :started
)
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    echo Launching Google Chrome on port %PORT%...
    start "" "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" %FLAGS% %URLS%
    goto :started
)
if /i "%PREF%"=="chrome" goto :not_found

:try_edge
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    echo Launching Microsoft Edge on port %PORT%...
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" %FLAGS% %URLS%
    goto :started
)
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    echo Launching Microsoft Edge on port %PORT%...
    start "" "C:\Program Files\Microsoft\Edge\Application\msedge.exe" %FLAGS% %URLS%
    goto :started
)
if /i "%PREF%"=="edge" goto :not_found

:try_brave
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    echo Launching Brave on port %PORT%...
    start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" %FLAGS% %URLS%
    goto :started
)
if exist "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" (
    echo Launching Brave on port %PORT%...
    start "" "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" %FLAGS% %URLS%
    goto :started
)

:not_found
echo [!] Could not find requested browser installation.
pause
exit /b 1

:started
echo [✓] Browser launched with remote debugging on port %PORT%.
echo ByteXL and Gemini tabs are opening automatically.
echo Your login session is saved permanently in %USER_DATA_DIR%.
if "%3"=="" (
    pause
)

