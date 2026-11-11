@echo off
rem ==============================================================================
rem 模擬国連 議事録支援システム (MUN Minutes Assistant) - Windows 起動ランチャー
rem ==============================================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ================================================================
echo   模擬国連 議事録支援システム (Windows 起動ランチャー)
echo ================================================================

set "RUNTIMEDIR=%~dp0.runtime"
set "BINDIR=%RUNTIMEDIR%\bin"
set "PORTABLE_PY=%RUNTIMEDIR%\python\python.exe"

rem 1. Python 実行コマンドの検出
set "PYTHON_CMD="
where py >nul 2>nul
if %ERRORLEVEL% equ 0 (
    set "PYTHON_CMD=py -3"
) else (
    where python >nul 2>nul
    if %ERRORLEVEL% equ 0 (
        set "PYTHON_CMD=python"
    ) else (
        if exist "%PORTABLE_PY%" (
            set "PYTHON_CMD=%PORTABLE_PY%"
        )
    )
)

if "%PYTHON_CMD%"=="" (
    echo [準備] システムに Python が未検出のため、PowerShell経由でポータブル版を自動取得します...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_win.ps1"
    goto :end
)

echo [情報] Python 実行環境: %PYTHON_CMD%

rem 2. PATH に .runtime/bin を一時追加
if exist "%BINDIR%" (
    set "PATH=%BINDIR%;%PATH%"
)

rem 3. 仮想環境 (.venv) が存在する場合はアクティベート
if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

rem 4. 外部コンポーネント事前確認
echo.
echo --- 外部コンポーネント確認 ---
where ffmpeg >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo   [OK] FFmpeg: 利用可能
) else (
    echo   [!] FFmpeg: 未検出 (配布ガイドをご覧ください)
)

where whisper-cli >nul 2>nul
if %ERRORLEVEL% equ 0 (
    echo   [OK] whisper-cli: 利用可能
) else (
    echo   [!] whisper-cli: 未検出 (配布ガイドをご覧ください)
)

echo --------------------------------
echo ※未導入のツールがある場合でも、配布ガイドの導入手順をご確認ください。
echo.

rem 5. サーバー起動
echo [起動] 議事録支援サーバーを起動します...
%PYTHON_CMD% web_app.py %*

:end
pause
