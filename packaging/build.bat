@echo off
cd /d "%~dp0.."
echo Building CursorTokenUsage.exe ...
py -3 -m PyInstaller --noconfirm --clean packaging\app.spec
if errorlevel 1 exit /b 1
echo.
echo Fertig: dist\CursorTokenUsage.exe
echo Diese Datei kannst du weitergeben. Beim ersten Start erscheint der Installer.
