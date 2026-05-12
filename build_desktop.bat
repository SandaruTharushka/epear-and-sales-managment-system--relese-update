@echo off
REM build_desktop.bat
REM -----------------
REM Quick desktop-only rebuild (skips the scanner_bridge sidecar).
REM Use when you only changed Python/Flask/frontend files.
REM For a full rebuild (including scanner bridge), run BUILD.bat instead.

setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" call .venv\Scripts\activate.bat

echo [BUILD] Syntax checking desktop files ...
python -m py_compile desktop.py         || goto :err
python -m py_compile desktop_runtime.py || goto :err
python -m py_compile launcher.py        || goto :err
echo [BUILD] Syntax OK.

echo [BUILD] Building GaragePOS ...
if exist "dist\GaragePOS" rmdir /s /q "dist\GaragePOS"
pyinstaller --noconfirm desktop_app.spec
if errorlevel 1 goto :err

REM Re-inject scanner sidecar if it was built previously
if exist "dist\scanner_bridge\scanner_bridge.exe" (
    echo [BUILD] Re-copying scanner_bridge sidecar ...
    xcopy /s /y "dist\scanner_bridge\*" "dist\GaragePOS\" >nul
)

if not exist "dist\GaragePOS\config" mkdir "dist\GaragePOS\config"
if not exist "dist\GaragePOS\logs"   mkdir "dist\GaragePOS\logs"
copy /y "config\scanner_devices.json" "dist\GaragePOS\config\" >nul

echo [BUILD] Done: dist\GaragePOS\GaragePOS.exe
goto :eof

:err
echo [ERROR] Build failed.
exit /b 1
