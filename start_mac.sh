#!/usr/bin/env bash
# ==============================================================================
# 模擬国連 議事録支援システム (MUN Minutes Assistant) - macOS 起動スクリプト
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "================================================================"
echo "  模擬国連 議事録支援システム (macOS 起動ランチャー)"
echo "================================================================"

# 1. Python 3 の存在確認
if ! command -v python3 >/dev/null 2>&1; then
    echo "[エラー] python3 が見つかりません。"
    echo "macOS の Command Line Tools (xcode-select --install) または https://www.python.org からインストールしてください。"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "[情報] Python バージョン: $PY_VERSION"

# 2. 仮想環境 (.venv) の自動準備
if [ ! -d ".venv" ]; then
    echo "[準備] 初回起動のため仮想環境 (.venv) を作成しています…"
    python3 -m venv .venv
    echo "[完了] 仮想環境を作成しました。"
fi

# 仮想環境のアクティベート
source .venv/bin/activate

# 2.5. PATH に .runtime/bin を追加
if [ -d "$SCRIPT_DIR/.runtime/bin" ]; then
    export PATH="$SCRIPT_DIR/.runtime/bin:$PATH"
fi

# 3. 外部依存コマンドの事前チェック案内
echo ""
echo "--- 外部コンポーネント確認 ---"

if command -v ffmpeg >/dev/null 2>&1; then
    echo "  ✔ FFmpeg: 検出済み ($(command -v ffmpeg))"
else
    echo "  ⚠ FFmpeg: 未検出 (音声サンプリングに必要です)"
    echo "    -> Homebrew導入推奨: 'brew install ffmpeg'"
fi

if command -v whisper-cli >/dev/null 2>&1; then
    echo "  ✔ whisper-cli: 検出済み ($(command -v whisper-cli))"
else
    echo "  ⚠ whisper-cli: 未検出 (ローカル文字起こしに必要です)"
    echo "    -> Homebrew導入推奨: 'brew install whisper-cpp'"
fi

if curl -s http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "  ✔ Ollama サービス: 稼働中 (http://127.0.0.1:11434)"
else
    echo "  ⚠ Ollama サービス: 未接続 (整文AIに必要です)"
    echo "    -> https://ollama.com よりインストールし、Ollamaアプリを起動してください。"
fi
echo "--------------------------------"
echo "※不足コンポーネントがある場合でも、ブラウザの設定画面から確認・モデルダウンロードが可能です。"
echo ""

# 4. アプリケーションサーバーの起動
echo "[起動] 議事録支援サーバーを起動します…"
python3 web_app.py "$@"
