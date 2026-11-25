@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Camoufox Antidetect Pro

set PYTHON_CMD=
if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
) else if exist "..\.venv\Scripts\python.exe" (
    set "PYTHON_CMD=..\.venv\Scripts\python.exe"
) else (
    echo [!] Chua tim thay moi truong cai dat .venv.
    echo Dang tu dong chay file cai dat Cai_Dat_Windows.bat...
    call Cai_Dat_Windows.bat
    if exist ".venv\Scripts\python.exe" (
        set "PYTHON_CMD=.venv\Scripts\python.exe"
    )
)

if "%PYTHON_CMD%"=="" (
    echo [LOI] Khong tim thay moi truong Python .venv!
    pause
    exit /b 1
)

echo [*] Dang khoi dong Camoufox Antidetect Studio...
"%PYTHON_CMD%" app.py
if %errorlevel% neq 0 (
    echo.
    echo [LOI] Co loi xay ra khi chay ung dung. Nhan phim bat ky de thoat...
    pause >nul
)
