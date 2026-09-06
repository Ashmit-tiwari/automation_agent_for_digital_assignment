@echo off
setlocal
echo ================================================================
echo   Launching Brave with Remote Debugging (Existing Profile)
echo   All your open tabs, saved passwords, and logins will be preserved!
echo ================================================================
echo.

:: 1. Close any running Brave window so port can attach to default profile
tasklist /fi "imagename eq brave.exe" 2>nul | find /i "brave.exe" >nul
if not errorlevel 1 (
    echo [*] Closing currently running Brave window...
    taskkill /IM brave.exe /F >nul 2>&1
    timeout /t 2 >nul
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

:: 3. Launch on default profile with remote debugging and restore session
set FLAGS=--remote-debugging-port=9222 --remote-allow-origins=* --restore-last-session --no-default-browser-check
set URLS=https://app.bytexl.ai/courses https://gemini.google.com/app

echo [*] Starting Brave with all your saved logins on port 9222...
start "" "%BRAVE_EXE%" %FLAGS% %URLS%

echo.
echo ================================================================
echo [OK] Brave is ready with Remote Debugging on port 9222!
echo [OK] All your existing sessions and logins are preserved!
echo Now go to http://localhost:5000 and click "Proceed with Brave Browser".
echo ================================================================
timeout /t 5 >nul
