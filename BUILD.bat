@echo off
REM BUILD.bat
REM ---------
REM Build the full Garage/Repair POS desktop application (PyInstaller).
REM Produces: dist\GaragePOS\  and  dist\scanner_bridge\
REM
REM Prerequisites:
REM   pip install pyinstaller requests flask pywebview
REM   pip install -r requirements_bridge.txt
REM
REM Usage:  BUILD.bat

setlocal EnableDelayedExpansion

echo =============================================================
echo  Garage / Repair POS  –  Production Build
echo =============================================================
echo.

REM ── ensure we are in the repo root ─────────────────────────────────────────
cd /d "%~dp0"

REM ── activate virtual environment if present ────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    echo [BUILD] Activating virtual environment ...
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM ── install / update bridge dependencies ──────────────────────────────────
echo [BUILD] Installing bridge dependencies ...
pip install -q -r requirements_bridge.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    exit /b 1
)

REM ── syntax check ──────────────────────────────────────────────────────────
echo [BUILD] Syntax checking ...
python -m py_compile scanner_bridge.py        || goto :syntax_error
python -m py_compile scanner_launcher.py      || goto :syntax_error
python -m py_compile workshop_usage_routes.py || goto :syntax_error
python -m py_compile desktop.py               || goto :syntax_error
python -m py_compile desktop_runtime.py       || goto :syntax_error
python -m py_compile launcher.py              || goto :syntax_error
echo [BUILD] Syntax OK.

REM ── clean previous build ──────────────────────────────────────────────────
echo [BUILD] Cleaning previous build output ...
if exist "dist\GaragePOS"      rmdir /s /q "dist\GaragePOS"
if exist "dist\scanner_bridge" rmdir /s /q "dist\scanner_bridge"
if exist "build"               rmdir /s /q "build"

REM ── build scanner bridge sidecar ──────────────────────────────────────────
echo [BUILD] Building scanner_bridge sidecar ...
pyinstaller --noconfirm scanner_bridge.spec
if errorlevel 1 (
    echo [ERROR] scanner_bridge build failed.
    exit /b 1
)
echo [BUILD] scanner_bridge built OK.

REM ── build main desktop application ────────────────────────────────────────
echo [BUILD] Building GaragePOS desktop application ...
pyinstaller --noconfirm desktop_app.spec
if errorlevel 1 (
    echo [ERROR] GaragePOS build failed.
    exit /b 1
)
echo [BUILD] GaragePOS built OK.

REM ── copy scanner_bridge sidecar into GaragePOS distribution ───────────────
echo [BUILD] Copying scanner_bridge into GaragePOS dist ...
xcopy /s /y "dist\scanner_bridge\*" "dist\GaragePOS\" >nul
if errorlevel 1 (
    echo [WARNING] Could not copy scanner_bridge sidecar.
)

REM ── ensure config and logs dirs exist in dist ─────────────────────────────
if not exist "dist\GaragePOS\config" mkdir "dist\GaragePOS\config"
if not exist "dist\GaragePOS\logs"   mkdir "dist\GaragePOS\logs"
copy /y "config\scanner_devices.json" "dist\GaragePOS\config\" >nul

echo.
echo =============================================================
echo  Build complete:  dist\GaragePOS\GaragePOS.exe
echo =============================================================
echo.
echo  Next step: run build_installer.bat to create the Inno Setup installer.
echo.
goto :eof

:syntax_error
echo [ERROR] Syntax check failed.  Fix the error above then re-run BUILD.bat.
exit /b 1
