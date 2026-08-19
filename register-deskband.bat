@echo off
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

py -3 pydeskband\registrar.py -r
if %errorlevel% neq 0 (
    echo Registrierung fehlgeschlagen.
    pause
    exit /b %errorlevel%
)

echo.
echo Fertig. Aktiviere die Leiste:
echo   Rechtsklick Taskleiste -^> Symbolleisten -^> "Cursor Token Usage"
echo.
pause
