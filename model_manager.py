"""模擬国連 議事録支援システム - モデル管理・安全ダウンロードモジュール"""
import json
import http.client
import logging
import socket
import ssl
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import mun_minutes

# ==========================================
# 1. モデルカタログ定義
# ==========================================

import hashlib

logger = logging.getLogger(__name__)

WHISPER_MODELS_CATALOG = [
    {
        "filename": "ggml-large-v3-turbo.bin",
        "name": "Large-v3-Turbo (FP16)",
        "size_mb": 1620.0,
        "expected_bytes": 1624555275,
        "sha256": "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin",
        "description": "Turbo の量子化していないモデル。使用するメモリや精度は環境によって異なります。"
    },
    {
        "filename": "ggml-large-v3-turbo-q5_0.bin",
        "name": "Large-v3-Turbo Q5_0 (軽量・高速)",
        "size_mb": 547.4,
        "expected_bytes": 574041195,
        "sha256": "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        "description": "高速な多言語モデル。精度は音声や録音環境によって異なります。"
    },
    {
        "filename": "ggml-large-v3-q5_0.bin",
        "name": "Large-v3 Q5_0 (精度比較用)",
        "size_mb": 1031.1,
        "expected_bytes": 1081140203,
        "sha256": "d75795ecff3f83b5faa89d1900604ad8c780abd5739fae406de19f23ecd98ad1",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-q5_0.bin",
        "description": "Turbo と比較できる大型モデル。処理速度と精度はお使いの環境で確認してください。"
    },
    {
        "filename": "ggml-medium.bin",
        "name": "Medium (標準・中容量)",
        "size_mb": 1530.0,
        "expected_bytes": 1533763059,
        "sha256": "6c14d5adee5f86394037b4e4e8b59f1673b6cee10e3cf0b11bbdbee79c156208",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin",
        "description": "標準精度モデル。"
    },
    {
        "filename": "ggml-small.bin",
        "name": "Small (軽量・低負荷)",
        "size_mb": 488.0,
        "expected_bytes": 487601967,
        "sha256": "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        "description": "低スペックPC・省メモリ環境向け。"
    },
    {
        "filename": "ggml-base.bin",
        "name": "Base (超軽量・動作確認用)",
        "size_mb": 148.0,
        "expected_bytes": 147951465,
        "sha256": "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        "description": "クイックテスト用。精度は控えめ。"
    },
    {
        "filename": "ggml-tiny.bin",
        "name": "Tiny (最小・最速)",
        "size_mb": 75.0,
        "expected_bytes": 77691713,
        "sha256": "be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
        "description": "最小サイズモデル。"
    }
]

OLLAMA_MODELS_CATALOG = [
    {
        "model": "qwen3:4b-instruct-2507-q4_K_M",
        "name": "Qwen3 4B Instruct (現行デフォルト)",
        "size_gb": 2.5,
        "description": "既存設定のカスタム検証モデル。"
    },
    {
        "model": "hf.co/dahara1/Qwen3.5-4B-UD-japanese-imatrix:UD-Q4_K_XL",
        "name": "Qwen3.5 4B 日本語向け (比較用)",
        "size_gb": 2.91,
        "description": "日本語を考慮して軽量化したモデル。整文品質・速度は環境により異なります。"
    },
    {
        "model": "qwen2.5:7b",
        "name": "Qwen 2.5 7B",
        "size_gb": 4.7,
        "description": "Qwen 2.5 系の整文モデル。性能と速度は環境によって異なります。"
    },
    {
        "model": "qwen2.5:3b",
        "name": "Qwen 2.5 3B (軽量・高速)",
        "size_gb": 2.0,
        "description": "軽量モデル。精度と動作速度は環境によって異なります。"
    },
    {
        "model": "qwen2.5:14b",
        "name": "Qwen 2.5 14B (大容量)",
        "size_gb": 9.0,
        "description": "強力な推論能力。16GB以上のRAMを推奨。"
    },
    {
        "model": "qwen2.5:1.5b",
        "name": "Qwen 2.5 1.5B (超軽量)",
        "size_gb": 1.0,
        "description": "動作確認やリソース制約のある環境向け。"
    }
]


def sanitize_filename(filename: str) -> str:
    """パストラバーサルを防ぐためファイル名を安全に検証・サニタイズします。"""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise ValueError("無効なファイル名です。")
    cleaned = filename.strip()
    if not cleaned.endswith(".bin"):
        raise ValueError("Whisperモデルファイル名は .bin で終わる必要があります。")
    return cleaned


# ==========================================
# 2. Whisperモデルダウンロードマネージャー
# ==========================================

class WhisperDownloadManager:
    """
    Whisper GGUF/ggml モデルの安全な非同期ダウンローダー。
    - .part ファイルへの一時書き込み
    - ダウンロード完了後のアトミックリネーム
    - 中断時のクリーンアップ
    - 既存ファイルの保護（確認なしの上書き防止）
    - 進行状況（バイト数、パーセント、速度）の追跡
    """
    DOWNLOAD_TIMEOUT = 60
    MAX_RETRIES = 3
    RETRY_BACKOFF = 1

    def __init__(self):
        self._lock = threading.Lock()
        self._status = {
            "status": "idle",  # idle, downloading, verifying, completed, failed, cancelled
            "filename": None,
            "bytes_downloaded": 0,
            "total_bytes": 0,
            "percent": 0.0,
            "speed_kbps": 0.0,
            "error": None,
            "message": "",
            "start_time": None,
            "completed_time": None
        }
        self._cancel_event = threading.Event()
        self._thread = None

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def cancel(self) -> Dict[str, Any]:
        with self._lock:
            if self._status["status"] in ("downloading", "verifying"):
                self._cancel_event.set()
                self._status["status"] = "cancelling"
                return {"ok": True, "message": "キャンセルを受け付けました。停止まで少し時間がかかる場合があります。"}
            return {"ok": False, "error": "現在実行中のダウンロードはありません。"}

    def start_download(
        self,
        filename: str,
        models_dir: Path,
        catalog: Optional[List[Dict[str, Any]]] = None,
        url_override: Optional[str] = None,
        stream_factory: Optional[Callable[[str], Any]] = None,
        force_overwrite: bool = False
    ) -> Dict[str, Any]:
        safe_name = sanitize_filename(filename)
        models_dir = Path(models_dir).resolve()
        models_dir.mkdir(parents=True, exist_ok=True)

        target_file = models_dir / safe_name
        if target_file.is_file() and not force_overwrite:
            return {
                "ok": True,
                "already_exists": True,
                "filename": safe_name,
                "path": str(target_file),
                "message": f"モデル {safe_name} は既に存在します。上書きは行いません。"
            }

        # URL・期待サイズ・SHA256ハッシュ特定
        download_url = url_override
        expected_size = None
        expected_sha256 = None
        cat = catalog or WHISPER_MODELS_CATALOG
        for item in cat:
            if item.get("filename") == safe_name:
                if not download_url:
                    download_url = item.get("url")
                expected_size = item.get("expected_bytes") or int(item.get("size_mb", 0) * 1024 * 1024)
                expected_sha256 = item.get("sha256")
                break
        if not download_url:
            download_url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{safe_name}"

        with self._lock:
            if (self._status["status"] in ("downloading", "verifying", "cancelling")
                    or (self._thread is not None and self._thread.is_alive())):
                return {"ok": False, "error": "現在別のダウンロードが実行中です。完了またはキャンセルを待ってください。"}

            self._cancel_event.clear()
            self._status = {
                "status": "downloading",
                "filename": safe_name,
                "bytes_downloaded": 0,
                "total_bytes": expected_size or 0,
                "percent": 0.0,
                "speed_kbps": 0.0,
                "error": None,
                "message": "",
                "start_time": time.time(),
                "completed_time": None
            }

        self._thread = threading.Thread(
            target=self._download_worker,
            args=(download_url, safe_name, models_dir, target_file, stream_factory, expected_sha256, expected_size),
            daemon=True
        )
        self._thread.start()
        return {"ok": True, "status": "started", "filename": safe_name}

    def _download_worker(
        self,
        url: str,
        filename: str,
        models_dir: Path,
        target_file: Path,
        stream_factory: Optional[Callable[[str], Any]],
        expected_sha256: Optional[str] = None,
        expected_size: Optional[int] = None
    ):
        part_file = models_dir / f"{filename}.part"
        network_errors = (TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, urllib.error.URLError,
                          http.client.IncompleteRead)
        try:
            start_t = time.time()
            for attempt in range(self.MAX_RETRIES + 1):
                resp = None
                try:
                    if self._cancel_event.is_set():
                        raise InterruptedError("ユーザーによりキャンセルされました。")
                    # 再開は全体のハッシュを検証できるモデルだけに限定する。
                    downloaded = part_file.stat().st_size if expected_sha256 and part_file.exists() else 0
                    if expected_size and downloaded > expected_size:
                        part_file.unlink()
                        downloaded = 0
                    hasher = hashlib.sha256()
                    if downloaded:
                        with part_file.open("rb") as existing:
                            while chunk := existing.read(1024 * 1024):
                                hasher.update(chunk)
                    if expected_size and downloaded == expected_size:
                        total_size = expected_size
                        break

                    request_headers = {"User-Agent": "MunMinutes-ModelManager/1.0"}
                    if downloaded:
                        request_headers["Range"] = f"bytes={downloaded}-"
                    req = urllib.request.Request(url, headers=request_headers)
                    resp = stream_factory(url) if stream_factory else urllib.request.urlopen(
                        req, timeout=self.DOWNLOAD_TIMEOUT)
                    headers = getattr(resp, "headers", {})
                    status = getattr(resp, "status", 200)
                    if status == 206:
                        content_range = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", headers.get("Content-Range", ""))
                        if (not content_range or int(content_range[1]) != downloaded
                                or int(content_range[2]) < downloaded
                                or int(content_range[2]) + 1 != int(content_range[3])):
                            raise ValueError("Content-Rangeが不正なため、安全に再開できません。")
                        total_size = int(content_range[3])
                        if headers.get("Content-Length") and int(headers["Content-Length"]) != total_size - downloaded:
                            raise ValueError("Content-LengthとContent-Rangeが一致しません。")
                    elif status == 200:
                        # Rangeが無視された場合は、応答を追記せず最初から取り直す。
                        downloaded = 0
                        hasher = hashlib.sha256()
                        total_size = int(headers.get("Content-Length") or expected_size or 0)
                    else:
                        raise ValueError(f"モデル取得のHTTP応答が不正です: {status}")
                    if expected_size and total_size and total_size != expected_size:
                        raise ValueError("Content-Lengthがカタログのモデルサイズと一致しません。")
                    with self._lock:
                        self._status["total_bytes"] = total_size
                        self._status["message"] = ""
                    read = getattr(resp, "read1", resp.read)
                    with part_file.open("ab" if downloaded else "wb") as out_f:
                        while True:
                            if self._cancel_event.is_set():
                                raise InterruptedError("ユーザーによりキャンセルされました。")
                            chunk = read(65536)
                            if self._cancel_event.is_set():
                                raise InterruptedError("ユーザーによりキャンセルされました。")
                            if not chunk:
                                break
                            out_f.write(chunk)
                            hasher.update(chunk)
                            downloaded += len(chunk)
                            elapsed = max(0.001, time.time() - start_t)
                            pct = round(downloaded / total_size * 100, 1) if total_size else 0.0
                            with self._lock:
                                self._status["bytes_downloaded"] = downloaded
                                self._status["percent"] = min(100.0, pct)
                                self._status["speed_kbps"] = round(downloaded / 1024 / elapsed, 1)
                    break
                except network_errors as exc:
                    if isinstance(exc, urllib.error.HTTPError) and exc.code not in (408, 429, 500, 502, 503, 504):
                        raise ValueError(f"モデル配布先のHTTPエラー: {exc.code}") from exc
                    if attempt == self.MAX_RETRIES:
                        raise
                    logger.warning("Whisper download retry %s/%s (%s, %s bytes received): %s",
                                   attempt + 1, self.MAX_RETRIES, filename,
                                   part_file.stat().st_size if part_file.exists() else 0, exc)
                    with self._lock:
                        self._status["message"] = f"通信を再試行しています（{attempt + 1}/{self.MAX_RETRIES}）"
                    if self._cancel_event.wait(self.RETRY_BACKOFF * min(2 ** attempt, 8)):
                        raise InterruptedError("ユーザーによりキャンセルされました。")
                finally:
                    if resp is not None:
                        getattr(resp, "close", lambda: None)()

            # 整合性検証: 1. Content-Length の厳格一致確認
            if total_size > 0 and downloaded != total_size:
                raise ValueError(f"Content-Lengthサイズ不一致: 期待 {total_size} bytes, 受信 {downloaded} bytes")

            # 整合性検証: 2. 最小サイズチェック（エラーページHTML等の遮断）
            if downloaded < 1024:
                raise ValueError(f"ダウンロードデータが不正または破損しています ({downloaded} bytes)。")

            # 整合性検証: 3. SHA256 ハッシュ完全性検証
            actual_sha256 = hasher.hexdigest().lower()
            if expected_sha256:
                if actual_sha256 != expected_sha256.lower():
                    raise ValueError(f"SHA256ハッシュ不一致: 期待値 {expected_sha256}, 受信データ {actual_sha256}")

            with self._lock:
                self._status["status"] = "verifying"
                self._status["sha256"] = actual_sha256
            if self._cancel_event.is_set():
                raise InterruptedError("ユーザーによりキャンセルされました。")

            # アトミックリネーム
            part_file.replace(target_file)

            with self._lock:
                self._status["status"] = "completed"
                self._status["percent"] = 100.0
                self._status["completed_time"] = time.time()
                self._status["message"] = ""

        except InterruptedError:
            if not expected_sha256 and part_file.exists():
                part_file.unlink(missing_ok=True)
            with self._lock:
                self._status["status"] = "cancelled"
                self._status["message"] = "取得済みの部分を保存しました。次回のダウンロードで再開します。" if expected_sha256 and part_file.exists() else ""
        except network_errors:
            logger.exception("Whisper model download interrupted: %s", filename)
            if not expected_sha256 and part_file.exists():
                part_file.unlink(missing_ok=True)
            with self._lock:
                self._status["status"] = "failed"
                self._status["message"] = ""
                self._status["error"] = "モデル配布先との通信が途切れました。接続を確認して、もう一度ダウンロードを押してください。取得済みの部分から再開します。" if expected_sha256 else "モデル配布先との通信が途切れました。接続を確認して再試行してください。"
        except Exception as exc:
            logger.exception("Whisper model download failed validation: %s", filename)
            if part_file.exists():
                part_file.unlink(missing_ok=True)
            with self._lock:
                self._status["status"] = "failed"
                self._status["error"] = str(exc)
                self._status["message"] = ""


# ==========================================
# 3. Ollama モデル Pull マネージャー
# ==========================================

class OllamaPullManager:
    """
    Ollama ローカルサービスに対する安全なモデル Pull マネージャー。
    - 既存Ollamaサービスの保護（他モデルを一切削除しない）
    - loopback限定通信
    - ストリーミング進捗のリアルタイム可視化
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._status = {
            "status": "idle",  # idle, pulling, completed, failed, cancelled
            "model": None,
            "completed_bytes": 0,
            "total_bytes": 0,
            "percent": 0.0,
            "current_status": None,
            "error": None,
            "start_time": None,
            "completed_time": None
        }
        self._cancel_event = threading.Event()
        self._thread = None

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def cancel(self) -> Dict[str, Any]:
        with self._lock:
            if self._status["status"] == "pulling":
                self._cancel_event.set()
                self._status["status"] = "cancelled"
                return {"ok": True, "message": "Pullをキャンセルしました。"}
            return {"ok": False, "error": "現在実行中のPullはありません。"}

    def start_pull(
        self,
        model_name: str,
        ollama_url: str = "http://127.0.0.1:11434",
        stream_factory: Optional[Callable[[str, bytes], Any]] = None
    ) -> Dict[str, Any]:
        model_name = model_name.strip()
        if not model_name or re.search(r"[;&|`$<>]", model_name):
            return {"ok": False, "error": "無効なモデル名です。"}

        try:
            valid_url = mun_minutes.validate_ollama_url(ollama_url)
        except Exception as exc:
            return {"ok": False, "error": f"無効なOllama URLです: {exc}"}

        with self._lock:
            if self._status["status"] == "pulling":
                return {"ok": False, "error": "現在別のモデルをPull中です。"}

            self._cancel_event.clear()
            self._status = {
                "status": "pulling",
                "model": model_name,
                "completed_bytes": 0,
                "total_bytes": 0,
                "percent": 0.0,
                "current_status": "開始中…",
                "error": None,
                "start_time": time.time(),
                "completed_time": None
            }

        self._thread = threading.Thread(
            target=self._pull_worker,
            args=(valid_url, model_name, stream_factory),
            daemon=True
        )
        self._thread.start()
        return {"ok": True, "status": "started", "model": model_name}

    def _pull_worker(
        self,
        ollama_url: str,
        model_name: str,
        stream_factory: Optional[Callable[[str, bytes], Any]]
    ):
        pull_endpoint = ollama_url.rstrip("/") + "/api/pull"
        payload = json.dumps({"name": model_name, "stream": True}).encode("utf-8")

        try:
            if stream_factory:
                resp = stream_factory(pull_endpoint, payload)
            else:
                req = urllib.request.Request(
                    pull_endpoint,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), mun_minutes.NoRedirect())
                resp = opener.open(req, timeout=600)

            for raw_line in resp:
                if self._cancel_event.is_set():
                    raise InterruptedError("ユーザーによりPullが中断されました。")

                line_str = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else str(raw_line).strip()
                if not line_str:
                    continue

                try:
                    ev = json.loads(line_str)
                except Exception:
                    continue

                if "error" in ev:
                    raise RuntimeError(ev["error"])

                st_text = ev.get("status", "")
                completed = ev.get("completed", 0)
                total = ev.get("total", 0)
                pct = round((completed / total * 100), 1) if total > 0 else 0.0

                with self._lock:
                    self._status["current_status"] = st_text
                    if total > 0:
                        self._status["completed_bytes"] = completed
                        self._status["total_bytes"] = total
                        self._status["percent"] = min(100.0, pct)

                if st_text == "success":
                    break

            with self._lock:
                self._status["status"] = "completed"
                self._status["percent"] = 100.0
                self._status["current_status"] = "完了"
                self._status["completed_time"] = time.time()

        except InterruptedError:
            with self._lock:
                self._status["status"] = "cancelled"
                self._status["current_status"] = "キャンセル済み"
        except Exception as exc:
            with self._lock:
                self._status["status"] = "failed"
                self._status["error"] = f"Ollamaモデルダウンロード失敗: {exc}"
                self._status["current_status"] = "エラー"
