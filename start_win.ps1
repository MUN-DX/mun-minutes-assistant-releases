# ==============================================================================
# 模擬国連 議事録支援システム (MUN Minutes Assistant) - Windows PowerShell 起動ランチャー
# ==============================================================================
[CmdletBinding()]
param()

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  模擬国連 議事録支援システム (Windows PowerShell ランチャー)" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan

$RuntimeDir = Join-Path $ScriptDir ".runtime"
$BinDir = Join-Path $RuntimeDir "bin"
$PortablePythonDir = Join-Path $RuntimeDir "python"
$PortablePythonExe = Join-Path $PortablePythonDir "python.exe"

# 1. Python 実行環境の検出または自動準備 (Zero-Prerequisites 対応)
$PythonCmd = $null
$PythonArgs = @()

if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCmd = "py"
    $PythonArgs = @("-3")
    Write-Host "[情報] システムの py ランチャーを検出しました。" -ForegroundColor Green
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
    $PythonArgs = @()
    Write-Host "[情報] システムの python を検出しました。" -ForegroundColor Green
} elseif (Test-Path $PortablePythonExe) {
    $PythonCmd = $PortablePythonExe
    $PythonArgs = @()
    Write-Host "[情報] ポータブル Python ランタイムを検出しました: $PortablePythonExe" -ForegroundColor Green
} else {
    Write-Host "[準備] Python が未検出です。公式ポータブル Python 3.11 を自動セットアップします…" -ForegroundColor Yellow
    try {
        New-Item -ItemType Directory -Force -Path $PortablePythonDir | Out-Null
        $ZipPath = Join-Path $RuntimeDir "python-embed.zip"
        $PythonUrl = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
        Write-Host "  -> ダウンロード中: $PythonUrl" -ForegroundColor Gray
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $PythonUrl -OutFile $ZipPath -TimeoutSec 60
        Write-Host "  -> 展開中…" -ForegroundColor Gray
        Expand-Archive -Path $ZipPath -DestinationPath $PortablePythonDir -Force
        Remove-Item -Force $ZipPath
        if (Test-Path $PortablePythonExe) {
            $PythonCmd = $PortablePythonExe
            $PythonArgs = @()
            Write-Host "[完了] ポータブル Python の準備が完了しました。" -ForegroundColor Green
        }
    } catch {
        Write-Host "[警告] ポータブル Python の自動取得に失敗しました: $_" -ForegroundColor Yellow
        Write-Host "Microsoft Store または https://www.python.org から Python 3.9 以上を導入してください。" -ForegroundColor Red
        exit 1
    }
}

# 2. PATH に .runtime/bin を追加
if (Test-Path $BinDir) {
    $env:PATH = "$BinDir;$env:PATH"
}

# 3. 外部コンポーネント状況の確認と表示
Write-Host "`n--- 外部コンポーネント確認 ---" -ForegroundColor Gray

$FfmpegOk = (Get-Command ffmpeg -ErrorAction SilentlyContinue) -ne $null -or (Test-Path (Join-Path $BinDir "ffmpeg.exe"))
if ($FfmpegOk) {
    Write-Host "  ✔ FFmpeg: 利用可能" -ForegroundColor Green
} else {
    Write-Host "  ⚠ FFmpeg: 未検出 (配布ガイドをご覧ください)" -ForegroundColor Yellow
}

$WhisperOk = (Get-Command whisper-cli -ErrorAction SilentlyContinue) -ne $null -or (Test-Path (Join-Path $BinDir "whisper-cli.exe"))
if ($WhisperOk) {
    Write-Host "  ✔ whisper-cli: 利用可能" -ForegroundColor Green
} else {
    Write-Host "  ⚠ whisper-cli: 未検出 (配布ガイドをご覧ください)" -ForegroundColor Yellow
}

$OllamaOk = $false
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2 -ErrorAction Stop
    $OllamaOk = $true
    Write-Host "  ✔ Ollama: サービス稼働中" -ForegroundColor Green
} catch {
    Write-Host "  ⚠ Ollama: サービス未接続 (Web設定画面からインストーラー取得、または https://ollama.com より起動)" -ForegroundColor Yellow
}

Write-Host "--------------------------------" -ForegroundColor Gray
Write-Host "※未導入のコンポーネントがある場合でも、配布ガイドの導入手順をご確認ください。`n" -ForegroundColor Gray

# 4. サーバー起動
Write-Host "[起動] 議事録支援サーバーを起動します…" -ForegroundColor Cyan
& $PythonCmd @PythonArgs web_app.py
