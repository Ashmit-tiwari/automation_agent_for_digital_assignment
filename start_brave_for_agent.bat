@echo off
echo ====================================================================
echo   Launching Brave with ByteXL AI Assistant Integration (Port 9222)
echo ====================================================================
echo.
echo Preserving all existing cookies, logins, and open tabs...
echo.

set FLAGS=--remote-debugging-port=9222 --remote-allow-origins=* --restore-last-session --no-first-run --no-default-browser-check --disable-background-timer-throttling --disable-backgrounding-occluded-windows --disable-renderer-backgrounding --disable-features=CalculateNativeWinOcclusion,IntensiveWakeUpThrottling,ThrottleDisplayNoneAndVisibilityHiddenCrossOriginIframes,ThrottleJavascript --run-all-compositor-stages-before-draw --disable-ipc-flooding-protection --disable-hang-monitor
set URLS=https://app.bytexl.ai/courses

if exist "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" (
    start "" "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" %FLAGS% %URLS%
    goto :done
)
if exist "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" (
    start "" "%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe" %FLAGS% %URLS%
    goto :done
)

echo [!] Could not locate Brave Browser executable.
pause
exit /b 1

:done
echo [OK] Brave is active with debugging port 9222!
echo Open http://localhost:5000 in your browser and click "Proceed with Brave".
timeout /t 3 >nul

