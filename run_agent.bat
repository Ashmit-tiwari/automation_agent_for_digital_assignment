@echo off
title ByteXL Autonomous Agent
cls
echo ======================================================================
echo             BYTE-XL AUTONOMOUS AGENT (COURSE + QUIZ + LABS)
echo ======================================================================
echo.
echo [*] Checking browser and starting agent...
echo [*] Agent will automatically:
echo      1. Connect to Brave or launch with remote debugging.
echo      2. Skip reading topics; target only Quizzes and Labs.
echo      3. Solve MCQs and Coding/SQL Labs with Gemini.
echo      4. Auto-tick side checkboxes on completion!
echo      5. Automatically advance to next submodules and units to 100%%!
echo      6. Fully operational even when browser window is MINIMIZED!
echo.
set TARGET_ARG=%*
if "%TARGET_ARG%"=="" (
    set /p USER_TARGET="[Optional] Enter course/module to target (or press ENTER to auto-detect open course): "
    if not "%USER_TARGET%"=="" set TARGET_ARG=--target "%USER_TARGET%"
)
echo.
python e:\Agent\bytexl_agent.py %TARGET_ARG%
pause
