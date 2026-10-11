@echo off
setlocal
echo ================================================================
echo   Launching Brave with Remote Debugging (Existing Profile)
echo   All your open tabs, saved passwords, and logins will be preserved!
echo ================================================================
echo.

:: 1. Check if port 9222 is ALREADY active with Remote Debugging
netstat -ano | findstr /R /C:":9222 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo [OK] Brave is ALREADY active with Remote Debugging on port 9222!
    echo [OK] Your existing profile, open tabs, and ByteXL login are ready!
    echo.
    echo Now open http://localhost:5000 and click "Proceed with Brave Browser".
    timeout /t 4 >nul
    exit /b 0
)

:: 2. Find Brave executable
set BRAVE_EXE=
if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    set BRAVE_EXE=C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe
) else if exist "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" (
    set BRAVE_EXE=%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe
) else if exist "C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe" (
    set BRAVE_EXE=C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe
)

if "%BRAVE_EXE%"=="" (
    echo [!] Could not locate Brave Browser installation!
    pause
    exit /b 1
)

:: 3. If Brave is running without port 9222, gracefully save and close
tasklist /fi "imagename eq brave.exe" 2>nul | find /i "brave.exe" >nul
if not errorlevel 1 (
    echo [*] Brave is currently running without remote debugging.
    echo [*] Gracefully closing Brave so all tabs and session data are saved...
    powershell -Command "Get-Process brave -ErrorAction SilentlyContinue | ForEach-Object { $_.CloseMainWindow() }" >nul 2>&1
    timeout /t 3 >nul
    tasklist /fi "imagename eq brave.exe" 2>nul | find /i "brave.exe" >nul
    if not errorlevel 1 (
        echo [*] Finishing background exit...
        taskkill /IM brave.exe >nul 2>&1
        timeout /t 2 >nul
    )
)

:: 4. Launch on default profile with remote debugging and restore session
set FLAGS=--remote-debugging-port=9222 --remote-allow-origins=* --restore-last-session --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes,ThrottleJavascript --run-all-compositor-stages-before-draw --disable-ipc-flooding-protection --disable-hang-monitor
set URLS=https://app.bytexl.ai/courses https://gemini.google.com/app

echo [*] Starting Brave with all your saved logins on port 9222...
start "" "%BRAVE_EXE%" %FLAGS% %URLS%

echo.
echo ================================================================
echo [OK] Brave is ready with Remote Debugging on port 9222!
echo [OK] All your existing sessions and logins are preserved!
echo Now go to http://localhost:5000 and click "Proceed with Brave Browser".
echo ================================================================
timeout /t 4 >nul
