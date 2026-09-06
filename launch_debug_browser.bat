@echo off
set PORT=%1
if "%PORT%"=="" set PORT=9222

echo ========================================================
echo Launching Automation Browser with Remote Debugging
echo Port: %PORT%
echo Profile: %USERPROFILE%\.bytexl_profile
echo ========================================================

set USER_DATA_DIR=%USERPROFILE%\.bytexl_profile
set FLAGS=--remote-debugging-port=%PORT% --remote-allow-origins=* --user-data-dir="%USER_DATA_DIR%" --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes --disable-ipc-flooding-protection --disable-hang-monitor
set URLS=https://www.bytexl.app https://gemini.google.com

:: Check if Brave exists
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

:: Check if Chrome exists
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

:: Check if Microsoft Edge exists
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

echo [!] Could not find Brave, Chrome, or Edge installation.
pause
exit /b 1

:started
echo [✓] Browser launched with remote debugging on port %PORT%.
echo ByteXL and Gemini tabs are opening automatically.
echo Your login session is saved permanently in %USER_DATA_DIR%.
if "%2"=="" (
    pause
)

