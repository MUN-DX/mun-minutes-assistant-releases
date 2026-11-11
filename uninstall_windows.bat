@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 uninstall_cleanup.py
) else (
    if exist ".runtime\python\python.exe" (
        ".runtime\python\python.exe" uninstall_cleanup.py
    ) else (
        echo Python が見つかりません。ガイドの手順でフォルダを削除してください。
    )
)
pause
