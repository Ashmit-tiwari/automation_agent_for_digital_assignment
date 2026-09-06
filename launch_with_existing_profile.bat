@echo off
set PORT=%1
if "%PORT%"=="" set PORT=9222
set PREF=%2

echo ================================================================
echo   Launch Browser with EXISTING Profile (All Logins Preserved)
echo ================================================================
echo Port: %PORT%
echo Mode: Native Default Profile (Uses your existing cookies and logins)
echo.

set FLAGS=--remote-debugging-port=%PORT% --remote-allow-origins=* --restore-last-session --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes --disable-ipc-flooding-protection --disable-hang-monitor
set URLS=https://app.bytexl.ai/courses https://gemini.google.com/app

:: Auto-detect browser or use preference
if /i "%PREF%"=="chrome" goto :try_chrome
if /i "%PREF%"=="edge" goto :try_edge
if /i "%PREF%"=="brave" goto :try_brave

tasklist /fi "imagename eq brave.exe" 2>nul | find /i "brave.exe" >nul
if not errorlevel 1 (
    echo [*] Detected Brave currently running.
    set BROWSER_EXE_NAME=brave.exe
    goto :try_brave
)

tasklist /fi "imagename eq chrome.exe" 2>nul | find /i "chrome.exe" >nul
if not errorlevel 1 (
    echo [*] Detected Google Chrome currently running.
    set BROWSER_EXE_NAME=chrome.exe
    goto :try_chrome
)

tasklist /fi "imagename eq msedge.exe" 2>nul | find /i "msedge.exe" >nul
if not errorlevel 1 (
    echo [*] Detected Microsoft Edge currently running.
    set BROWSER_EXE_NAME=msedge.exe
    goto :try_edge
)

:: If none running, try Brave -> Chrome -> Edge
:try_brave
if exist "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" (
    set TARGET_EXE=%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe
    set BNAME=Brave
    goto :check_running
)
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    set TARGET_EXE=C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe
    set BNAME=Brave
    goto :check_running
)
if /i "%PREF%"=="brave" goto :not_found

:try_chrome
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    set TARGET_EXE=C:\Program Files\Google\Chrome\Application\chrome.exe
    set BNAME=Chrome
    goto :check_running
)
if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
    set TARGET_EXE=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe
    set BNAME=Chrome
    goto :check_running
)
if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    set TARGET_EXE=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe
    set BNAME=Chrome
    goto :check_running
)
if /i "%PREF%"=="chrome" goto :not_found

:try_edge
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    set TARGET_EXE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe
    set BNAME=Edge
    goto :check_running
)
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" (
    set TARGET_EXE=C:\Program Files\Microsoft\Edge\Application\msedge.exe
    set BNAME=Edge
    goto :check_running
)
if /i "%PREF%"=="edge" goto :not_found

:not_found
echo [!] Could not locate installed browser.
pause
exit /b 1

:check_running
echo Selected Browser: %BNAME% (%TARGET_EXE%)
echo.
if not "%BROWSER_EXE_NAME%"=="" (
    echo [*] Closing existing %BNAME% window so remote debugging can attach to your default profile...
    taskkill /IM "%BROWSER_EXE_NAME%" /F >nul 2>&1
    timeout /t 2 >nul
)

echo [*] Launching %BNAME% with remote debugging on port %PORT%...
start "" "%TARGET_EXE%" %FLAGS% %URLS%

echo.
echo ================================================================
echo [OK] %BNAME% launched with remote debugging on port %PORT%!
echo [OK] All your existing logins (ByteXL, Gemini, Google) and open tabs are preserved!
echo Now switch back to http://localhost:5000 and click "Proceed with %BNAME%".
echo ================================================================
timeout /t 4 >nul
