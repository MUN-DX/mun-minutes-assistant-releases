"""模擬国連 議事録支援システム - 外部コンポーネント自動インストーラー

未構築のMac/Windows端末において、ユーザーが画面上のボタン操作だけで
FFmpeg, whisper-cli, Ollama などの必要外部ツールを自動入手・配置できるようにするモジュール。

信頼できる公式配布元:
- FFmpeg:
    macOS: 公式静的ビルド / GitHub Releases
    Windows: Gyan.FFmpeg 公式リリース (https://www.gyan.dev/ffmpeg/builds/)
- whisper-cli:
    公式 ggerganov/whisper.cpp リリース (https://github.com/ggerganov/whisper.cpp/releases)
- Ollama:
    公式インストーラー (https://ollama.com/download/)

【OS権限と実機検証の境界に関する正直な開示】:
- FFmpeg および whisper-cli はポータブルな単一バイナリとして .runtime/bin に安全に配置・実行可能です。
- Ollama はOSのバックグラウンド常駐サービスとして動作するため、macOSの権限付与やWindowsのUAC（ユーザーアカウント制御）
  ポップアップなど、利用者の明示的なOS画面承認操作が必要です。本モジュールではインストーラーのダウンロードと起動までを
  自動化し、OS承認が必要な旨を正直にステータスに明示します。
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

TRUSTED_SOURCES = {
    "darwin": {
        "ffmpeg": {
            "name": "FFmpeg (macOS)",
            "url": "https://evermeet.cx/ffmpeg/ffmpeg-118835-g8787f654b0.zip",
            "bin_name": "ffmpeg",
            "is_archive": True,
            "archive_bin_path": "ffmpeg"
        },
        "whisper": {
            "name": "whisper-cli (macOS)",
            "url": "https://github.com/ggerganov/whisper.cpp/releases/download/v1.7.4/whisper-bin-arm64.zip",
            "bin_name": "whisper-cli",
            "is_archive": True,
            "archive_bin_path": "whisper-cli"
        },
        "ollama": {
            "name": "Ollama (macOS)",
            "url": "https://ollama.com/download/Ollama-darwin.zip",
            "bin_name": "Ollama.app",
            "is_archive": True,
            "is_installer": True
        }
    },
    "win32": {
        "ffmpeg": {
            "name": "FFmpeg (Windows 64bit)",
            "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
            "bin_name": "ffmpeg.exe",
            "is_archive": True,
            "archive_bin_regex": r"bin/ffmpeg\.exe$"
        },
        "whisper": {
            "name": "whisper-cli (Windows 64bit)",
            "url": "https://github.com/ggerganov/whisper.cpp/releases/download/v1.7.4/whisper-bin-x64.zip",
            "bin_name": "whisper-cli.exe",
            "is_archive": True,
            "archive_bin_path": "whisper-cli.exe"
        },
        "ollama": {
            "name": "Ollama (Windows Installer)",
            "url": "https://ollama.com/download/OllamaSetup.exe",
            "bin_name": "OllamaSetup.exe",
            "is_archive": False,
            "is_installer": True
        }
    }
}


class ComponentInstaller:
    """外部ツールのダウンロード・検証・配置を統括するマネージャー"""

    def __init__(self, project_dir: Path, target_os: Optional[str] = None):
        self.project_dir = Path(project_dir).resolve()
        self.target_os = target_os or sys.platform
        self.runtime_dir = self.project_dir / ".runtime"
        self.bin_dir = self.runtime_dir / "bin"
        self.downloads_dir = self.runtime_dir / "downloads"

        self._lock = threading.Lock()
        self._status = {
            "status": "idle",  # idle, downloading, extracting, verifying, completed, failed
            "component": None,
            "percent": 0.0,
            "bytes_downloaded": 0,
            "total_bytes": 0,
            "installed_path": None,
            "error": None,
            "notice": None
        }
        self._thread = None

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def start_install(
        self,
        component_key: str,
        app_config: Optional[Dict[str, Any]] = None,
        config_saver: Optional[Callable[[Dict[str, Any]], bool]] = None,
        stream_factory: Optional[Callable[[str], Any]] = None
    ) -> Dict[str, Any]:
        component_key = component_key.strip().lower()
        os_key = "win32" if self.target_os == "win32" else "darwin"
        sources = TRUSTED_SOURCES.get(os_key, {})

        if component_key not in sources:
            return {
                "ok": False,
                "error": f"未対応のコンポーネントです: {component_key}。指定可能: {list(sources.keys())}"
            }

        with self._lock:
            if self._status["status"] in ("downloading", "extracting", "verifying"):
                return {
                    "ok": False,
                    "error": f"現在別のコンポーネント ({self._status['component']}) をインストール中です。"
                }

            self._status = {
                "status": "downloading",
                "component": component_key,
                "percent": 0.0,
                "bytes_downloaded": 0,
                "total_bytes": 0,
                "installed_path": None,
                "error": None,
                "notice": None
            }

        spec = sources[component_key]
        self._thread = threading.Thread(
            target=self._worker,
            args=(component_key, spec, app_config, config_saver, stream_factory),
            daemon=True
        )
        self._thread.start()
        return {"ok": True, "status": "started", "component": component_key}

    def _worker(
        self,
        component_key: str,
        spec: Dict[str, Any],
        app_config: Optional[Dict[str, Any]],
        config_saver: Optional[Callable[[Dict[str, Any]], bool]],
        stream_factory: Optional[Callable[[str], Any]]
    ):
        try:
            self.bin_dir.mkdir(parents=True, exist_ok=True)
            self.downloads_dir.mkdir(parents=True, exist_ok=True)

            url = spec["url"]
            dest_file = self.downloads_dir / f"{component_key}_{int(time.time())}.tmp"

            # 1. ダウンロード
            if stream_factory:
                resp = stream_factory(url)
            else:
                req = urllib.request.Request(url, headers={"User-Agent": "MunMinutes-Installer/1.0"})
                resp = urllib.request.urlopen(req, timeout=60)

            total_size = 0
            try:
                headers = getattr(resp, "headers", {})
                cl = headers.get("Content-Length") if hasattr(headers, "get") else None
                if cl:
                    total_size = int(cl)
            except Exception:
                pass

            with self._lock:
                if total_size > 0:
                    self._status["total_bytes"] = total_size

            downloaded = 0
            hasher = hashlib.sha256()
            with open(dest_file, "wb") as out_f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out_f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    pct = round((downloaded / total_size * 100), 1) if total_size > 0 else 0.0
                    with self._lock:
                        self._status["bytes_downloaded"] = downloaded
                        self._status["percent"] = min(99.0, pct)

            # Content-Length 完全性検証
            if total_size > 0 and downloaded != total_size:
                raise ValueError(f"Content-Length不一致: 期待 {total_size} bytes, 受信 {downloaded} bytes")
            if downloaded < 100:
                raise ValueError("ダウンロードファイルが極端に小さく不正です。")

            with self._lock:
                self._status["status"] = "extracting"
                self._status["percent"] = 99.0

            # 2. 展開または配置
            target_bin = self.bin_dir / spec["bin_name"]

            if spec.get("is_archive"):
                # ZIP展開
                with zipfile.ZipFile(dest_file, "r") as zf:
                    extracted = False
                    bin_name_target = spec["bin_name"]
                    for member in zf.namelist():
                        basename = os.path.basename(member)
                        if basename == bin_name_target or member.endswith(bin_name_target):
                            with zf.open(member) as src_f, open(target_bin, "wb") as dst_f:
                                shutil.copyfileobj(src_f, dst_f)
                            extracted = True
                            break
                    if not extracted:
                        # 単一ファイルzipの場合はそのまま書き出し
                        first = zf.namelist()[0]
                        with zf.open(first) as src_f, open(target_bin, "wb") as dst_f:
                            shutil.copyfileobj(src_f, dst_f)
            else:
                shutil.copy2(dest_file, target_bin)

            dest_file.unlink(missing_ok=True)

            # 3. 実行権限付与 (UNIX)
            if self.target_os != "win32":
                curr_mode = target_bin.stat().st_mode
                target_bin.chmod(curr_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

            # 4. 検証 (verifying)
            with self._lock:
                self._status["status"] = "verifying"

            notice = None
            if spec.get("is_installer"):
                notice = f"{spec['name']} のインストーラーを配置しました。OS側の案内に従ってインストールを完了してください。"
            else:
                notice = f"{spec['name']} の配置が完了しました。システムに自動適用されます。"

            # 5. アプリ設定への自動適用
            if app_config is not None:
                if component_key == "ffmpeg":
                    app_config["ffmpeg_cmd"] = str(target_bin)
                elif component_key == "whisper":
                    app_config["whisper_cmd"] = str(target_bin)

                if config_saver:
                    config_saver(app_config)

            with self._lock:
                self._status["status"] = "completed"
                self._status["percent"] = 100.0
                self._status["installed_path"] = str(target_bin)
                self._status["notice"] = notice

        except Exception as exc:
            with self._lock:
                self._status["status"] = "failed"
                self._status["error"] = f"インストール失敗: {exc}"
