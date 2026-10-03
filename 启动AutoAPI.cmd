@echo off
setlocal
title AutoAPI
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "AUTOAPI_EXIT_CODE=%ERRORLEVEL%"
if not "%AUTOAPI_EXIT_CODE%"=="0" (
    echo.
    echo AutoAPI failed to start or stopped with an error. Exit code: %AUTOAPI_EXIT_CODE%
    echo Check the error above. Press any key to close this window.
    pause >nul
)
exit /b %AUTOAPI_EXIT_CODE%
