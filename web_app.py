"""模擬国連 議事録支援システム - ローカルWebアプリケーションサーバー"""
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
import socket
import ipaddress
import secrets
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, List, Any, Callable

# mun_minutes から既存の検証済みコアロジックを再利用
import mun_minutes
import dictionary
import meeting_documents
import meeting_import
import re
import mac_key_helper
import win_key_helper
import model_manager
import settings_manager
import component_installer

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_MODELS_DIR = PROJECT_DIR / "モデル"
DEFAULT_WHISPER_MODEL = DEFAULT_MODELS_DIR / "ggml-large-v3-turbo.bin"
DEFAULT_DATA_DIR = PROJECT_DIR / "議事録データ"
DEFAULT_COMMON_DICT_PATH = PROJECT_DIR / "辞書" / "共通辞書.json"
DEFAULT_WEB_DIR = PROJECT_DIR / "web"
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_LLM_MODEL = "qwen3:4b-instruct-2507-q4_K_M"
MAX_QUEUE_SIZE = 50
MAX_AUDIO_UPLOAD_SIZE = 100 * 1024 * 1024


def sanitize_session_id(session_id: str, base_dir: Path) -> Path:
    """パストラバーサルを防ぎ、指定セッションフォルダの安全なパスを返します。"""
    if not session_id or ".." in session_id or "/" in session_id or "\\" in session_id:
        raise ValueError("無効なセッションIDです。")
    target = (base_dir / session_id).resolve()
    base_resolved = base_dir.resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError("安全でないパスアクセスです。") from exc
    return target


def sanitize_chunk_index(raw_index) -> int:
    """チャンクインデックスを安全な整数として検証します。"""
    try:
        idx = int(raw_index)
        if idx < 0:
            raise ValueError()
        return idx
    except Exception as exc:
        raise ValueError("チャンクインデックスは0以上の整数である必要があります。") from exc


def create_unique_session_dir(base_dir: Path) -> Path:
    """既存データを上書きしない一意のセッションディレクトリを生成します。"""
    base_dir.mkdir(parents=True, exist_ok=True)
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    folder_name = f"セッション_{now_str}"
    target = base_dir / folder_name
    counter = 1
    while target.exists():
        target = base_dir / f"{folder_name}_{counter}"
        counter += 1
    target.mkdir(parents=True, exist_ok=False)
    # チャンク管理フォルダと未処理・失敗音声フォルダを初期化
    (target / "未処理音声").mkdir(parents=True, exist_ok=True)
    (target / "失敗音声").mkdir(parents=True, exist_ok=True)
    (target / "chunks").mkdir(parents=True, exist_ok=True)
    return target


def get_installed_ollama_models(ollama_url: str = DEFAULT_OLLAMA_URL) -> list:
    url = f"{ollama_url.rstrip('/')}/api/tags"
    req = urllib.request.Request(url, headers={"User-Agent": "MunMinutes/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []

def get_available_whisper_models(models_dir: Path) -> list:
    if not models_dir.is_dir():
        return []
    models = []
    for f in sorted(models_dir.glob("*.bin")):
        try:
            size_mb = round(f.stat().st_size / (1024 * 1024), 1)
            models.append({
                "filename": f.name,
                "path": str(f),
                "size_mb": size_mb
            })
        except Exception:
            pass
    return models

def preload_ollama_model(model_name: str, ollama_url: str = DEFAULT_OLLAMA_URL) -> dict:
    url = f"{ollama_url.rstrip('/')}/api/generate"
    payload = json.dumps({"model": model_name, "keep_alive": "10m"}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "model": model_name, "status": "loaded"}
    except Exception as exc:
        return {"ok": False, "error": f"Ollamaロード失敗: {exc}"}

def unload_ollama_model(model_name: str, ollama_url: str = DEFAULT_OLLAMA_URL) -> dict:
    url = f"{ollama_url.rstrip('/')}/api/generate"
    payload = json.dumps({"model": model_name, "keep_alive": 0}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"ok": True, "model": model_name, "status": "unloaded"}
    except Exception as exc:
        return {"ok": False, "error": f"Ollamaアンロード失敗: {exc}"}

def check_system_status(whisper_model: Path, whisper_cmd: str, ffmpeg_cmd: str, ollama_url: str, llm_model: str) -> dict:
    """システムのローカルコンポーネント状態を確認します。"""
    whisper_model_path = Path(whisper_model)
    whisper_model_ok = whisper_model_path.is_file()
    whisper_cmd_ok = shutil.which(whisper_cmd) is not None
    ffmpeg_cmd_ok = shutil.which(ffmpeg_cmd) is not None

    ollama_ok = False
    ollama_model_ready = False
    try:
        valid_url = mun_minutes.validate_ollama_url(ollama_url)
        req = urllib.request.Request(valid_url + "/api/tags", headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), mun_minutes.NoRedirect())
        with opener.open(req, timeout=3) as resp:
            data = json.load(resp)
            ollama_ok = True
            model_names = [m.get("name", "") for m in data.get("models", [])]
            for name in model_names:
                if name == llm_model or name.startswith(llm_model + ":") or llm_model in name:
                    ollama_model_ready = True
                    break
    except Exception:
        ollama_ok = False
        ollama_model_ready = False

    return {
        "status": "ok" if (whisper_model_ok and whisper_cmd_ok and ffmpeg_cmd_ok and ollama_ok and ollama_model_ready) else "degraded",
        "whisper_model": whisper_model_ok,
        "whisper_model_path": str(whisper_model_path),
        "whisper_cmd": whisper_cmd_ok,
        "ffmpeg_cmd": ffmpeg_cmd_ok,
        "ollama": ollama_ok,
        "ollama_url": ollama_url,
        "ollama_model_ready": ollama_model_ready,
        "llm_model": llm_model
    }

def get_system_setup_status(app_config: dict, key_monitor=None) -> dict:
    """システムの詳細診断情報およびセットアップ準備状態を取得します。"""
    whisper_model_path = Path(app_config.get("whisper_model", DEFAULT_WHISPER_MODEL))
    models_dir = Path(app_config.get("models_dir", whisper_model_path.parent if whisper_model_path.parent.is_dir() else DEFAULT_MODELS_DIR))
    data_dir = Path(app_config.get("data_dir", DEFAULT_DATA_DIR))
    ollama_url = app_config.get("ollama_url", DEFAULT_OLLAMA_URL)
    llm_model = app_config.get("llm_model", DEFAULT_LLM_MODEL)
    whisper_cmd = app_config.get("whisper_cmd", "whisper-cli")
    ffmpeg_cmd = app_config.get("ffmpeg_cmd", "ffmpeg")

    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    os_name = sys.platform
    arch = os.uname().machine if hasattr(os, "uname") else "unknown"

    ffmpeg_path = shutil.which(ffmpeg_cmd)
    ffmpeg_ok = ffmpeg_path is not None
    whisper_path = shutil.which(whisper_cmd)
    whisper_ok = whisper_path is not None

    ollama_ok = False
    ollama_models = []
    llm_ready = False
    try:
        valid_url = mun_minutes.validate_ollama_url(ollama_url)
        req = urllib.request.Request(valid_url + "/api/tags", headers={"Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), mun_minutes.NoRedirect())
        with opener.open(req, timeout=2) as resp:
            data = json.load(resp)
            ollama_ok = True
            ollama_models = [m.get("name", "") for m in data.get("models", [])]
            llm_ready = any(name == llm_model or name.startswith(llm_model + ":") or llm_model in name for name in ollama_models)
    except Exception:
        pass

    whisper_model_ok = whisper_model_path.is_file()
    available_whisper = get_available_whisper_models(models_dir)

    km_status = key_monitor.get_status() if key_monitor else {
        "platform": sys.platform,
        "is_running": False,
        "status": "unsupported"
    }

    data_free = settings_manager.get_disk_free_gb(data_dir)
    models_free = settings_manager.get_disk_free_gb(models_dir)
    data_writable = settings_manager.is_directory_writable(data_dir)
    models_writable = settings_manager.is_directory_writable(models_dir)

    all_ready = bool(ffmpeg_ok and whisper_ok and whisper_model_ok and ollama_ok and llm_ready)

    next_actions = []
    if not ffmpeg_ok:
        if os_name == "darwin":
            next_actions.append("FFmpegを導入してください: 'brew install ffmpeg'")
        elif os_name == "win32":
            next_actions.append("FFmpegを導入してください: 'winget install Gyan.FFmpeg' または https://ffmpeg.org から入手")
        else:
            next_actions.append("FFmpegをインストールしてください。")

    if not whisper_ok:
        if os_name == "darwin":
            next_actions.append("whisper-cliを導入してください: 'brew install whisper-cpp'")
        elif os_name == "win32":
            next_actions.append("whisper-cliを配置してください: https://github.com/ggerganov/whisper.cpp/releases から入手しPATHに追加")
        else:
            next_actions.append("whisper.cppをビルドまたはインストールしてください。")

    if not whisper_model_ok:
        next_actions.append("音声認識モデルが未配置です: 設定画面の『音声認識モデルのダウンロード』ボタンから取得してください。")

    if not ollama_ok:
        next_actions.append("Ollamaサービスが未稼働です: https://ollama.com よりインストールし、起動してください。")
    elif not llm_ready:
        next_actions.append(f"整文モデル '{llm_model}' が未導入です: 設定画面の『整文モデルのダウンロード』ボタンまたは 'ollama pull {llm_model}' で取得してください。")

    return {
        "ok": True,
        "all_ready": all_ready,
        "system": {
            "os": os_name,
            "architecture": arch,
            "python_version": py_ver
        },
        "components": {
            "python": {"ok": True, "version": py_ver},
            "ffmpeg": {
                "ok": ffmpeg_ok,
                "command": ffmpeg_cmd,
                "path": ffmpeg_path,
                "guide": "Mac: brew install ffmpeg / Win: winget install Gyan.FFmpeg"
            },
            "whisper": {
                "ok": whisper_ok,
                "command": whisper_cmd,
                "path": whisper_path,
                "guide": "Mac: brew install whisper-cpp / Win: https://github.com/ggerganov/whisper.cpp/releases"
            },
            "ollama": {
                "ok": ollama_ok,
                "url": ollama_url,
                "guide": "https://ollama.com からOllamaをインストール・起動"
            },
            "whisper_model": {
                "ok": whisper_model_ok,
                "filename": whisper_model_path.name,
                "path": str(whisper_model_path),
                "available_count": len(available_whisper)
            },
            "llm_model": {
                "ok": llm_ready,
                "model": llm_model,
                "installed_models": ollama_models
            },
            "key_monitor": km_status
        },
        "storage": {
            "data_dir": str(data_dir),
            "data_dir_exists": data_dir.is_dir(),
            "data_dir_writable": data_writable,
            "data_dir_free_gb": data_free,
            "models_dir": str(models_dir),
            "models_dir_exists": models_dir.is_dir(),
            "models_dir_writable": models_writable,
            "models_dir_free_gb": models_free
        },
        "next_actions": next_actions
    }




def match_speech_range_to_chunks(speech_start_sec: float, speech_end_sec: float, chunks: list) -> tuple:
    """
    発言指定範囲 [speech_start_sec, speech_end_sec] に重なるチャンクを特定し、
    時間的一致度に基づいて該当テキストを結合して返します。
    ※チャンク全文近似である境界条件や重複状況を明示します。
    """
    matched = []
    for c in chunks:
        # チャンクの時間範囲推定
        # 各チャンクのduration_secから概算の区間を計算
        c_dur = float(c.get("duration_sec", 20.0))
        # チャンクごとの開始・終了時刻情報があれば優先、なければindex * 20秒で近似
        c_start = float(c.get("start_sec", c.get("index", 0) * c_dur))
        c_end = float(c.get("end_sec", c_start + c_dur))

        overlap = max(0.0, min(speech_end_sec, c_end) - max(speech_start_sec, c_start))
        if overlap > 0.5:
            matched.append((c, overlap))

    matched.sort(key=lambda x: x[0].get("index", 0))
    texts = [c[0].get("polished_text") or c[0].get("raw_text", "") for c in matched if (c[0].get("polished_text") or c[0].get("raw_text"))]
    combined = " ".join(t.strip() for t in texts if t.strip())
    matched_indices = [c[0].get("index", 0) for c in matched]
    return combined, matched_indices


class ConflictError(Exception):
    """同時更新競合エラー（409 Conflict）"""
    pass


class SharingManager:
    """
    2台目（遠隔PC）とのローカル共有管理マネージャー（動的LANリスナー制御・CSRF防御・試行制限）。
    - 共有OFF時は loopback (127.0.0.1) のみ稼働し、LANソケットは存在しない。
    - 共有ON時のみ制限付きLANリスナー (0.0.0.0:lan_port) を動的起動。
    - 共有OFF時は LANリスナーを物理的に shutdown & close。
    - 接続コードはホスト本人のみに返し、未認証/LAN端末へは一切漏らさない。
    - 8桁の推測耐性コード (MUN-XXXX-YYYY) と 5回連続失敗で5分ロックアウトする試行回数制限。
    - LANゲストはホワイトリストAPIのみ許可し、録音・モデル・他セッションは403遮断。
    - 厳格なSame-Origin検証およびホスト専用CSRFトークンによるDNS rebinding/CSRF防御。
    """
    def __init__(self, host_port: int = 8000):
        self.lock = threading.RLock()
        self.host_port = host_port
        self.host_csrf_token = secrets.token_hex(32)
        self.enabled = False
        self.join_code = None
        self.role_default = "editor"  # "viewer" or "editor"
        self.shared_session_id = None # 共有対象の会議ID（スコープ限定）
        self.guest_sessions = {}      # token -> dict
        self.started_at = None
        self.lan_port = None
        self.lan_server = None
        self.lan_thread = None
        self.failed_attempts = {}     # ip -> {"count": int, "locked_until": float, "last_attempt": float}

    def get_lan_ips(self) -> List[str]:
        ipv4_ips = []
        ipv6_ips = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None):
                ip = info[4][0]
                if ip.startswith("127.") or ip == "::1":
                    continue
                try:
                    addr = ipaddress.ip_address(ip)
                    if getattr(addr, "is_link_local", False):
                        continue
                    if addr.is_private:
                        if addr.version == 4:
                            if ip not in ipv4_ips:
                                ipv4_ips.append(ip)
                        else:
                            if ip not in ipv6_ips:
                                ipv6_ips.append(f"[{ip}]")
                except ValueError:
                    pass
        except Exception:
            pass
        if not ipv4_ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("10.255.255.255", 1))
                ip = s.getsockname()[0]
                s.close()
                if ip not in ipv4_ips and not ip.startswith("127."):
                    ipv4_ips.append(ip)
            except Exception:
                pass
        return ipv4_ips + ipv6_ips

    def toggle_sharing(self, enable: bool, role: str = "editor", session_id: str = None, app_config: dict = None, queue_manager = None) -> dict:
        server_to_stop = None
        port_to_stop = None
        with self.lock:
            self.enabled = bool(enable)
            if self.enabled:
                if not session_id or not isinstance(session_id, str) or not session_id.strip():
                    self.enabled = False
                    raise ValueError("共有対象の会議セッションID（session_id）を明示的に指定してください。")
                session_id = session_id.strip()

                if app_config and "data_dir" in app_config:
                    data_dir = app_config["data_dir"]
                    sanitize_session_id(session_id, data_dir)
                    sess_path = Path(data_dir) / session_id
                    if not sess_path.is_dir():
                        self.enabled = False
                        raise ValueError(f"指定されたセッション {session_id} のディレクトリが存在しません。")

                charset = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
                p1 = "".join(secrets.choice(charset) for _ in range(4))
                p2 = "".join(secrets.choice(charset) for _ in range(4))
                self.join_code = f"MUN-{p1}-{p2}"
                self.role_default = role if role in ("viewer", "editor") else "editor"
                self.shared_session_id = session_id
                self.started_at = time.time()
                self.guest_sessions.clear()
                self.failed_attempts.clear()

                if self.lan_server is None and app_config is not None:
                    self._start_lan_listener(app_config, queue_manager)
            else:
                self.join_code = None
                self.shared_session_id = None
                self.started_at = None
                self.guest_sessions.clear()
                self.failed_attempts.clear()
                server_to_stop = self.lan_server
                port_to_stop = self.lan_port
                self.lan_server = None
                self.lan_thread = None
                self.lan_port = None
            res = self.get_status(is_host=True)

        if server_to_stop is not None:
            try:
                print(f"[LAN共有] LANリスナーを停止・クローズしています: ポート {port_to_stop}")
                server_to_stop.shutdown()
                server_to_stop.server_close()
            except Exception as e:
                print(f"[LAN共有] リスナー停止エラー: {e}", file=sys.stderr)
        return res

    def _start_lan_listener(self, app_config: dict, queue_manager):
        target_port = self.host_port + 1
        for p in range(target_port, target_port + 20):
            try:
                handler_factory = lambda *args, **kwargs: LanRequestHandler(
                    *args,
                    app_config=app_config,
                    queue_manager=queue_manager,
                    sharing_manager=self,
                    **kwargs
                )
                self.lan_server = ThreadingHTTPServer(("0.0.0.0", p), handler_factory)
                self.lan_port = p
                self.lan_thread = threading.Thread(target=self.lan_server.serve_forever, daemon=True)
                self.lan_thread.start()
                print(f"[LAN共有] 制限付きLANリスナーを起動しました: 0.0.0.0:{self.lan_port}")
                return
            except OSError:
                continue
        raise RuntimeError("LAN共有リスナー用の空きポートを確保できませんでした。")

    def _stop_lan_listener(self):
        if self.lan_server is not None:
            try:
                print(f"[LAN共有] LANリスナーを停止・クローズしています: ポート {self.lan_port}")
                self.lan_server.shutdown()
                self.lan_server.server_close()
            except Exception as e:
                print(f"[LAN共有] リスナー停止エラー: {e}", file=sys.stderr)
            finally:
                self.lan_server = None
                self.lan_thread = None
                self.lan_port = None

    def get_status(self, is_host: bool = False) -> dict:
        with self.lock:
            status = {
                "enabled": self.enabled,
                "role_default": self.role_default,
                "shared_session_id": self.shared_session_id,
                "lan_port": self.lan_port,
                "lan_ips": self.get_lan_ips(),
                "guest_count": len(self.guest_sessions),
                "security_notice": (
                    "【LAN通信セキュリティについての重要警告】\n"
                    "本共有は同一ローカルネットワーク（Wi-Fi/有線LAN）内の直接HTTP通信を使用します。\n"
                    "接続コードはアクセス認証用であり、通信内容を暗号化（HTTPS）するものではありません。\n"
                    "公衆Wi-Fi等では盗聴のリスクがあります。信頼できる安全なプライベートLAN内でのみご利用ください。\n"
                    "外部クラウドや外部DBへの送信は一切行いません。"
                )
            }
            if is_host:
                status["join_code"] = self.join_code
                status["host_csrf_token"] = self.host_csrf_token
            return status

    def authenticate_code(self, code: str, client_ip: str) -> tuple:
        with self.lock:
            if not self.enabled or not self.join_code:
                return None, "共有モードが無効です。"

            now = time.time()
            attempt = self.failed_attempts.setdefault(client_ip, {"count": 0, "locked_until": 0.0, "last_attempt": 0.0})
            if now < attempt["locked_until"]:
                rem_sec = int(attempt["locked_until"] - now)
                return None, f"試行回数の上限を超えました。あと {rem_sec} 秒間ロックされています (429 Too Many Requests)。"

            clean_input = (code or "").strip().upper().replace("-", "")
            target_clean = self.join_code.replace("-", "").replace("MUN", "")

            matches = (
                clean_input == target_clean or
                clean_input == ("MUN" + target_clean) or
                (code or "").strip().upper() == self.join_code
            )

            if not matches:
                attempt["count"] += 1
                attempt["last_attempt"] = now
                if attempt["count"] >= 5:
                    attempt["locked_until"] = now + 300.0
                    return None, "接続コードが正しくありません。5回連続失敗したため5分間ロックアウトされました。"
                return None, f"接続コードが正しくありません。（残り試行可能回数: {5 - attempt['count']}回）"

            del self.failed_attempts[client_ip]
            token = secrets.token_hex(24)
            sess = {
                "token": token,
                "role": self.role_default,
                "session_id": self.shared_session_id,
                "ip": client_ip,
                "joined_at": now,
                "last_seen": now
            }
            self.guest_sessions[token] = sess
            return sess, None

    def validate_token(self, token: str) -> Optional[dict]:
        with self.lock:
            if not self.enabled:
                return None
            sess = self.guest_sessions.get(token)
            if sess:
                sess["last_seen"] = time.time()
                return sess
            return None

    def remove_token(self, token: str) -> bool:
        with self.lock:
            if token in self.guest_sessions:
                del self.guest_sessions[token]
                return True
            return False

    def terminate_all(self):
        server_to_stop = None
        port_to_stop = None
        with self.lock:
            self.enabled = False
            self.join_code = None
            self.shared_session_id = None
            self.guest_sessions.clear()
            self.failed_attempts.clear()
            server_to_stop = self.lan_server
            port_to_stop = self.lan_port
            self.lan_server = None
            self.lan_thread = None
            self.lan_port = None
        if server_to_stop is not None:
            try:
                print(f"[LAN共有] LANリスナーを停止・クローズしています: ポート {port_to_stop}")
                server_to_stop.shutdown()
                server_to_stop.server_close()
            except Exception as e:
                print(f"[LAN共有] リスナー停止エラー: {e}", file=sys.stderr)

def cleanup_expired_audio(session_dir: Path, retention_days: int) -> int:
    """保存音声フォルダから保持期限（retention_days日）を超過したファイルを削除"""
    if not retention_days or not isinstance(retention_days, int) or retention_days <= 0:
        return 0
    saved_dir = session_dir / "保存音声"
    if not saved_dir.is_dir():
        return 0
    now = time.time()
    cutoff = now - (retention_days * 86400)
    deleted_count = 0
    for f in saved_dir.glob("*.wav"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                deleted_count += 1
        except Exception:
            pass
    return deleted_count


class SessionQueueManager:
    """
    セッションごとの音声チャンクキューおよび順序保証バックグラウンドワーカー。
    - 音声データは到着時に即座に「未処理音声/」へ保存。
    - WhisperとQwenが共に成功した場合のみ未処理音声を削除（永久保存はしない）。
    - 処理失敗時は音声を「失敗音声/」に保持し、UIで明示・再試行可能。
    - 録音停止時も残りのキューが空になるまで処理を継続。
    - ユーザーの手動編集は上書きしない。
    """

    def __init__(self, app_config: dict):
        self.app_config = app_config
        self.lock = threading.RLock()
        self.sessions = {}  # session_id -> session_state dict
        self.active_session_id = None
        self._stop_event = threading.Event()
        self._asr_lock = threading.Lock()
        self.active_finalizers = set()  # set of (session_id, utterance_id)
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def stop(self):
        self._stop_event.set()

    def get_active_session_id(self) -> Optional[str]:
        with self.lock:
            return self.active_session_id

    def start_utterance(self, session_id: str, press_time_ms: float, label: str = "") -> dict:
        """発言開始（F2キー押下またはトグルON）。1発言1セルのセル枠を割り当てます。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            meta = state["meta"]
            blocks = meta.setdefault("blocks", [])
            seen = set(meta.get("block_source_ids", []))
            seen.update(b.get("id") for b in blocks if b.get("id"))

            utterance_index = len(meta.get("utterances", [])) + 1
            utterance_id = f"utt_{utterance_index:04d}"
            block_id = f"blk_{time.time_ns()}"

            cell_label = label or f"発言 #{len(blocks) + 1}"
            now_time_str = datetime.datetime.now().strftime("%H:%M:%S")

            new_block = {
                "id": block_id,
                "utterance_id": utterance_id,
                "chunk_index": None,
                "label": cell_label,
                "speaker": "話者",
                "text": "",
                "raw_text": "",
                "status": "recording",
                "is_transcribed": False,
                "created_at": now_time_str,
                "updated_at": now_time_str,
                "version": 1,
                "user_edited": False,
                "hidden": False,
                "chunk_pieces": {},
                "final_candidate": None,
                "candidate_status": None,
                "candidate_diff": None
            }
            blocks.append(new_block)
            seen.add(utterance_id)
            seen.add(block_id)
            meta["block_source_ids"] = sorted(seen)

            active_utt = {
                "id": utterance_id,
                "block_id": block_id,
                "start_time_ms": press_time_ms,
                "label": cell_label,
                "closed": False,
                "chunk_count": 0,
                "recording_mode": meta.get("recording_mode", "streaming"),
                "audio_retention": meta.get("audio_retention", "delete_on_success"),
                "retention_days": meta.get("retention_days", 0)
            }
            meta.setdefault("utterances", []).append(active_utt)
            meta["active_utterance"] = active_utt
            meta["active_cue_hold"] = {
                "is_holding": True,
                "start_time_ms": press_time_ms,
                "utterance_id": utterance_id,
                "block_id": block_id
            }
            if not self._save_meta_unlocked(state):
                blocks.remove(new_block)
                seen.discard(utterance_id)
                seen.discard(block_id)
                meta["block_source_ids"] = sorted(seen)
                meta["utterances"].remove(active_utt)
                meta["active_utterance"] = None
                meta["active_cue_hold"] = {
                    "is_holding": False,
                    "start_time_ms": None
                }
                raise IOError("発言開始のメタ情報保存に失敗しました。ディスク容量を確認してください。")
            return {"utterance_id": utterance_id, "block_id": block_id}

    def end_utterance(self, session_id: str, release_time_ms: float) -> dict:
        """発言終了（F2キー離上またはトグルOFF）。現在の発言セルを確定クローズします。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            meta = state["meta"]
            active_utt = meta.get("active_utterance")
            if active_utt:
                active_utt["closed"] = True
                active_utt["end_time_ms"] = release_time_ms
                meta["active_utterance"] = None

            old_cue_hold = dict(meta.get("active_cue_hold", {}))
            meta["active_cue_hold"] = {
                "is_holding": False,
                "start_time_ms": None
            }
            if not self._save_meta_unlocked(state):
                if active_utt:
                    active_utt["closed"] = False
                    active_utt["end_time_ms"] = None
                    meta["active_utterance"] = active_utt
                meta["active_cue_hold"] = old_cue_hold
                raise IOError("発言終了のメタ情報保存に失敗しました。ディスク容量を確認してください。")
            return {"ok": True}


    def get_blocks(self, session_id: str) -> list:
        """セッションの発言ブロック一覧を取得します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            # 新たに完成した発言だけを移行し、編集・非表示・統合済みの発言を再作成しない。
            seen = set(state["meta"].get("block_source_ids", []))
            seen.update(b.get("id") for b in blocks if b.get("id"))
            changed = False
            if state["meta"].get("paste_queue"):
                for pq in state["meta"]["paste_queue"]:
                    source_id = pq.get("id")
                    if source_id and source_id not in seen and pq.get("text"):
                        blocks.append({
                            "id": pq.get("id", f"blk_{len(blocks)+1:04d}"),
                            "chunk_index": pq.get("chunk_index"),
                            "label": pq.get("label", f"発言 #{len(blocks)+1}"),
                            "text": pq.get("text", ""),
                            "raw_text": pq.get("text", ""),
                            "is_transcribed": pq.get("state") == "pasted",
                            "created_at": pq.get("created_at", datetime.datetime.now().strftime("%H:%M:%S")),
                            "updated_at": pq.get("created_at", datetime.datetime.now().strftime("%H:%M:%S")),
                            "version": 1,
                            "user_edited": False,
                            "hidden": False
                        })
                        seen.add(source_id)
                        changed = True
            if changed:
                state["meta"]["block_source_ids"] = sorted(seen)
                self._save_meta_unlocked(state)
            return [dict(b) for b in blocks if not b.get("hidden", False)]

    def update_block(self, session_id: str, block_id: str, text: Optional[str] = None, is_transcribed: Optional[bool] = None, base_text: Optional[str] = None, expected_version: Optional[int] = None) -> dict:
        """発言ブロックのテキスト編集または転記済みトグルを更新します。同時更新競合を検知します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            target = next((b for b in blocks if b.get("id") == block_id), None)
            if not target:
                raise ValueError(f"発言ブロック {block_id} が見つかりません。")
            if expected_version is not None and target.get("version") != int(expected_version):
                raise ConflictError("他の端末で文章が変更されました。最新の表示を確認してください。")
            if text is not None:
                current = target.get("text", "")
                if base_text is not None and current != base_text:
                    if current.startswith(base_text):
                        text += current[len(base_text):]
                    else:
                        raise ValueError("他の端末で文章が変更されました。コピーしてから再読み込みしてください。")
                target["text"] = text
                target["user_edited"] = True
            if is_transcribed is not None:
                target["is_transcribed"] = bool(is_transcribed)
            target["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            target["version"] = target.get("version", 1) + 1
            self._save_meta_unlocked(state)
            return dict(target)

    def split_block(self, session_id: str, block_id: str, first_text: Optional[str] = None, second_text: Optional[str] = None, split_index: Optional[int] = None) -> list:
        """発言ブロックを2つに分割します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            idx = next((i for i, b in enumerate(blocks) if b.get("id") == block_id), None)
            if idx is None:
                raise ValueError(f"発言ブロック {block_id} が見つかりません。")
            target = blocks[idx]
            original_text = target.get("text", "")

            if first_text is not None and second_text is not None:
                part1 = first_text
                part2 = second_text
            elif split_index is not None and 0 <= split_index <= len(original_text):
                part1 = original_text[:split_index].rstrip()
                part2 = original_text[split_index:].lstrip()
            else:
                lines = [l for l in original_text.splitlines() if l.strip()]
                if len(lines) > 1:
                    mid = len(lines) // 2
                    part1 = "\n".join(lines[:mid]).strip()
                    part2 = "\n".join(lines[mid:]).strip()
                else:
                    mid = len(original_text) // 2
                    part1 = original_text[:mid].strip()
                    part2 = original_text[mid:].strip()

            target["text"] = part1
            target["user_edited"] = True
            target["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            target["version"] = target.get("version", 1) + 1

            new_id = f"{block_id}_part2"
            if any(b.get("id") == new_id for b in blocks):
                new_id = f"{new_id}_{time.time_ns()}"
            new_block = {
                "id": new_id,
                "chunk_index": target.get("chunk_index"),
                "label": f"{target.get('label', '発言')} (後編)",
                "text": part2,
                "raw_text": "",
                "is_transcribed": target.get("is_transcribed", False),
                "created_at": datetime.datetime.now().strftime("%H:%M:%S"),
                "updated_at": datetime.datetime.now().strftime("%H:%M:%S"),
                "version": 1,
                "user_edited": True,
                "hidden": False
            }
            blocks.insert(idx + 1, new_block)
            self._save_meta_unlocked(state)
            return [dict(target), dict(new_block)]

    def merge_block(self, session_id: str, block_id: str, target_id: Optional[str] = None) -> dict:
        """発言ブロックを直前のブロック（または指定ブロック）と結合します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            idx = next((i for i, b in enumerate(blocks) if b.get("id") == block_id), None)
            if idx is None:
                raise ValueError(f"発言ブロック {block_id} が見つかりません。")

            if target_id:
                t_idx = next((i for i, b in enumerate(blocks) if b.get("id") == target_id), None)
                if t_idx is None:
                    raise ValueError(f"結合対象ブロック {target_id} が見つかりません。")
            else:
                if idx == 0:
                    raise ValueError("直前のブロックが存在しないため結合できません。")
                t_idx = idx - 1

            first_idx, second_idx = min(idx, t_idx), max(idx, t_idx)
            b1 = blocks[first_idx]
            b2 = blocks[second_idx]

            b1["text"] = f"{b1.get('text', '').rstrip()}\n{b2.get('text', '').lstrip()}".strip()
            b1["user_edited"] = True
            b1["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            b1["version"] = b1.get("version", 1) + 1
            b1["is_transcribed"] = b1.get("is_transcribed", False) and b2.get("is_transcribed", False)

            blocks.pop(second_idx)
            self._save_meta_unlocked(state)
            return dict(b1)

    def clear_blocks_view(self, session_id: str) -> bool:
        """画面上のブロック表示をクリアします（サーバー保存データ・履歴は保護されます）。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            for b in blocks:
                b["hidden"] = True
            self._save_meta_unlocked(state)
            return True

    def restore_blocks(self, session_id: str) -> list:
        """非表示にしたブロックを復元し、履歴から再構成します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            for b in blocks:
                b["hidden"] = False
            if not blocks:
                # 整文.txt からの安全な多重復元
                p_file = state["session_dir"] / "整文.txt"
                if p_file.is_file():
                    content = p_file.read_text(encoding="utf-8").strip()
                    if content:
                        for i, p in enumerate([content]):
                            if p.strip():
                                blocks.append({
                                    "id": f"restored_{i+1:04d}",
                                    "label": f"復元発言 #{i+1}",
                                    "text": p.strip(),
                                    "raw_text": p.strip(),
                                    "is_transcribed": False,
                                    "created_at": datetime.datetime.now().strftime("%H:%M:%S"),
                                    "updated_at": datetime.datetime.now().strftime("%H:%M:%S"),
                                    "version": 1,
                                    "user_edited": False,
                                    "hidden": False
                                })
            self._save_meta_unlocked(state)
            return [dict(b) for b in blocks if not b.get("hidden", False)]

    def get_paste_queue(self, session_id: str) -> list:
        """セッションの発言貼付キューを取得します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            queue = state["meta"].get("paste_queue", [])
            return [dict(item) for item in queue]

    def add_speech_segment(self, session_id: str, press_time_ms: float, release_time_ms: float, pre_margin_sec: float = 2.0, post_margin_sec: float = 2.0, label: str = "") -> dict:
        """Docs操作中に記録された発言区間（キー押下〜離上）を登録し貼付キューへ追加します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            segments = state["meta"].setdefault("speech_segments", [])
            paste_queue = state["meta"].setdefault("paste_queue", [])

            # タイムベースの統一（録音開始からの相対秒へ変換）
            session_start_ms = float(state["meta"].get("start_timestamp_ms", 0.0))
            if session_start_ms <= 0.0:
                session_start_ms = time.time() * 1000.0
                state["meta"]["start_timestamp_ms"] = session_start_ms

            if press_time_ms > 1000000000.0:  # UNIX epoch ミリ秒の場合
                rel_press_sec = max(0.0, (press_time_ms - session_start_ms) / 1000.0)
                rel_release_sec = max(rel_press_sec, (release_time_ms - session_start_ms) / 1000.0)
            else:  # 既に相対ミリ秒（テスト等の相対値）
                rel_press_sec = max(0.0, press_time_ms / 1000.0)
                rel_release_sec = max(rel_press_sec, release_time_ms / 1000.0)

            dur_sec = max(0.0, rel_release_sec - rel_press_sec)
            seg_id = f"seg_{len(segments) + 1:04d}"
            effective_start_sec = max(0.0, rel_press_sec - pre_margin_sec)
            effective_end_sec = rel_release_sec + post_margin_sec

            target_start_ms = press_time_ms - (pre_margin_sec * 1000.0)
            target_end_ms = release_time_ms + (post_margin_sec * 1000.0)

            # 各発言指定は必ず今回指定の実音声から作成するため、過去チャンクへの自動流用は行わずfresh audioを要求
            seg_info = {
                "id": seg_id,
                "press_time_ms": press_time_ms,
                "release_time_ms": release_time_ms,
                "target_start_ms": target_start_ms,
                "target_end_ms": target_end_ms,
                "needs_audio": True,
                "rel_start_sec": rel_press_sec,
                "rel_end_sec": rel_release_sec,
                "duration_sec": dur_sec,
                "pre_margin_sec": pre_margin_sec,
                "post_margin_sec": post_margin_sec,
                "effective_start_sec": effective_start_sec,
                "effective_end_sec": effective_end_sec,
                "label": label or f"発言 #{len(segments) + 1}",
                "state": "pending_audio",
                "text": "",
                "chunk_index": None,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            segments.append(seg_info)

            queue_item = {
                "id": seg_id,
                "label": seg_info["label"],
                "state": "preparing",
                "text": "",
                "duration_sec": dur_sec,
                "pre_margin_sec": pre_margin_sec,
                "post_margin_sec": post_margin_sec,
                "target_start_ms": target_start_ms,
                "target_end_ms": target_end_ms,
                "needs_audio": True,
                "rel_start_sec": rel_press_sec,
                "rel_end_sec": rel_release_sec,
                "effective_start_sec": effective_start_sec,
                "effective_end_sec": effective_end_sec,
                "matched_chunk_indices": [],
                "chunk_index": None,
                "created_at": seg_info["created_at"],
                "copied_at": None,
                "pasted_at": None,
                "order": len(paste_queue)
            }
            paste_queue.append(queue_item)
            self._save_meta_unlocked(state)
            return queue_item

    def update_paste_queue_item(self, session_id: str, item_id: str, text: str = None, state_str: str = None, label: str = None) -> dict:
        """キューアイテムのテキスト、状態、ラベルを更新します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            paste_queue = state["meta"].setdefault("paste_queue", [])
            target = next((item for item in paste_queue if item.get("id") == item_id), None)
            if not target:
                raise ValueError(f"キューアイテム {item_id} が見つかりません。")
            if text is not None:
                target["text"] = text
            if state_str is not None:
                if state_str not in ("preparing", "ready", "pasted", "failed"):
                    raise ValueError(f"無効なキュー状態です: {state_str}")
                target["state"] = state_str
                if state_str == "pasted" and not target.get("pasted_at"):
                    target["pasted_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if label is not None:
                target["label"] = label
            self._save_meta_unlocked(state)
            return dict(target)

    def get_head_item_for_paste(self, session_id: str) -> dict:
        """
        貼付キューの先頭（未確認貼付の最上位）アイテムを取得します。
        ※成功を確認する前に勝手に 'pasted' へ遷移させず、まず 'copied'（クリップボード送信済）とし、
        ユーザーのDocs貼付確認（POST .../confirm）または手動確認をもって 'pasted' とします。
        先頭が未完了（preparing）の場合は後続を勝手に貼らずエラーを返します。
        """
        state = self.get_or_create_session(session_id)
        with self.lock:
            paste_queue = state["meta"].setdefault("paste_queue", [])
            # 未貼付またはコピー済み（未確認）のアイテムを探索
            pending_items = [it for it in paste_queue if it.get("state") not in ("pasted", "confirmed")]
            if not pending_items:
                return {"ok": False, "error": "貼付待ちのキューアイテムがありません。"}

            head = pending_items[0]
            if head.get("state") == "preparing":
                return {
                    "ok": False,
                    "error": f"先頭の発言「{head.get('label')}」は認識・整文処理中（準備中）です。未完了時は後続を勝手に貼り付けません。",
                    "head_item": dict(head)
                }
            if head.get("state") == "failed":
                return {
                    "ok": False,
                    "error": f"先頭の発言「{head.get('label')}」は処理に失敗しています。再試行または編集してから貼り付けてください。",
                    "head_item": dict(head)
                }

            # クリップボード送信用に状態を 'copied'（準備・コピー済）へ安全に更新
            head["state"] = "copied"
            head["copied_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_meta_unlocked(state)

            return {
                "ok": True,
                "copied_item": dict(head),
                "remaining_count": len([it for it in paste_queue if it.get("state") not in ("pasted", "confirmed")])
            }

    def confirm_paste_item(self, session_id: str, item_id: str, success: bool = True) -> dict:
        """
        Docsへの貼付完了（または失敗・キャンセル）を明示的に確定します。
        成功確認時のみ 'pasted' とし、失敗時は 'ready' または 'failed' に戻してキューから消えないように保護します。
        """
        state = self.get_or_create_session(session_id)
        with self.lock:
            paste_queue = state["meta"].setdefault("paste_queue", [])
            target = next((item for item in paste_queue if item.get("id") == item_id), None)
            if not target:
                raise ValueError(f"キューアイテム {item_id} が見つかりません。")

            if success:
                target["state"] = "pasted"
                target["pasted_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                # 失敗時は消さずに ready（再試行可能）へ復帰保護
                target["state"] = "ready"
                target["copied_at"] = None

            self._save_meta_unlocked(state)
            return dict(target)

    def paste_head_item(self, session_id: str) -> dict:
        """後方互換ラッパー: get_head_item_for_paste を呼び出します。"""
        res = self.get_head_item_for_paste(session_id)
        if res.get("ok"):
            # 後方互換キー
            res["pasted_item"] = res["copied_item"]
        return res

    def reorder_paste_queue(self, session_id: str, order_ids: list) -> list:
        """キューアイテムの表示順序を並べ替えます。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            paste_queue = state["meta"].setdefault("paste_queue", [])
            id_map = {it.get("id"): it for it in paste_queue}
            new_queue = []
            for item_id in order_ids:
                if item_id in id_map:
                    new_queue.append(id_map.pop(item_id))
            for remaining in id_map.values():
                new_queue.append(remaining)
            for idx, item in enumerate(new_queue):
                item["order"] = idx
            state["meta"]["paste_queue"] = new_queue
            self._save_meta_unlocked(state)
            return [dict(it) for it in new_queue]

    def get_or_create_session(self, session_id: str) -> dict:
        data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
        session_dir = sanitize_session_id(session_id, data_dir)
        with self.lock:
            if self.active_session_id is None:
                self.active_session_id = session_id
            if session_id in self.sessions:
                return self.sessions[session_id]

            # ディレクトリの復元または作成
            (session_dir / "未処理音声").mkdir(parents=True, exist_ok=True)
            (session_dir / "失敗音声").mkdir(parents=True, exist_ok=True)
            (session_dir / "chunks").mkdir(parents=True, exist_ok=True)

            meta = {
                "session_id": session_id,
                "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "start_timestamp_ms": time.time() * 1000.0,
                "is_recording": True,
                "recording_mode": "streaming",
                "audio_retention": "delete_on_success",
                "retention_days": 0,
                "whisper_model": str(self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL)),
                "llm_model": self.app_config.get("llm_model", DEFAULT_LLM_MODEL),
                "total_chunks": 0,
                "completed_chunks": 0,
                "failed_chunks": 0,
                "has_error": False,
                "paste_queue": [],
                "speech_segments": []
            }
            meta_path = session_dir / "メタ情報.json"
            if meta_path.is_file():
                try:
                    loaded = json.loads(meta_path.read_text(encoding="utf-8"))
                    meta.update(loaded)
                    if "retention_days" not in loaded or loaded.get("retention_days") is None:
                        meta["retention_days"] = 0
                except Exception:
                    pass
            else:
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

            # 既存の未処理チャンクの検出とキュー復元
            queue = []
            chunks_dir = session_dir / "chunks"
            if chunks_dir.is_dir():
                for f in sorted(chunks_dir.glob("chunk_*.json")):
                    try:
                        cdata = json.loads(f.read_text(encoding="utf-8"))
                        if cdata.get("state") in ("queued", "transcribing", "polishing"):
                            queue.append(cdata["index"])
                    except Exception:
                        pass

            state = {
                "session_id": session_id,
                "session_dir": session_dir,
                "is_recording": meta.get("is_recording", True),
                "queue": sorted(list(set(queue))),
                "processing_chunk_index": None,
                "processing_step": "idle",
                "meta": meta
            }
            # 前回のプロセスが途中終了した仕上げ処理は実行中ではない。
            for utterance in meta.get("utterances", []):
                if utterance.get("finalizer_running"):
                    utterance["finalizer_running"] = False
                    if not queue and utterance.get("state") == "finalizing":
                        utterance["state"] = "failed"
                        utterance["error"] = "仕上げ処理が中断されました。再試行してください。"
                        for block in meta.get("blocks", []):
                            if block.get("utterance_id") == utterance.get("id"):
                                block["status"] = "failed"
            self.sessions[session_id] = state
            return state

    def enqueue_chunk(self, session_id: str, chunk_index: int, audio_bytes: bytes, duration_sec: float, is_final: bool, file_ext: str = ".webm", utterance_id: Optional[str] = None, seq_index: Optional[int] = None) -> dict:
        state = self.get_or_create_session(session_id)
        with self.lock:
            if len(state["queue"]) >= MAX_QUEUE_SIZE:
                raise ValueError(f"待機中キューの処理上限（{MAX_QUEUE_SIZE}件）に達しています。")
        session_dir = state["session_dir"]
        unprocessed_dir = session_dir / "未処理音声"
        chunks_dir = session_dir / "chunks"

        # utterance_id未指定時は現在のアクティブ発言を自動バインド
        if not utterance_id:
            with self.lock:
                active_utt = state["meta"].get("active_utterance")
                if active_utt and not active_utt.get("closed"):
                    utterance_id = active_utt.get("id")
                    if seq_index is None:
                        seq_index = active_utt.get("chunk_count", 0)
                        active_utt["chunk_count"] = seq_index + 1

        audio_path = unprocessed_dir / f"chunk_{chunk_index:04d}{file_ext}"
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        upload_key = f"{utterance_id}:{seq_index}" if utterance_id is not None and seq_index is not None else None
        if upload_key:
            with self.lock:
                previous = state["meta"].setdefault("audio_uploads", {}).get(upload_key)
                if previous:
                    if previous.get("sha256") != audio_hash:
                        raise ValueError("同じ音声区間番号に異なる音声が送信されました。")
                    return {"ok": True, "session_id": session_id, "chunk_index": previous["index"], "state": "already_received"}
        existing_audio = list(unprocessed_dir.glob(f"chunk_{chunk_index:04d}.*"))
        if existing_audio:
            if len(existing_audio) != 1 or existing_audio[0] != audio_path or hashlib.sha256(audio_path.read_bytes()).hexdigest() != audio_hash:
                raise ValueError("保存済みの音声区間と内容が異なるため上書きできません。")
        else:
            audio_path.write_bytes(audio_bytes)

        chunk_info = {
            "index": chunk_index,
            "filename": audio_path.name,
            "duration_sec": duration_sec,
            "is_final": is_final,
            "state": "queued",
            "raw_text": "",
            "polished_text": "",
            "error": None,
            "utterance_id": utterance_id,
            "seq_index": seq_index if seq_index is not None else chunk_index,
            "audio_sha256": audio_hash,
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None
        }
        chunk_json_path = chunks_dir / f"chunk_{chunk_index:04d}.json"
        chunk_json_path.write_text(json.dumps(chunk_info, ensure_ascii=False, indent=2), encoding="utf-8")

        with self.lock:
            if upload_key:
                state["meta"].setdefault("audio_uploads", {})[upload_key] = {"sha256": audio_hash, "index": chunk_index}
            target_utt = None
            if utterance_id:
                target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utterance_id), None)
            bound_mode = target_utt.get("recording_mode") if target_utt else None
            is_high_accuracy = (bound_mode == "high_accuracy") if bound_mode else (utterance_id is not None and state["meta"].get("recording_mode") == "high_accuracy")
            added_to_queue = False
            if not is_high_accuracy:
                if chunk_index not in state["queue"]:
                    state["queue"].append(chunk_index)
                    state["queue"].sort()
                    added_to_queue = True
            old_total_chunks = state["meta"].get("total_chunks", 0)
            state["meta"]["total_chunks"] = max(old_total_chunks, chunk_index + 1)

            # 遅延到着ガード（high_accuracyモード）: finish通知後に遅れて届いた最終チャンクで全区間が揃った場合、仕上げ処理を安全に起動
            should_finalize = False
            if is_high_accuracy and target_utt and target_utt.get("finish_notified"):
                expected = target_utt.get("expected_chunks", 0)
                chunks = self._read_all_chunks_unlocked(session_dir)
                u_chunks = [c for c in chunks if c.get("utterance_id") == utterance_id]
                present_seqs = {c.get("seq_index") for c in u_chunks if c.get("seq_index") is not None}
                failed_chunks = [c for c in u_chunks if c.get("state") == "failed"]
                if expected > 0 and present_seqs == set(range(expected)) and not failed_chunks:
                    if not target_utt.get("finalizer_running") and (session_id, utterance_id) not in self.active_finalizers:
                        target_utt["finalizer_running"] = True
                        self.active_finalizers.add((session_id, utterance_id))
                        target_utt["state"] = "finalizing"
                        should_finalize = True

            if not self._save_meta_unlocked(state):
                if upload_key:
                    state["meta"].get("audio_uploads", {}).pop(upload_key, None)
                if added_to_queue and chunk_index in state["queue"]:
                    state["queue"].remove(chunk_index)
                state["meta"]["total_chunks"] = old_total_chunks
                if should_finalize:
                    target_utt["finalizer_running"] = False
                    self.active_finalizers.discard((session_id, utterance_id))
                    target_utt["state"] = "recording"
                try:
                    if chunk_json_path.exists():
                        chunk_json_path.unlink()
                except Exception:
                    pass
                raise IOError("メタ情報の保存に失敗しました。ディスク容量を確認してください。")

            if should_finalize:
                threading.Thread(target=self._finalize_utterance, args=(session_id, utterance_id), daemon=True).start()

        return {"ok": True, "session_id": session_id, "chunk_index": chunk_index, "state": "queued"}

    def stop_recording(self, session_id: str) -> dict:
        state = self.get_or_create_session(session_id)
        with self.lock:
            state["is_recording"] = False
            state["meta"]["is_recording"] = False
            self._save_meta_unlocked(state)
            pending_count = len(state["queue"]) + (1 if state["processing_chunk_index"] is not None else 0)
        return {"ok": True, "session_id": session_id, "is_recording": False, "pending_chunks": pending_count}

    def retry_failed(self, session_id: str, chunk_index: int = None) -> dict:
        state = self.get_or_create_session(session_id)
        session_dir = state["session_dir"]
        failed_dir = session_dir / "失敗音声"
        unprocessed_dir = session_dir / "未処理音声"
        chunks_dir = session_dir / "chunks"

        retried = []
        with self.lock:
            for f in sorted(chunks_dir.glob("chunk_*.json")):
                try:
                    cdata = json.loads(f.read_text(encoding="utf-8"))
                    idx = cdata.get("index")
                    if chunk_index is not None and idx != chunk_index:
                        continue
                    if cdata.get("state") == "failed":
                        # 音声ファイルを未処理へ戻す
                        pattern = f"chunk_{idx:04d}.*"
                        found_audio = list(failed_dir.glob(pattern)) or list(unprocessed_dir.glob(pattern))
                        if found_audio:
                            target_audio = unprocessed_dir / found_audio[0].name
                            if found_audio[0].parent != unprocessed_dir:
                                shutil.move(str(found_audio[0]), str(target_audio))
                            cdata["state"] = "queued"
                            cdata["error"] = None
                            f.write_text(json.dumps(cdata, ensure_ascii=False, indent=2), encoding="utf-8")
                            if idx not in state["queue"]:
                                state["queue"].append(idx)
                                state["queue"].sort()
                            retried.append(idx)
                except Exception:
                    pass
            state["meta"]["has_error"] = any(c.get("state") == "failed" for c in self._read_all_chunks_unlocked(session_dir))
            self._save_meta_unlocked(state)

        return {"ok": True, "session_id": session_id, "retried_chunks": retried}

    def get_session_status(self, session_id: str) -> dict:
        state = self.get_or_create_session(session_id)
        session_dir = state["session_dir"]

        with self.lock:
            is_recording = state["is_recording"]
            proc_idx = state["processing_chunk_index"]
            proc_step = state["processing_step"]
            queue_len = len(state["queue"])
            meta = dict(state["meta"])

        chunks = self._read_all_chunks(session_dir)
        completed_count = sum(1 for c in chunks if c.get("state") == "completed")
        failed_count = sum(1 for c in chunks if c.get("state") == "failed")
        has_error = failed_count > 0

        # 音声保持状況の集計
        unprocessed_audio_count = len(list((session_dir / "未処理音声").glob("chunk_*")))
        failed_audio_count = len(list((session_dir / "失敗音声").glob("chunk_*")))

        raw_path = session_dir / "原文.txt"
        polished_path = session_dir / "整文.txt"
        edited_path = session_dir / "編集済み.txt"

        cumulative_raw = raw_path.read_text(encoding="utf-8") if raw_path.is_file() else ""
        cumulative_polished = polished_path.read_text(encoding="utf-8") if polished_path.is_file() else ""
        edited_text = edited_path.read_text(encoding="utf-8") if edited_path.is_file() else ""

        is_processing = (proc_idx is not None) or (queue_len > 0)

        return {
            "ok": True,
            "session_id": session_id,
            "is_recording": is_recording,
            "is_processing": is_processing,
            "total_chunks": len(chunks),
            "completed_chunks": completed_count,
            "pending_chunks": queue_len,
            "failed_chunks": failed_count,
            "processing_chunk_index": proc_idx,
            "processing_step": proc_step,
            "has_error": has_error,
            "chunks": chunks,
            "cumulative_raw_text": cumulative_raw,
            "cumulative_polished_text": cumulative_polished,
            "edited_text": edited_text,
            "unprocessed_audio_count": unprocessed_audio_count,
            "failed_audio_count": failed_audio_count,
            "created_at": meta.get("created_at", ""),
            "paste_queue": meta.get("paste_queue", []),
            "speech_segments": meta.get("speech_segments", []),
            "active_cue_hold": meta.get("active_cue_hold", {"is_holding": False, "start_time_ms": None}),
            "utterances": meta.get("utterances", [])[-50:]
        }


    def finish_utterance(self, session_id: str, utterance_id: str, total_chunks: Optional[int] = None) -> dict:
        """F2発言終了後、クライアントからの最終音声送信完了通知を受理し、全区間0..expected-1の一意完了を確認して仕上げ処理を安全に起動します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            meta = state["meta"]
            target_utt = next((u for u in meta.get("utterances", []) if u.get("id") == utterance_id), None)
            if not target_utt:
                raise ValueError(f"発言ID {utterance_id} が見つかりません。")
            if target_utt.get("state") == "completed":
                return {"ok": True, "utterance_id": utterance_id, "state": "completed"}
            if not target_utt.get("closed"):
                raise ValueError("発言を終了してから仕上げを開始してください。")
            if total_chunks is not None and (not isinstance(total_chunks, int) or total_chunks < 0):
                raise ValueError("音声区間数が不正です。")
            if target_utt.get("finish_notified") and total_chunks is not None and target_utt.get("expected_chunks") != total_chunks:
                raise ValueError("通知済みの音声区間数と一致しません。")
            target_utt["finish_notified"] = True
            if total_chunks is not None:
                target_utt["expected_chunks"] = int(total_chunks)

            chunks = self._read_all_chunks_unlocked(state["session_dir"])
            utt_chunks = [c for c in chunks if c.get("utterance_id") == utterance_id]
            completed_chunks = [c for c in utt_chunks if c.get("state") == "completed"]
            failed_chunks = [c for c in utt_chunks if c.get("state") == "failed"]

            expected = target_utt.get("expected_chunks")
            if expected is None:
                if utt_chunks:
                    max_seq = max(c.get("seq_index", 0) for c in utt_chunks)
                    expected = max_seq + 1
                    target_utt["expected_chunks"] = expected
                else:
                    expected = target_utt.get("chunk_count", 0)
                    target_utt["expected_chunks"] = expected

            # 厳密な一意完了チェック: seq 0..expected-1 が欠番なく存在すること
            completed_seqs = {c.get("seq_index") for c in completed_chunks if c.get("seq_index") is not None}
            present_seqs = {c.get("seq_index") for c in utt_chunks if c.get("seq_index") is not None}
            required_seqs = set(range(expected))

            bound_mode = target_utt.get("recording_mode") or meta.get("recording_mode", "streaming")
            is_high_acc = bound_mode == "high_accuracy"
            if is_high_acc:
                ready_to_fin = (expected > 0 and present_seqs == required_seqs and not failed_chunks)
            else:
                ready_to_fin = (expected > 0 and completed_seqs == required_seqs and not failed_chunks)

            block = next((b for b in meta.get("blocks", []) if b.get("utterance_id") == utterance_id), None)
            if expected == 0:
                target_utt["state"] = "failed"
                target_utt["error"] = "発言の音声を取得できませんでした。"
                if block:
                    block["status"] = "failed"
            elif failed_chunks:
                target_utt["state"] = "failed"
                target_utt["error"] = "一部の音声チャンク認識に失敗しました。"
                if block:
                    block["status"] = "failed"
            elif ready_to_fin:
                # べき等性ガード: 既に実行中の場合は多重起動しない
                if not target_utt.get("finalizer_running") and (session_id, utterance_id) not in self.active_finalizers:
                    target_utt["finalizer_running"] = True
                    self.active_finalizers.add((session_id, utterance_id))
                    target_utt["state"] = "finalizing"
                    threading.Thread(target=self._finalize_utterance, args=(session_id, utterance_id), daemon=True).start()
            else:
                target_utt["state"] = "finalizing"
                if block:
                    block["status"] = "processing"
                # 欠落区間の救済または確定失敗タイムアウト:
                # 遅延到着を短時間待機し、それでも揃わない場合はfailedに遷移（永久にprocessingと偽装しない）
                threading.Thread(target=self._check_missing_chunks_timeout, args=(session_id, utterance_id), daemon=True).start()

            if not self._save_meta_unlocked(state):
                raise IOError("発言終了通知のメタ情報保存に失敗しました。ディスク容量を確認してください。")
            return {
                "ok": True,
                "utterance_id": utterance_id,
                "state": target_utt.get("state"),
                "completed_chunks": len(completed_seqs),
                "expected_chunks": expected
            }

    def _finalize_utterance(self, session_id: str, utterance_id: str):
        """対象発言の全区間のつながりを見た完成版を作成します。"""
        state = self.sessions.get(session_id)
        if not state:
            with self.lock:
                self.active_finalizers.discard((session_id, utterance_id))
            return
        session_dir = state["session_dir"]
        unprocessed_dir = session_dir / "未処理音声"

        try:
            with self.lock:
                meta = state["meta"]
                target_utt = next((u for u in meta.get("utterances", []) if u.get("id") == utterance_id), None)
                if not target_utt:
                    return
                target_block = next((b for b in meta.get("blocks", []) if b.get("utterance_id") == utterance_id or b.get("id") == target_utt.get("block_id")), None)
                target_utt["state"] = "finalizing"
                if target_block and target_block.get("status") not in ("pending_review", "completed"):
                    target_block["status"] = "finalizing"
                self._save_meta_unlocked(state)

            chunks = self._read_all_chunks(session_dir)
            utt_chunks = [c for c in chunks if c.get("utterance_id") == utterance_id]
            utt_chunks.sort(key=lambda c: c.get("seq_index", c.get("index", 0)))

            expected = target_utt.get("expected_chunks", 0)
            seqs = [c.get("seq_index") for c in utt_chunks if c.get("seq_index") is not None]
            has_failed_chunk = any(c.get("state") == "failed" for c in utt_chunks)
            missing_chunks = (expected > 0 and set(seqs) != set(range(expected))) or not utt_chunks

            if missing_chunks or has_failed_chunk:
                with self.lock:
                    target_utt["state"] = "failed"
                    target_utt["error"] = "一部の音声区間が欠落または認識失敗しています。全音声は未処理フォルダに保持されています。"
                    if target_block:
                        target_block["status"] = "failed"
                    self._save_meta_unlocked(state)
                # 欠落・失敗時は全音声を保持して安全終了
                return

            whisper_model = self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL)
            whisper_cmd = self.app_config.get("whisper_cmd", "whisper-cli")
            ffmpeg_cmd = self.app_config.get("ffmpeg_cmd", "ffmpeg")
            ollama_url = self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL)
            llm_model = self.app_config.get("llm_model", DEFAULT_LLM_MODEL)
            session_dict = dictionary.load_session_dictionary(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            common_dict = dictionary.load_common_dictionary(self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
            merged_dict = dictionary.merge_dictionaries(common_dict, session_dict)
            country_asr, _ = dictionary.country_code_prompts(session_dict)
            asr_prompt = ", ".join(part for part in (country_asr, dictionary.get_asr_prompt(merged_dict, max_chars=100)) if part)
            polishing_prompt = dictionary.get_polishing_prompt(merged_dict, max_entries=15)

            audio_paths = [unprocessed_dir / c.get("filename", "") for c in utt_chunks]
            if not audio_paths or not all(p.is_file() for p in audio_paths):
                with self.lock:
                    target_utt["state"] = "failed"
                    target_utt["error"] = "必要な音声ファイルの一部が見つかりません。一時音声を保持しています。"
                    if target_block:
                        target_block["status"] = "failed"
                    self._save_meta_unlocked(state)
                return

            total_dur = 0.0
            for p in audio_paths:
                try:
                    total_dur += mun_minutes.get_audio_duration(p, ffmpeg_cmd=ffmpeg_cmd)
                except Exception:
                    pass

            joined_raw = ""
            asr_error = None

            try:
                # 180秒以下の短時間発言: 一括結合して1パスASRを試行
                if total_dur <= 180.0 and len(audio_paths) > 0 and all(p.suffix.lower() == ".wav" for p in audio_paths):
                    with tempfile.TemporaryDirectory(prefix="mun-utterance-") as temp_dir:
                        combined = Path(temp_dir) / "speech.wav"
                        if mun_minutes.combine_wav_files(audio_paths, combined, max_seconds=180):
                            with self._asr_lock:
                                joined_raw = mun_minutes.transcribe(
                                    combined,
                                    whisper_model,
                                    whisper_cmd,
                                    ffmpeg_cmd,
                                    prompt=asr_prompt
                                )

                # 180秒超の長時間発言、またはcombine失敗時:
                # 適切な分割認識 / 順序結合で必ずASRする
                if not joined_raw:
                    chunk_texts = []
                    with tempfile.TemporaryDirectory(prefix="mun-long-split-") as temp_dir:
                        for p, c in zip(audio_paths, utt_chunks):
                            c_text = c.get("raw_text", "")
                            # 既にストリーミング個別認識済みの生原文があれば活用
                            if c_text and c.get("state") == "completed":
                                chunk_texts.append(c_text)
                                continue

                            c_dur = mun_minutes.get_audio_duration(p, ffmpeg_cmd=ffmpeg_cmd)
                            if c_dur <= 180.0:
                                with self._asr_lock:
                                    txt = mun_minutes.transcribe(p, whisper_model, whisper_cmd, ffmpeg_cmd, prompt=asr_prompt)
                                chunk_texts.append(txt)
                            else:
                                # 単一音声ファイルが180秒を超える場合（巨大直接アップロードなど）:
                                # 自然な無音境界で15〜30秒に安全分割して順序認識
                                sub_dir = Path(temp_dir) / f"sub_{c.get('index', 0)}"
                                sub_chunks = mun_minutes.split_audio_into_chunks(p, sub_dir, ffmpeg_cmd=ffmpeg_cmd)
                                sub_texts = []
                                for sc in sub_chunks:
                                    with self._asr_lock:
                                        s_txt = mun_minutes.transcribe(sc, whisper_model, whisper_cmd, ffmpeg_cmd, prompt=asr_prompt)
                                    sub_texts.append(s_txt)
                                merged_sub = mun_minutes.merge_chunk_texts_with_overlap_detection(sub_texts, has_audio_overlap=False)
                                chunk_texts.append(merged_sub)

                    joined_raw = mun_minutes.merge_chunk_texts_with_overlap_detection(chunk_texts, has_audio_overlap=False)

            except Exception as exc:
                asr_error = f"音声認識エラー: {exc}"

            if asr_error or not joined_raw or not joined_raw.strip():
                with self.lock:
                    target_utt["state"] = "failed"
                    target_utt["error"] = asr_error or "音声認識結果が空でした。未処理音声を保持しています。"
                    if target_block:
                        target_block["status"] = "failed"
                    self._save_meta_unlocked(state)
                # 失敗時は全音声を未処理フォルダに保持
                return

            # 整文（Qwen）
            final_text = ""
            finalize_error = None
            try:
                polishing_prompt = dictionary.get_polishing_prompt(merged_dict, max_entries=15, recognized_text=joined_raw)
                _, country_polish = dictionary.country_code_prompts(session_dict, recognized_text=joined_raw)
                if country_polish:
                    polishing_prompt = "\n\n".join(p for p in (polishing_prompt, country_polish) if p)
                doc_context = meeting_documents.get_bounded_context_for_polishing(session_dir, max_chars=350, query_text=joined_raw)
                if doc_context:
                    polishing_prompt = "\n\n".join(p for p in (polishing_prompt, doc_context) if p)
                final_text = mun_minutes.polish(joined_raw, ollama_url, llm_model, dictionary_prompt=polishing_prompt)
            except Exception as exc:
                finalize_error = f"整文エラー: {exc}"

            if finalize_error:
                with self.lock:
                    target_utt["state"] = "failed"
                    target_utt["error"] = finalize_error
                    if target_block:
                        target_block["status"] = "failed"
                    self._save_meta_unlocked(state)
                # 失敗時は全音声を未処理フォルダに保持
                return

            # 音声保持設定の処理（retain時は保存音声ディレクトリへ保管）
            retention_mode = target_utt.get("audio_retention") or state["meta"].get("audio_retention", "delete_on_success")
            saved_ok = True
            if retention_mode == "retain":
                saved_dir = session_dir / "保存音声"
                saved_dir.mkdir(parents=True, exist_ok=True)
                saved_path = saved_dir / f"{utterance_id}.wav"
                audio_files = [unprocessed_dir / c.get("filename", "") for c in utt_chunks if (unprocessed_dir / c.get("filename", "")).is_file()]
                try:
                    if len(audio_files) == 1:
                        shutil.copy2(audio_files[0], saved_path)
                    elif len(audio_files) > 1:
                        # 長時間音声でも上限なしで安全結合して恒久保存
                        saved_ok = mun_minutes.combine_wav_files(audio_files, saved_path, max_seconds=None)
                    saved_ok = saved_ok and saved_path.is_file() and (saved_path.stat().st_size > 0)
                except Exception:
                    saved_ok = False

                if not saved_ok:
                    with self.lock:
                        target_utt["state"] = "failed"
                        target_utt["error"] = "音声ファイルの恒久保存に失敗しました。原音を未処理音声に保持しています。"
                        if target_block:
                            target_block["status"] = "failed"
                        self._save_meta_unlocked(state)
                    # 保存失敗時は原音を必ず保持し、一時音声は絶対に削除しない
                    return

                # 有効期限（retention_days）に応じた期限切れ音声の自動クリーンアップ（0は無期限）
                with self.lock:
                    rd = state["meta"].get("retention_days", 0)
                    if rd and isinstance(rd, int) and rd > 0:
                        removed = cleanup_expired_audio(session_dir, rd)
                        if removed:
                            for block in state["meta"].get("blocks", []):
                                old_id = block.get("utterance_id")
                                if (block.get("has_saved_audio") and isinstance(old_id, str)
                                        and re.fullmatch(r"utt_[A-Za-z0-9_-]+", old_id)
                                        and not (saved_dir / f"{old_id}.wav").is_file()):
                                    block["has_saved_audio"] = False
                                    block.pop("audio_url", None)

            with self.lock:
                target_utt["state"] = "completed"
                target_utt["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                target_utt["error"] = None

                if target_block:
                    is_edited = target_block.get("user_edited", False)
                    is_copied = target_block.get("is_copied", False) or target_block.get("is_transcribed", False)

                    # 既に手編集またはコピー済みのセルは無断上書きせず、完成版候補として保持
                    if (is_edited or is_copied) and target_block.get("text") and final_text != target_block.get("text"):
                        target_block["final_candidate"] = final_text
                        target_block["candidate_status"] = "pending"
                        target_block["candidate_diff"] = mun_minutes.compute_text_diff(target_block.get("text", ""), final_text)
                        target_block["candidate_created_at"] = datetime.datetime.now().strftime("%H:%M:%S")
                        target_block["status"] = "pending_review"
                    else:
                        target_block["text"] = final_text
                        target_block["status"] = "needs_check" if final_text == joined_raw else "completed"
                        target_block["final_candidate"] = None
                        target_block["candidate_status"] = None
                        target_block["candidate_diff"] = None

                    target_block["raw_text"] = joined_raw
                    target_block["version"] = target_block.get("version", 1) + 1
                    target_block["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
                    if retention_mode == "retain":
                        saved_path = session_dir / "保存音声" / f"{utterance_id}.wav"
                        target_block["has_saved_audio"] = saved_path.is_file()
                        target_block["audio_url"] = f"/api/session/{session_id}/utterance/{utterance_id}/audio"

                # 耐障害的・原子的永続化: メタ情報保存の成功を確認してからのみ元音声を安全に削除
                save_ok = self._save_meta_unlocked(state)
                if not save_ok:
                    target_utt["state"] = "failed"
                    target_utt["error"] = "メタ情報の保存に失敗しました（空き容量不足等の可能性があります）。音声を未処理フォルダに保持しています。"
                    if target_block:
                        target_block["status"] = "failed"
                    return

                # メタ情報の永続化成功を確認してからのみ、当該発言の一時音声を安全削除
                for c in utt_chunks:
                    idx = c.get("index")
                    for fpath in unprocessed_dir.glob(f"chunk_{idx:04d}.*"):
                        try:
                            fpath.unlink()
                        except Exception:
                            pass
        except Exception as exc:
            with self.lock:
                target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utterance_id), None)
                if target_utt:
                    target_utt["state"] = "failed"
                    target_utt["error"] = f"仕上げ処理に失敗しました: {exc}"
                    for block in state["meta"].get("blocks", []):
                        if block.get("utterance_id") == utterance_id:
                            block["status"] = "failed"
                self._save_meta_unlocked(state)
        finally:
            with self.lock:
                target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utterance_id), None)
                if target_utt:
                    target_utt["finalizer_running"] = False
                    self._save_meta_unlocked(state)
                self.active_finalizers.discard((session_id, utterance_id))

    # === 会議資料・国名・高精度録音・音声保持 API 実装 ===

    def register_document(self, session_id: str, title: str, content: str) -> dict:
        state = self.get_or_create_session(session_id)
        with self.lock:
            doc = meeting_documents.register_document(state["session_dir"], title, content)
            self._save_meta_unlocked(state)
        return {"ok": True, "document": doc}

    def list_documents(self, session_id: str) -> list:
        state = self.get_or_create_session(session_id)
        return meeting_documents.list_documents(state["session_dir"])

    def get_document(self, session_id: str, doc_id: str) -> Optional[dict]:
        state = self.get_or_create_session(session_id)
        return meeting_documents.get_document(state["session_dir"], doc_id)

    def delete_document(self, session_id: str, doc_id: str) -> bool:
        state = self.get_or_create_session(session_id)
        with self.lock:
            res = meeting_documents.delete_document(state["session_dir"], doc_id)
            self._save_meta_unlocked(state)
        return res

    def extract_document_terms(self, session_id: str, doc_id: str, async_mode: bool = False, timeout_wait: float = 30.0) -> dict:
        """会議資料からの用語・コンテキスト抽出。HTTP接続を長時間ブロックしない非同期スレッド＋高速完了時即時応答"""
        state = self.get_or_create_session(session_id)
        doc = meeting_documents.get_document(state["session_dir"], doc_id)
        if not doc:
            raise ValueError(f"資料 {doc_id} が見つかりません。")
        ollama_url = self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL)
        llm_model = self.app_config.get("llm_model", DEFAULT_LLM_MODEL)

        with self.lock:
            catalog = meeting_documents._load_catalog(state["session_dir"])
            target = next((d for d in catalog.get("documents", []) if d.get("id") == doc_id), None)
            if not target:
                raise ValueError(f"資料 {doc_id} が見つかりません。")

            if target.get("extraction_status") == "running":
                return {
                    "ok": True,
                    "doc_id": doc_id,
                    "status": "running",
                    "progress": target.get("extraction_progress", {}),
                    "terms": target.get("candidate_terms", []),
                    "context_summaries": target.get("candidate_context_summaries", []),
                    "message": "抽出処理が既に実行中です。"
                }

            target["extraction_status"] = "running"
            target["extraction_progress"] = {"current": 0, "total": 0, "percent": 0}
            target["extraction_error"] = None
            meeting_documents._save_catalog(state["session_dir"], catalog)

        content_text = doc.get("content", "")
        finished_event = threading.Event()
        result_holder = {}

        def _worker():
            try:
                def on_progress(cur, tot):
                    with self.lock:
                        cat = meeting_documents._load_catalog(state["session_dir"])
                        t = next((d for d in cat.get("documents", []) if d.get("id") == doc_id), None)
                        if t:
                            t["extraction_progress"] = {"current": cur, "total": tot, "percent": int(cur / tot * 100) if tot > 0 else 0}
                            meeting_documents._save_catalog(state["session_dir"], cat)

                res = meeting_documents.extract_terms_and_context(
                    content_text,
                    ollama_url=ollama_url,
                    llm_model=llm_model,
                    progress_callback=on_progress
                )
                with self.lock:
                    cat = meeting_documents._load_catalog(state["session_dir"])
                    t = next((d for d in cat.get("documents", []) if d.get("id") == doc_id), None)
                    if t:
                        t["candidate_terms"] = res["terms"]
                        t["candidate_context_summaries"] = res["context_summaries"]
                        t["extracted_terms"] = res["terms"]
                        t["extraction_status"] = "completed"
                        t["status"] = "extracted"
                        tot = res.get("total_segments", 1)
                        t["extraction_progress"] = {"current": tot, "total": tot, "percent": 100}
                        meeting_documents._save_catalog(state["session_dir"], cat)
                result_holder["result"] = res
            except Exception as e:
                with self.lock:
                    cat = meeting_documents._load_catalog(state["session_dir"])
                    t = next((d for d in cat.get("documents", []) if d.get("id") == doc_id), None)
                    if t:
                        t["extraction_status"] = "failed"
                        t["extraction_error"] = str(e)
                        meeting_documents._save_catalog(state["session_dir"], cat)
                result_holder["error"] = str(e)
            finally:
                finished_event.set()

        t_thread = threading.Thread(target=_worker, daemon=True)
        t_thread.start()

        if async_mode:
            cat = meeting_documents._load_catalog(state["session_dir"])
            t = next((d for d in cat.get("documents", []) if d.get("id") == doc_id), None)
            prog = t.get("extraction_progress", {}) if t else {}
            return {
                "ok": True,
                "doc_id": doc_id,
                "status": "running",
                "progress": prog,
                "terms": [],
                "context_summaries": [],
                "message": "バックグラウンドで抽出を実行中です。GET /api/session/<id>/documents/<doc_id>/extract で進捗を確認できます。"
            }

        finished = finished_event.wait(timeout=timeout_wait)
        if finished:
            if "error" in result_holder:
                raise RuntimeError(f"用語抽出エラー: {result_holder['error']}")
            res = result_holder.get("result", {"terms": [], "context_summaries": []})
            return {
                "ok": True,
                "doc_id": doc_id,
                "status": "completed",
                "progress": {"current": res.get("total_segments", 1), "total": res.get("total_segments", 1), "percent": 100},
                "terms": res["terms"],
                "context_summaries": res["context_summaries"]
            }
        else:
            cat = meeting_documents._load_catalog(state["session_dir"])
            t = next((d for d in cat.get("documents", []) if d.get("id") == doc_id), None)
            prog = t.get("extraction_progress", {}) if t else {}
            return {
                "ok": True,
                "doc_id": doc_id,
                "status": "running",
                "progress": prog,
                "terms": [],
                "context_summaries": [],
                "message": "バックグラウンドで抽出を実行中です。GET /api/session/<id>/documents/<doc_id>/extract で進捗を確認できます。"
            }

    def get_document_extraction_status(self, session_id: str, doc_id: str) -> dict:
        """抽出処理の進捗・完了状態取得"""
        state = self.get_or_create_session(session_id)
        catalog = meeting_documents._load_catalog(state["session_dir"])
        target = next((d for d in catalog.get("documents", []) if d.get("id") == doc_id), None)
        if not target:
            raise ValueError(f"資料 {doc_id} が見つかりません。")
        return {
            "ok": True,
            "doc_id": doc_id,
            "status": target.get("extraction_status", "idle"),
            "progress": target.get("extraction_progress", {"current": 0, "total": 0, "percent": 0}),
            "terms": target.get("candidate_terms", target.get("extracted_terms", [])),
            "context_summaries": target.get("candidate_context_summaries", []),
            "confirmed_context_summaries": target.get("confirmed_context_summaries", []),
            "error": target.get("extraction_error")
        }

    def confirm_document_terms(self, session_id: str, doc_id: str, terms: list, context_summary: Optional[str] = None, context_summaries: Optional[list] = None, overwrite: bool = False) -> dict:
        with self.lock:
            return self._confirm_document_terms_locked(session_id, doc_id, terms, context_summary, context_summaries, overwrite)

    def _confirm_document_terms_locked(self, session_id: str, doc_id: str, terms: list, context_summary: Optional[str] = None, context_summaries: Optional[list] = None, overwrite: bool = False) -> dict:
        """用語および背景要約の確定登録（ユーザー明示確認時のみ背景適用・既存辞書の無断上書き防止）"""
        state = self.get_or_create_session(session_id)

        # 存在チェックを事前に行い、未存在資料で辞書を変更しない
        catalog = meeting_documents._load_catalog(state["session_dir"])
        target = next((d for d in catalog.get("documents", []) if d.get("id") == doc_id), None)
        if not target:
            raise ValueError(f"指定された資料ID '{doc_id}' が存在しません。")

        data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
        sdict = dictionary.load_session_dictionary(session_id, data_dir)
        existing_entries = sdict.setdefault("entries", [])

        added_count = 0
        skipped_existing = []
        for i, t in enumerate(terms):
            surf = (t.get("surface") or "").strip()
            if not surf:
                continue
            rep = (t.get("replacement") or surf).strip()
            cat = (t.get("category") or "資料確定").strip()
            desc = (t.get("description") or "").strip()
            enabled = bool(t.get("enabled", True))

            existing = next((e for e in existing_entries if e.get("surface") == surf), None)
            if existing:
                if overwrite:
                    existing["replacement"] = rep
                    existing["category"] = cat
                    existing["description"] = desc
                    existing["enabled"] = enabled
                    existing["status"] = "confirmed"
                    added_count += 1
                else:
                    skipped_existing.append(surf)
            else:
                entry = {
                    "id": f"doc_{time.time_ns()}_{i}",
                    "surface": surf,
                    "reading": (t.get("reading") or "").strip(),
                    "replacement": rep,
                    "category": cat,
                    "description": desc,
                    "enabled": enabled,
                    "status": "confirmed",
                    "tier": "session",
                    "source": "document"
                }
                existing_entries.append(entry)
                added_count += 1

        dictionary.save_session_dictionary(session_id, sdict, data_dir)

        # ユーザー確認操作を経て初めて背景要約を確定適用
        confirmed_summaries = []
        if context_summary and isinstance(context_summary, str) and context_summary.strip():
            confirmed_summaries.append(context_summary.strip())
        if context_summaries and isinstance(context_summaries, list):
            for s in context_summaries:
                if isinstance(s, str) and s.strip() and s.strip() not in confirmed_summaries:
                    confirmed_summaries.append(s.strip())

        if confirmed_summaries:
            target["confirmed_context_summaries"] = confirmed_summaries
            target["context_summaries"] = confirmed_summaries
        elif (context_summary is not None and not context_summary.strip()) or (context_summaries is not None and len(context_summaries) == 0):
            # 明示的な空文字または空リストによる解除指定
            target["confirmed_context_summaries"] = []
            target["context_summaries"] = []
            confirmed_summaries = []
        elif context_summary is None and context_summaries is None:
            cands = target.get("candidate_context_summaries", [])
            target["confirmed_context_summaries"] = list(cands)
            target["context_summaries"] = list(cands)
            confirmed_summaries = list(cands)
        meeting_documents._save_catalog(state["session_dir"], catalog)

        return {
            "ok": True,
            "doc_id": doc_id,
            "added_count": added_count,
            "skipped_existing": skipped_existing,
            "total_session_entries": len(existing_entries),
            "confirmed_context_summaries": confirmed_summaries
        }

    def confirm_terms(self, session_id: str, terms: list, overwrite: bool = False) -> dict:
        """議事録由来等の用語を直接確定登録（資料なし・既存辞書の無断上書き防止）"""
        state = self.get_or_create_session(session_id)
        data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
        sdict = dictionary.load_session_dictionary(session_id, data_dir)
        existing_entries = sdict.setdefault("entries", [])

        added_count = 0
        skipped_existing = []
        for i, t in enumerate(terms):
            surf = (t.get("surface") or "").strip()
            if not surf:
                continue
            rep = (t.get("replacement") or surf).strip()
            cat = (t.get("category") or "議事録確定").strip()
            desc = (t.get("description") or "").strip()
            enabled = bool(t.get("enabled", True))

            existing = next((e for e in existing_entries if e.get("surface") == surf), None)
            if existing:
                if overwrite:
                    existing["replacement"] = rep
                    existing["category"] = cat
                    existing["description"] = desc
                    existing["enabled"] = enabled
                    existing["status"] = "confirmed"
                    added_count += 1
                else:
                    skipped_existing.append(surf)
            else:
                entry = {
                    "id": f"min_{time.time_ns()}_{i}",
                    "surface": surf,
                    "reading": (t.get("reading") or "").strip(),
                    "replacement": rep,
                    "category": cat,
                    "description": desc,
                    "enabled": enabled,
                    "status": "confirmed",
                    "tier": "session",
                    "source": "minutes"
                }
                existing_entries.append(entry)
                added_count += 1

        dictionary.save_session_dictionary(session_id, sdict, data_dir)
        return {
            "ok": True,
            "added_count": added_count,
            "skipped_existing": skipped_existing,
            "total_session_entries": len(existing_entries)
        }

    def suggest_minutes_terms(self, session_id: str) -> list:
        state = self.get_or_create_session(session_id)
        data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
        cdict = dictionary.load_common_dictionary(self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
        sdict = dictionary.load_session_dictionary(session_id, data_dir)
        merged = dictionary.merge_dictionaries(cdict, sdict)
        existing_surfs = {e.get("surface") for e in merged.get("entries", [])}
        blocks = self.get_blocks(session_id)
        return meeting_documents.suggest_terms_from_minutes(blocks, existing_surfs)

    def export_external_prompt(self, session_id: str, doc_id: str) -> str:
        state = self.get_or_create_session(session_id)
        doc = meeting_documents.get_document(state["session_dir"], doc_id)
        if not doc:
            raise ValueError(f"資料 {doc_id} が見つかりません。")
        return meeting_documents.export_external_prompt(doc.get("title", ""), doc.get("content", ""))

    def import_external_response(self, session_id: str, doc_id: str, text: str) -> dict:
        with self.lock:
            return self._import_external_response_locked(session_id, doc_id, text)

    def _import_external_response_locked(self, session_id: str, doc_id: str, text: str) -> dict:
        state = self.get_or_create_session(session_id)
        result = meeting_documents.import_external_response(text)
        catalog = meeting_documents._load_catalog(state["session_dir"])
        target = next((d for d in catalog.get("documents", []) if d.get("id") == doc_id), None)
        if target:
            target["extracted_terms"] = result["terms"]
            target["candidate_terms"] = result["terms"]
            if result.get("context_summary"):
                target.setdefault("candidate_context_summaries", []).append(result["context_summary"])
            target["status"] = "external_imported"
            meeting_documents._save_catalog(state["session_dir"], catalog)
        return {"ok": True, "doc_id": doc_id, **result}

    def confirm_meeting_import(self, session_id: str, text: str, kind: str = "all") -> dict:
        parsed = meeting_import.parse_scoped(text, kind)
        with self.lock:
            self.get_or_create_session(session_id)
            data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
            existing = dictionary.load_session_dictionary(session_id, data_dir)
            merged, skipped = meeting_import.merge(existing, parsed)
            added_count = len(merged.get("entries", [])) - len(existing.get("entries", []))
            dictionary.save_session_dictionary(session_id, merged, data_dir)
        return {"ok": True, "added_count": added_count, "skipped_existing": skipped}

    def remove_meeting_context(self, session_id: str, text: str) -> dict:
        if not isinstance(text, str) or not text:
            raise ValueError("削除する背景を指定してください")
        with self.lock:
            self.get_or_create_session(session_id)
            data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
            saved = dictionary.load_session_dictionary(session_id, data_dir)
            contexts = saved.get("external_context", [])
            if text not in contexts:
                raise ValueError("背景が見つかりません。再読み込みしてください")
            saved["external_context"] = [item for item in contexts if item != text]
            dictionary.save_session_dictionary(session_id, saved, data_dir)
        return {"ok": True}

    def get_session_countries(self, session_id: str) -> dict:
        data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
        sdict = dictionary.load_session_dictionary(session_id, data_dir)
        return {
            "ok": True,
            "countries": sdict.get("countries", []),
            "country_codes": sdict.get("country_codes", [])
        }

    def set_session_countries(self, session_id: str, countries_data: Any) -> dict:
        data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
        sdict = dictionary.load_session_dictionary(session_id, data_dir)
        normalized, codes, warnings = dictionary.validate_and_normalize_countries(countries_data)
        sdict["countries"] = normalized
        sdict["country_codes"] = codes
        dictionary.save_session_dictionary(session_id, sdict, data_dir)
        return {
            "ok": True,
            "countries": normalized,
            "country_codes": codes,
            "warnings": warnings
        }

    def _check_missing_chunks_timeout(self, session_id: str, utterance_id: str, timeout_sec: float = 2.0):
        """欠落区間の遅延到着待機後、未解決ならfailedに遷移して永久待機を防ぐ"""
        time.sleep(timeout_sec)
        with self.lock:
            state = self.sessions.get(session_id)
            if not state:
                return
            meta = state["meta"]
            target_utt = next((u for u in meta.get("utterances", []) if u.get("id") == utterance_id), None)
            if not target_utt or target_utt.get("state") in ("completed", "failed") or target_utt.get("finalizer_running"):
                return
            chunks = self._read_all_chunks_unlocked(state["session_dir"])
            u_chunks = [c for c in chunks if c.get("utterance_id") == utterance_id]
            expected = target_utt.get("expected_chunks", 0)
            bound_mode = target_utt.get("recording_mode") or meta.get("recording_mode", "streaming")
            is_high_acc = bound_mode == "high_accuracy"
            if is_high_acc:
                present_seqs = {c.get("seq_index") for c in u_chunks if c.get("seq_index") is not None}
                required_seqs = set(range(expected))
                missing = sorted(list(required_seqs - present_seqs))
            else:
                completed_seqs = {c.get("seq_index") for c in u_chunks if c.get("state") == "completed" and c.get("seq_index") is not None}
                required_seqs = set(range(expected))
                missing = sorted(list(required_seqs - completed_seqs))

            if expected > 0 and missing:
                target_utt["state"] = "failed"
                target_utt["error"] = f"一部の音声区間（連番: {missing}）が未着または認識完了していません。再試行してください。"
                block = next((b for b in meta.get("blocks", []) if b.get("utterance_id") == utterance_id), None)
                if block:
                    block["status"] = "failed"
                self._save_meta_unlocked(state)

    def get_session_cue_status(self, session_id: str, since_ms: Optional[float] = None) -> dict:
        """キーポーリング専用の超軽量インメモリ状態取得（ファイルI/Oなし）"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            meta = state["meta"]
            active_cue = meta.get("active_cue_hold", {
                "is_holding": False,
                "start_time_ms": None
            })
            active_utt = meta.get("active_utterance")
            active_utt_id = active_utt.get("id") if active_utt else None
            utterances = meta.get("utterances", [])
            if since_ms is not None:
                try:
                    s_ms = float(since_ms)
                    # 長い発言中(not closed)やアクティブ発言、または指定時刻以降のものを確実に保持
                    utterances = [
                        u for u in utterances
                        if (not u.get("closed"))
                        or (u.get("id") == active_utt_id)
                        or (u.get("end_time_ms") or 0) >= s_ms
                        or (u.get("start_time_ms") or 0) >= s_ms
                    ]
                except (ValueError, TypeError):
                    pass

            return {
                "ok": True,
                "session_id": session_id,
                "is_recording": meta.get("is_recording", True),
                "recording_mode": meta.get("recording_mode", "streaming"),
                "active_cue_hold": active_cue,
                "active_utterance": active_utt,
                "active_utterance_id": active_utt_id,
                "utterances": [
                    {
                        "id": u.get("id"),
                        "block_id": u.get("block_id"),
                        "state": u.get("state"),
                        "label": u.get("label"),
                        "start_time_ms": u.get("start_time_ms"),
                        "end_time_ms": u.get("end_time_ms"),
                        "closed": u.get("closed", False),
                        "chunk_count": u.get("chunk_count", 0),
                        "expected_chunks": u.get("expected_chunks"),
                        "finish_notified": u.get("finish_notified", False),
                        "recording_mode": u.get("recording_mode"),
                        "audio_retention": u.get("audio_retention"),
                        "error": u.get("error")
                    }
                    for u in utterances
                ],
                "server_time_ms": time.time() * 1000.0
            }

    def get_session_settings(self, session_id: str) -> dict:
        state = self.get_or_create_session(session_id)
        with self.lock:
            meta = state["meta"]
            return {
                "ok": True,
                "recording_mode": meta.get("recording_mode", "streaming"),
                "audio_retention": meta.get("audio_retention", "delete_on_success"),
                "retention_days": meta.get("retention_days", 0)
            }

    def update_session_settings(self, session_id: str, settings: dict) -> dict:
        state = self.get_or_create_session(session_id)
        with self.lock:
            meta = state["meta"]
            rec_mode = settings.get("recording_mode")
            if rec_mode in ("streaming", "high_accuracy") and rec_mode != meta.get("recording_mode"):
                # 未処理キューまたは処理中発言がある場合はモード切替を拒否
                has_pending = (
                    len(state.get("queue", [])) > 0 or
                    state.get("processing_chunk_index") is not None or
                    any((not u.get("closed")) or u.get("state") in ("recording", "finalizing") for u in meta.get("utterances", []))
                )
                if has_pending:
                    raise ValueError("未処理の音声または実行中の発言があるため、録音モードの変更はできません。")
                meta["recording_mode"] = rec_mode

            ret_mode = settings.get("audio_retention")
            if ret_mode in ("delete_on_success", "retain"):
                meta["audio_retention"] = ret_mode

            if "retention_days" in settings:
                try:
                    rd = int(settings["retention_days"])
                    if rd >= 0:
                        meta["retention_days"] = rd
                except (ValueError, TypeError):
                    pass

            self._save_meta_unlocked(state)
            return {
                "ok": True,
                "recording_mode": meta.get("recording_mode", "streaming"),
                "audio_retention": meta.get("audio_retention", "delete_on_success"),
                "retention_days": meta.get("retention_days", 0)
            }

    def enqueue_utterance_audio(self, session_id: str, audio_bytes: bytes, utterance_id: Optional[str] = None, label: str = "", file_ext: str = ".wav") -> dict:
        if file_ext not in (".wav", ".webm"):
            raise ValueError("音声形式が不正です。")
        if len(audio_bytes) > MAX_AUDIO_UPLOAD_SIZE:
            raise ValueError(f"音声サイズが上限（{MAX_AUDIO_UPLOAD_SIZE // (1024*1024)}MB）を超えています。")
        state = self.get_or_create_session(session_id)
        with self.lock:
            if not utterance_id:
                u = self.start_utterance(session_id, press_time_ms=time.time() * 1000.0, label=label)
                utterance_id = u["utterance_id"]
                block_id = u["block_id"]
            else:
                target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utterance_id), None)
                if not target_utt:
                    u = self.start_utterance(session_id, press_time_ms=time.time() * 1000.0, label=label)
                    utterance_id = u["utterance_id"]
                    block_id = u["block_id"]
                else:
                    block_id = target_utt.get("block_id")

        unprocessed_dir = state["session_dir"] / "未処理音声"
        chunks_dir = state["session_dir"] / "chunks"
        chunk_idx = state["meta"].get("total_chunks", 0)
        audio_path = unprocessed_dir / f"chunk_{chunk_idx:04d}{file_ext}"
        existing_audio = list(unprocessed_dir.glob(f"chunk_{chunk_idx:04d}.*"))
        if existing_audio:
            audio_hash = hashlib.sha256(audio_bytes).hexdigest()
            if len(existing_audio) != 1 or existing_audio[0] != audio_path or hashlib.sha256(audio_path.read_bytes()).hexdigest() != audio_hash:
                raise ValueError("保存済みの音声区間と内容が異なるため上書きできません。")
        else:
            audio_path.write_bytes(audio_bytes)

        dur_sec = mun_minutes.get_audio_duration(audio_path)
        chunk_info = {
            "index": chunk_idx,
            "filename": audio_path.name,
            "duration_sec": dur_sec,
            "is_final": True,
            "state": "completed",
            "raw_text": "",
            "polished_text": "",
            "error": None,
            "utterance_id": utterance_id,
            "seq_index": 0,
            "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None
        }
        chunk_json_path = chunks_dir / f"chunk_{chunk_idx:04d}.json"
        chunk_json_path.write_text(json.dumps(chunk_info, ensure_ascii=False, indent=2), encoding="utf-8")

        with self.lock:
            state["meta"]["total_chunks"] = chunk_idx + 1
            target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utterance_id), None)
            if target_utt:
                target_utt["closed"] = True
                target_utt["chunk_count"] = 1
            if not self._save_meta_unlocked(state):
                raise IOError("一括音声のメタ情報保存に失敗しました。ディスク容量を確認してください。")

        return self.finish_utterance(session_id, utterance_id, total_chunks=1)

    def get_utterance_audio(self, session_id: str, utterance_id: str) -> Optional[Path]:
        if not isinstance(utterance_id, str) or not re.fullmatch(r"utt_[A-Za-z0-9_-]+", utterance_id):
            return None
        state = self.get_or_create_session(session_id)
        saved_path = state["session_dir"] / "保存音声" / f"{utterance_id}.wav"
        if saved_path.is_file():
            return saved_path
        unproc = state["session_dir"] / "未処理音声"
        for p in unproc.glob(f"*{utterance_id}*"):
            if p.is_file():
                return p
        return None

    def delete_utterance_audio(self, session_id: str, utterance_id: str) -> bool:
        if not isinstance(utterance_id, str) or not re.fullmatch(r"utt_[A-Za-z0-9_-]+", utterance_id):
            return False
        state = self.get_or_create_session(session_id)
        deleted = False
        saved_path = state["session_dir"] / "保存音声" / f"{utterance_id}.wav"
        if saved_path.is_file():
            try:
                saved_path.unlink()
                deleted = True
            except Exception:
                pass
        with self.lock:
            for b in state["meta"].get("blocks", []):
                if b.get("utterance_id") == utterance_id:
                    b["has_saved_audio"] = False
            self._save_meta_unlocked(state)
        return deleted

    def delete_all_session_audio(self, session_id: str) -> int:
        state = self.get_or_create_session(session_id)
        saved_dir = state["session_dir"] / "保存音声"
        count = 0
        if saved_dir.is_dir():
            for p in saved_dir.glob("*.wav"):
                try:
                    p.unlink()
                    count += 1
                except Exception:
                    pass
        with self.lock:
            for b in state["meta"].get("blocks", []):
                b["has_saved_audio"] = False
            self._save_meta_unlocked(state)
        return count

    def resolve_block_candidate(self, session_id: str, block_id: str, action: str, expected_version: Optional[int] = None, base_text: Optional[str] = None, custom_text: Optional[str] = None) -> dict:
        """完成版候補の適用・現状維持・カスタム編集を処理します。同時更新競合を検知します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            target = next((b for b in blocks if b.get("id") == block_id), None)
            if not target:
                raise ValueError(f"発言ブロック {block_id} が見つかりません。")

            if expected_version is not None and target.get("version") != int(expected_version):
                raise ConflictError("他の操作により発言セルが更新されています。最新の表示を確認してください。")
            if base_text is not None and target.get("text") != base_text:
                raise ConflictError("他の操作によりテキストが更新されています。最新の表示を確認してください。")

            if action == "apply":
                candidate = target.get("final_candidate")
                if not candidate:
                    raise ValueError("適用可能な完成版候補がありません。")
                target["text"] = candidate
                target["final_candidate"] = None
                target["candidate_diff"] = None
                target["candidate_status"] = "applied"
                target["user_edited"] = True
                target["status"] = "completed"
            elif action == "keep":
                target["final_candidate"] = None
                target["candidate_diff"] = None
                target["candidate_status"] = "dismissed"
                target["status"] = "completed"
            elif action == "custom":
                if custom_text is None:
                    raise ValueError("custom_text が指定されていません。")
                target["text"] = custom_text
                target["final_candidate"] = None
                target["candidate_diff"] = None
                target["candidate_status"] = "resolved"
                target["user_edited"] = True
                target["status"] = "completed"
            else:
                raise ValueError(f"不正なアクションです: {action}")

            target["version"] = target.get("version", 1) + 1
            target["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
            self._save_meta_unlocked(state)
            return dict(target)

    def retry_utterance(self, session_id: str, utterance_id: str) -> dict:
        """指定された発言の失敗したチャンクを再試行キューに戻し、仕上げ処理を再試行します。"""
        state = self.get_or_create_session(session_id)
        session_dir = state["session_dir"]
        failed_dir = session_dir / "失敗音声"
        unprocessed_dir = session_dir / "未処理音声"
        chunks_dir = session_dir / "chunks"

        retried = []
        with self.lock:
            meta = state["meta"]
            target_utt = next((u for u in meta.get("utterances", []) if u.get("id") == utterance_id), None)
            if not target_utt:
                raise ValueError(f"発言ID {utterance_id} が見つかりません。")
            if target_utt.get("expected_chunks") == 0:
                raise ValueError("音声が記録されていません。もう一度録音してください。")

            for f in sorted(chunks_dir.glob("chunk_*.json")):
                try:
                    cdata = json.loads(f.read_text(encoding="utf-8"))
                    if cdata.get("utterance_id") != utterance_id:
                        continue
                    idx = cdata.get("index")
                    if cdata.get("state") == "failed":
                        pattern = f"chunk_{idx:04d}.*"
                        found = list(failed_dir.glob(pattern)) or list(unprocessed_dir.glob(pattern))
                        if not found:
                            continue
                        if found and found[0].parent != unprocessed_dir:
                            shutil.move(str(found[0]), str(unprocessed_dir / found[0].name))
                        cdata["state"] = "queued"
                        cdata["error"] = None
                        f.write_text(json.dumps(cdata, ensure_ascii=False, indent=2), encoding="utf-8")
                        if idx not in state["queue"]:
                            state["queue"].append(idx)
                            state["queue"].sort()
                        retried.append(idx)
                except Exception:
                    pass

            target_utt["error"] = None
            target_utt["state"] = "finalizing"
            if not retried:
                chunks = self._read_all_chunks_unlocked(session_dir)
                completed = {c.get("seq_index") for c in chunks if c.get("utterance_id") == utterance_id and c.get("state") == "completed"}
                expected = target_utt.get("expected_chunks", 0)
                if expected and completed == set(range(expected)) and (session_id, utterance_id) not in self.active_finalizers:
                    target_utt["finalizer_running"] = True
                    self.active_finalizers.add((session_id, utterance_id))
                    threading.Thread(target=self._finalize_utterance, args=(session_id, utterance_id), daemon=True).start()
                else:
                    target_utt["state"] = "failed"
                    target_utt["error"] = "未送信または未完了の音声区間があります。"
                    self._save_meta_unlocked(state)
                    return {"ok": False, "error": target_utt["error"], "retried_chunks": retried}

            self._save_meta_unlocked(state)
        return {"ok": True, "session_id": session_id, "utterance_id": utterance_id, "retried_chunks": retried}

    def is_busy(self) -> bool:
        """処理中チャンク、キュー、または実行中・終了通知済みの未完了仕上げがあるか確認します。"""
        with self.lock:
            for state in self.sessions.values():
                if state.get("queue") or state.get("processing_chunk_index") is not None:
                    return True
                for utt in state.get("meta", {}).get("utterances", []):
                    if utt.get("finish_notified") and utt.get("state") not in ("completed", "failed"):
                        return True
            if self.active_finalizers:
                return True
            return False

    def reserve_block_copy(self, session_id: str, block_id: str) -> dict:
        """クリップボードへの転送前に原子的にテキストとバージョンを取得し、is_copied=Trueに設定して後発finalizeの上書きを防止します。"""
        state = self.get_or_create_session(session_id)
        with self.lock:
            blocks = state["meta"].setdefault("blocks", [])
            target = next((b for b in blocks if b.get("id") == block_id), None)
            if not target:
                raise ValueError(f"発言ブロック {block_id} が見つかりません。")

            target["is_copied"] = True
            target["copied_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_meta_unlocked(state)
            return {
                "ok": True,
                "block_id": block_id,
                "text": target.get("text", ""),
                "version": target.get("version", 1),
                "is_copied": True,
                "status": target.get("status")
            }

    def _read_all_chunks(self, session_dir: Path) -> list:
        chunks_dir = session_dir / "chunks"
        chunks = []
        if chunks_dir.is_dir():
            for f in sorted(chunks_dir.glob("chunk_*.json")):
                try:
                    chunks.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass
        chunks.sort(key=lambda x: x.get("index", 0))
        return chunks

    def _read_all_chunks_unlocked(self, session_dir: Path) -> list:
        return self._read_all_chunks(session_dir)

    def _save_meta_unlocked(self, state: dict) -> bool:
        session_dir = state["session_dir"]
        meta_path = session_dir / "メタ情報.json"
        temp_path = session_dir / f"メタ情報.json.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            temp_path.write_text(json.dumps(state["meta"], ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(meta_path)
            return True
        except Exception:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            return False

    def _worker_loop(self):
        """順序保証バックグラウンドワーカー"""
        while not self._stop_event.is_set():
            target_session_id = None
            target_chunk_idx = None

            with self.lock:
                for sid, s in self.sessions.items():
                    if s["queue"] and s["processing_chunk_index"] is None:
                        target_session_id = sid
                        target_chunk_idx = s["queue"].pop(0)
                        s["processing_chunk_index"] = target_chunk_idx
                        s["processing_step"] = "transcribing"
                        break

            if target_session_id is None:
                time.sleep(0.2)
                continue

            self._process_single_chunk(target_session_id, target_chunk_idx)

    def _process_single_chunk(self, session_id: str, chunk_index: int):
        state = self.sessions.get(session_id)
        if not state:
            return

        session_dir = state["session_dir"]
        unprocessed_dir = session_dir / "未処理音声"
        failed_dir = session_dir / "失敗音声"
        chunks_dir = session_dir / "chunks"
        chunk_json_path = chunks_dir / f"chunk_{chunk_index:04d}.json"

        # 音声ファイルの特定
        pattern = f"chunk_{chunk_index:04d}.*"
        matches = list(unprocessed_dir.glob(pattern))
        if not matches:
            # 音声ファイルが見当たらない場合
            with self.lock:
                state["processing_chunk_index"] = None
                state["processing_step"] = "idle"
            return

        audio_path = matches[0]

        # チャンクJSONを更新: transcribing
        chunk_data = {}
        if chunk_json_path.is_file():
            try:
                chunk_data = json.loads(chunk_json_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        chunk_data["state"] = "transcribing"
        chunk_json_path.write_text(json.dumps(chunk_data, ensure_ascii=False, indent=2), encoding="utf-8")

        utt_id = chunk_data.get("utterance_id")
        seg_id = chunk_data.get("segment_id")
        seq_idx = chunk_data.get("seq_index", chunk_index)

        whisper_model = self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL)
        whisper_cmd = self.app_config.get("whisper_cmd", "whisper-cli")
        ffmpeg_cmd = self.app_config.get("ffmpeg_cmd", "ffmpeg")
        ollama_url = self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL)
        llm_model = self.app_config.get("llm_model", DEFAULT_LLM_MODEL)

        raw_text = ""
        polished_text = ""
        process_error = None

        # 2階層辞書（共通辞書 + 会議別辞書）の読み込みとプロンプト生成
        common_dict = dictionary.load_common_dictionary(self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
        session_dict = dictionary.load_session_dictionary(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
        merged_dict = dictionary.merge_dictionaries(common_dict, session_dict)
        asr_prompt = dictionary.get_asr_prompt(merged_dict, max_chars=100)
        polishing_prompt = dictionary.get_polishing_prompt(merged_dict, max_entries=15)
        country_asr, country_polish = dictionary.country_code_prompts(session_dict)
        if country_asr:
            asr_prompt = ", ".join(part for part in (country_asr, asr_prompt) if part)
        if country_polish:
            polishing_prompt = "\n\n".join(part for part in (polishing_prompt, country_polish) if part)

        # 1. 音声認識 (Whisper) - 辞書候補をプロンプトとして適用
        try:
            with self._asr_lock:
                raw_text = mun_minutes.transcribe(audio_path, whisper_model, whisper_cmd, ffmpeg_cmd, prompt=asr_prompt)
        except Exception as exc:
            process_error = f"Whisper認識エラー: {exc}"

        # 発言中は認識原文を下書きとして表示し、整文は発言終了後にまとめて行う。
        if not process_error and utt_id:
            polished_text = raw_text
        # 発言管理外の従来ルートは従来どおり区間ごとに整文する。
        elif not process_error:
            with self.lock:
                state["processing_step"] = "polishing"
            chunk_data["state"] = "polishing"
            chunk_data["raw_text"] = raw_text
            chunk_json_path.write_text(json.dumps(chunk_data, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                polishing_prompt = dictionary.get_polishing_prompt(merged_dict, max_entries=15, recognized_text=raw_text)
                _, country_polish = dictionary.country_code_prompts(session_dict, recognized_text=raw_text)
                if country_polish:
                    polishing_prompt = "\n\n".join(part for part in (polishing_prompt, country_polish) if part)
                polished_text = mun_minutes.polish(raw_text, ollama_url, llm_model, dictionary_prompt=polishing_prompt)
            except Exception as exc:
                process_error = f"Qwen整文エラー: {exc}"

        # 3. 処理結果の反映と未処理音声の削除 / 失敗時保持
        with self.lock:
            state["processing_chunk_index"] = None
            state["processing_step"] = "idle"

            if process_error:
                # 失敗時: 音声は絶対に削除せず「失敗音声/」へ退避保持し、エラーを明示
                failed_audio_path = failed_dir / audio_path.name
                if audio_path.is_file():
                    shutil.move(str(audio_path), str(failed_audio_path))
                chunk_data["state"] = "failed"
                chunk_data["error"] = process_error
                chunk_data["raw_text"] = raw_text
                chunk_json_path.write_text(json.dumps(chunk_data, ensure_ascii=False, indent=2), encoding="utf-8")
                state["meta"]["has_error"] = True
                if utt_id:
                    target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utt_id), None)
                    if target_utt:
                        target_utt["state"] = "failed"
                        target_utt["error"] = process_error
                    target_block = next((b for b in state["meta"].get("blocks", []) if b.get("utterance_id") == utt_id), None)
                    if target_block:
                        target_block["status"] = "failed"
                state["meta"]["failed_chunks"] = state["meta"].get("failed_chunks", 0) + 1
                seg_id = chunk_data.get("segment_id")
                if seg_id:
                    for p_item in state["meta"].get("paste_queue", []):
                        if p_item.get("id") == seg_id:
                            p_item["state"] = "failed"
                            p_item["error"] = process_error
                            p_item["chunk_index"] = chunk_index
                    for s_item in state["meta"].get("speech_segments", []):
                        if s_item.get("id") == seg_id:
                            s_item["state"] = "failed"
                            s_item["error"] = process_error
                            s_item["chunk_index"] = chunk_index
            else:
                # 成功時: 結果を順次追加し、未処理音声を削除（永久保存はしない）
                chunk_data["state"] = "completed"
                chunk_data["raw_text"] = raw_text
                chunk_data["polished_text"] = polished_text
                chunk_data["finished_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                chunk_data["error"] = None
                chunk_json_path.write_text(json.dumps(chunk_data, ensure_ascii=False, indent=2), encoding="utf-8")

                # Cumulative 原文.txt / 整文.txt の追記
                raw_path = session_dir / "原文.txt"
                polished_path = session_dir / "整文.txt"
                with raw_path.open("a", encoding="utf-8") as f:
                    if raw_path.stat().st_size > 0:
                        f.write("\n\n")
                    f.write(raw_text)
                with polished_path.open("a", encoding="utf-8") as f:
                    if polished_path.stat().st_size > 0:
                        f.write("\n\n")
                    f.write(polished_text)

                # 音声の削除（発言管理外のチャンクのみ即時削除。発言チャンクは最終成功時に安全削除）
                utt_id = chunk_data.get("utterance_id")
                if not utt_id and audio_path.is_file():
                    try:
                        audio_path.unlink()
                    except Exception:
                        pass

                state["meta"]["completed_chunks"] = state["meta"].get("completed_chunks", 0) + 1

                # 発言セル（blocks）および貼付キューへの順序追記＆編集保護
                utt_id = chunk_data.get("utterance_id")
                seg_id = chunk_data.get("segment_id")
                seq_idx = chunk_data.get("seq_index", chunk_index)

                blocks = state["meta"].setdefault("blocks", [])
                target_block = None
                if utt_id:
                    target_block = next((b for b in blocks if b.get("utterance_id") == utt_id), None)
                if not target_block and seg_id:
                    target_block = next((b for b in blocks if b.get("id") == seg_id or b.get("utterance_id") == seg_id), None)

                # 空音声でなければ同一セルに順序どおり追記（保存済み編集は絶対に消さない）
                if polished_text and polished_text.strip():
                    if target_block:
                        target_block.setdefault("chunk_pieces", {})
                        already_present = str(seq_idx) in target_block["chunk_pieces"]
                        target_block["chunk_pieces"][str(seq_idx)] = polished_text.strip()

                        # ユーザー編集保護: 編集済みテキストがある場合は上書きせず末尾へ追記
                        if target_block.get("user_edited"):
                            cur_text = target_block.get("text", "")
                            # 重複追記防止
                            if not already_present:
                                target_block["text"] = f"{cur_text}{polished_text.strip()}" if cur_text else polished_text.strip()
                        else:
                            # 順序保証: seq_index 順に整列してセル文字列を構築
                            sorted_keys = sorted(target_block["chunk_pieces"].keys(), key=lambda k: int(k))
                            target_block["text"] = "".join(target_block["chunk_pieces"][k] for k in sorted_keys)

                        raw_existing = target_block.get("raw_text", "")
                        target_block["raw_text"] = f"{raw_existing} {raw_text}".strip() if raw_existing else raw_text
                        target_block["updated_at"] = datetime.datetime.now().strftime("%H:%M:%S")
                        target_block["version"] = target_block.get("version", 1) + 1

                # 従来の paste_queue / speech_segments への同期更新
                updated_item = False
                if seg_id or utt_id:
                    target_key = seg_id or utt_id
                    for p_item in state["meta"].get("paste_queue", []):
                        if p_item.get("id") == target_key or p_item.get("utterance_id") == utt_id:
                            p_item["text"] = target_block.get("text") if target_block else polished_text
                            p_item["state"] = "ready"
                            p_item["needs_audio"] = False
                            p_item["chunk_index"] = chunk_index
                            updated_item = True
                            break
                    for s_item in state["meta"].get("speech_segments", []):
                        if s_item.get("id") == target_key:
                            s_item["text"] = target_block.get("text") if target_block else polished_text
                            s_item["state"] = "completed"
                            s_item["needs_audio"] = False
                            s_item["chunk_index"] = chunk_index
                            break
                else:
                    for p_item in state["meta"].get("paste_queue", []):
                        if p_item.get("chunk_index") == chunk_index or (p_item.get("state") == "preparing" and not p_item.get("text") and not p_item.get("needs_audio")):
                            p_item["text"] = polished_text
                            p_item["state"] = "ready"
                            p_item["chunk_index"] = chunk_index
                            updated_item = True
                            break
                    for s_item in state["meta"].get("speech_segments", []):
                        if s_item.get("chunk_index") == chunk_index or (s_item.get("state") in ("pending_audio", "processing", "preparing") and not s_item.get("text") and not s_item.get("needs_audio")):
                            s_item["text"] = polished_text
                            s_item["state"] = "completed"
                            s_item["chunk_index"] = chunk_index
                            break

                if not updated_item and not target_block:
                    dur = chunk_data.get("duration_sec", 15.0)
                    lbl = chunk_data.get("label") or f"発言 #{len(state['meta'].get('paste_queue', [])) + 1}"
                    new_item = {
                        "id": seg_id or utt_id or f"seg_{len(state['meta'].get('paste_queue', [])) + 1:04d}",
                        "label": lbl,
                        "state": "ready",
                        "text": polished_text,
                        "duration_sec": dur,
                        "chunk_index": chunk_index,
                        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    state["meta"].setdefault("paste_queue", []).append(new_item)

            if utt_id:
                target_utt = next((u for u in state["meta"].get("utterances", []) if u.get("id") == utt_id), None)
                if target_utt and target_utt.get("finish_notified"):
                    expected = target_utt.get("expected_chunks", 0)
                    chunks = self._read_all_chunks_unlocked(session_dir)
                    u_chunks = [c for c in chunks if c.get("utterance_id") == utt_id]
                    completed_seqs = {c.get("seq_index") for c in u_chunks if c.get("state") == "completed" and c.get("seq_index") is not None}
                    required_seqs = set(range(expected))
                    if expected > 0 and completed_seqs == required_seqs:
                        if not target_utt.get("finalizer_running") and (session_id, utt_id) not in self.active_finalizers:
                            target_utt["finalizer_running"] = True
                            self.active_finalizers.add((session_id, utt_id))
                            target_utt["state"] = "finalizing"
                            threading.Thread(target=self._finalize_utterance, args=(session_id, utt_id), daemon=True).start()

            self._save_meta_unlocked(state)


def handle_transcribe_audio(
    audio_bytes: bytes,
    original_filename: str,
    whisper_model: Path,
    whisper_cmd: str,
    ffmpeg_cmd: str,
    ollama_url: str,
    llm_model: str,
    data_dir: Path
) -> dict:
    """単一音声ファイルの一括認識（ドラッグ＆ドロップ後方互換用）。"""
    session_dir = create_unique_session_dir(data_dir)
    session_id = session_dir.name

    ext = ".webm"
    if original_filename:
        _, file_ext = os.path.splitext(original_filename.lower())
        if file_ext in (".m4a", ".wav", ".webm", ".ogg", ".mp3", ".mp4"):
            ext = file_ext

    raw_audio_path = session_dir / f"音声{ext}"
    raw_audio_path.write_bytes(audio_bytes)

    meta = {
        "session_id": session_id,
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "original_filename": original_filename or f"recording{ext}",
        "raw_audio_size": len(audio_bytes),
        "whisper_model": str(whisper_model),
        "llm_model": llm_model,
        "status": "audio_saved"
    }

    raw_text = ""
    try:
        raw_text = mun_minutes.transcribe(raw_audio_path, whisper_model, whisper_cmd, ffmpeg_cmd)
        raw_path = session_dir / "原文.txt"
        with raw_path.open("x", encoding="utf-8") as f:
            f.write(raw_text)
        meta["status"] = "transcribed"
    except Exception as exc:
        meta["status"] = "transcribe_failed"
        meta["error"] = str(exc)
        (session_dir / "メタ情報.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError(f"Whisperによる音声認識に失敗しました: {exc}（音声ファイルは保持されています）") from exc

    polished_text = ""
    polish_error = None
    try:
        polished_text = mun_minutes.polish(raw_text, ollama_url, llm_model)
        polished_path = session_dir / "整文.txt"
        with polished_path.open("x", encoding="utf-8") as f:
            f.write(polished_text)
        meta["status"] = "completed"
    except Exception as exc:
        polish_error = str(exc)
        meta["status"] = "polish_failed"
        meta["error"] = polish_error

    (session_dir / "メタ情報.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if polish_error:
        return {
            "ok": False,
            "session_id": session_id,
            "created_at": meta["created_at"],
            "raw_text": raw_text,
            "polished_text": "",
            "error": f"整文処理でエラーが発生しました: {polish_error}（認識原文は保存されています）"
        }

    return {
        "ok": True,
        "session_id": session_id,
        "created_at": meta["created_at"],
        "raw_text": raw_text,
        "polished_text": polished_text
    }


def list_sessions(data_dir: Path) -> list:
    """保存されたセッション一覧を最新順で返します。"""
    if not data_dir.is_dir():
        return []

    sessions = []
    for item in sorted(data_dir.iterdir(), reverse=True):
        if not item.is_dir():
            continue

        raw_path = item / "原文.txt"
        polished_path = item / "整文.txt"
        edited_path = item / "編集済み.txt"
        meta_path = item / "メタ情報.json"

        created_at = ""
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                created_at = meta.get("created_at", "")
            except Exception:
                pass
        if not created_at:
            mtime = item.stat().st_mtime
            created_at = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

        preview = ""
        for p in (edited_path, polished_path, raw_path):
            if p.is_file():
                try:
                    txt = p.read_text(encoding="utf-8").strip()
                    if txt:
                        preview = txt[:80] + ("…" if len(txt) > 80 else "")
                        break
                except Exception:
                    pass

        sessions.append({
            "session_id": item.name,
            "created_at": created_at,
            "has_raw": raw_path.is_file(),
            "has_polished": polished_path.is_file(),
            "has_edited": edited_path.is_file(),
            "preview": preview
        })
    return sessions


def get_session_detail(session_id: str, data_dir: Path) -> dict:
    """特定のセッションの詳細テキストとメタデータを返します。"""
    session_dir = sanitize_session_id(session_id, data_dir)
    if not session_dir.is_dir():
        raise FileNotFoundError(f"セッション {session_id} が見つかりません。")

    raw_path = session_dir / "原文.txt"
    polished_path = session_dir / "整文.txt"
    edited_path = session_dir / "編集済み.txt"
    meta_path = session_dir / "メタ情報.json"

    raw_text = raw_path.read_text(encoding="utf-8") if raw_path.is_file() else ""
    polished_text = polished_path.read_text(encoding="utf-8") if polished_path.is_file() else ""
    edited_text = edited_path.read_text(encoding="utf-8") if edited_path.is_file() else ""

    meta = {}
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    return {
        "session_id": session_dir.name,
        "raw_text": raw_text,
        "polished_text": polished_text,
        "edited_text": edited_text,
        "meta": meta
    }


def save_session_edit(session_id: str, edited_text: str, data_dir: Path) -> dict:
    """手動編集されたテキストを 編集済み.txt に安全に保存します。"""
    session_dir = sanitize_session_id(session_id, data_dir)
    if not session_dir.is_dir():
        raise FileNotFoundError(f"セッション {session_id} が見つかりません。")

    edited_path = session_dir / "編集済み.txt"
    edited_path.write_text(edited_text, encoding="utf-8")

    meta_path = session_dir / "メタ情報.json"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["edited_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {"ok": True, "session_id": session_id}


class LanRequestHandler(SimpleHTTPRequestHandler):
    """
    LANリスナー専用ハンドラ（0.0.0.0:lan_port）。
    - 完全ホワイトリスト方式: 指定されたブロック同期API・参加API以外はすべて403で遮断。
    - 録音（/chunk, /speech-chunk, /cue-down, /cue-up）、モデル管理、辞書、セッション作成・終了は全拒否。
    - 静的ファイルも index.html 以外は遮断。
    - join_code は絶対に漏洩させない（/api/share/status でも非開示）。
    - 共有開始時の現在のsession_idだけにスコープを限定（他会議の閲覧・改変・一覧漏洩を完全防止）。
    - Strict Same-Origin（scheme + host + port の完全一致）検証。
    """
    def __init__(self, *args, app_config=None, queue_manager=None, sharing_manager=None, **kwargs):
        self.app_config = app_config or {}
        self.queue_manager = queue_manager
        self.sharing_manager = sharing_manager
        directory = str(self.app_config.get("web_dir", DEFAULT_WEB_DIR))
        super().__init__(*args, directory=directory, **kwargs)

    def validate_strict_same_origin(self) -> bool:
        host_hdr = self.headers.get("Host", "").strip()
        if not host_hdr:
            return False

        parsed_host = urllib.parse.urlsplit(f"//{host_hdr}")
        hostname = (parsed_host.hostname or "").lower()
        if not hostname:
            return False
        # loopback 以外のプライベートIPまたは localhost
        try:
            ip = ipaddress.ip_address(hostname)
            if not ip.is_private and not ip.is_loopback:
                return False
        except ValueError:
            if hostname not in ("localhost",) and not hostname.endswith(".local"):
                return False

        # Sec-Fetch-Site が cross-site の場合は即時遮断
        sec_fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if sec_fetch_site == "cross-site":
            return False

        # Origin がある場合は scheme + host + port が Host ヘッダーと 100% 完全一致することを検証
        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urllib.parse.urlsplit(origin)
            if parsed_origin.scheme not in ("http", "https"):
                return False
            origin_netloc = parsed_origin.netloc.lower()
            host_netloc = host_hdr.lower()
            if ":" not in origin_netloc and parsed_origin.scheme == "http":
                origin_netloc += ":80"
            if ":" not in host_netloc:
                host_netloc += ":80"
            if origin_netloc != host_netloc:
                return False

        # Referer がある場合も同様に Same-Origin を厳密検証
        referer = self.headers.get("Referer")
        if referer and not origin:
            parsed_ref = urllib.parse.urlsplit(referer)
            ref_netloc = parsed_ref.netloc.lower()
            host_netloc = host_hdr.lower()
            if ":" not in ref_netloc and parsed_ref.scheme == "http":
                ref_netloc += ":80"
            if ":" not in host_netloc:
                host_netloc += ":80"
            if ref_netloc != host_netloc:
                return False

        return True

    def send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def send_error_json(self, status: int, message: str):
        self.send_json(status, {"ok": False, "error": message})

    def extract_token(self) -> Optional[str]:
        token = self.headers.get("X-Share-Token")
        if not token:
            auth_hdr = self.headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()
        if not token:
            cookie_hdr = self.headers.get("Cookie", "")
            for part in cookie_hdr.split(";"):
                if "share_token=" in part:
                    token = part.split("share_token=")[1].strip()
                    break
        return token

    def do_GET(self):
        if not self.validate_strict_same_origin():
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なOriginヘッダーです（クロスオリジン拒否）。")
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        # 1. 静的画面: / または /index.html のみ許可（他ファイル探索禁止）
        if path in ("", "/", "/index.html"):
            index_path = self.app_config.get("web_dir", DEFAULT_WEB_DIR) / "index.html"
            if index_path.is_file():
                content = index_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(content)
                self.wfile.flush()
                return
            self.send_error_json(HTTPStatus.NOT_FOUND, "index.htmlが見つかりません。")
            return

        # 2. 共有ステータス（※join_code は絶対に含めない！）
        if path == "/api/share/status":
            status = self.sharing_manager.get_status(is_host=False)
            self.send_json(HTTPStatus.OK, {"ok": True, **status})
            return

        # 3.0 軽量キュー状態取得: GET /api/session/<id>/cue-status (共有セッション限定)
        if path.startswith("/api/session/") and path.endswith("/cue-status"):
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンがありません。接続コードを入力して参加してください。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。再接続してください。")
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/cue-status")])
            manager_session_id = self.sharing_manager.shared_session_id
            token_session_id = sess.get("session_id")
            if not manager_session_id or not token_session_id:
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象セッションが未設定または無効です。")
                return
            if session_id != manager_session_id or session_id != token_session_id:
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッションです。")
                return
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                qs = urllib.parse.parse_qs(parsed.query)
                since_ms = qs.get("since_ms", [None])[0]
                if self.queue_manager:
                    cue_status = self.queue_manager.get_session_cue_status(session_id, since_ms=since_ms)
                    self.send_json(HTTPStatus.OK, cue_status)
                    return
                else:
                    self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "キュー管理マネージャーが初期化されていません。")
                    return
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
                return

        # 3. 発言ブロック一覧取得: GET /api/session/<id>/blocks (現在の共有セッション限定)
        if path.startswith("/api/session/") and path.endswith("/blocks"):
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンがありません。接続コードを入力して参加してください。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。再接続してください。")
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks")])

            # 重大セキュリティガード（Fail-Closed二重検証）:
            # 1. マネージャーの共有セッションIDとトークンに紐づくセッションIDの両方が存在すること
            # 2. リクエストされた session_id がマネージャーの共有IDおよびトークン認可IDの双方と完全一致すること
            manager_session_id = self.sharing_manager.shared_session_id
            token_session_id = sess.get("session_id")
            if not manager_session_id or not token_session_id:
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象セッションが未設定または無効です。")
                return
            if session_id != manager_session_id or session_id != token_session_id:
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッション（他会議の議事録データ）です。閲覧は拒否されました。")
                return

            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            blocks = self.queue_manager.get_blocks(session_id) if self.queue_manager else []
            self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "blocks": blocks})
            return

        # 3.1 会議資料一覧取得: GET /api/session/<id>/documents (共有セッション限定)
        if path.startswith("/api/session/") and path.endswith("/documents"):
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンがありません。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。")
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/documents")])
            if session_id != self.sharing_manager.shared_session_id or session_id != sess.get("session_id"):
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッションです。")
                return
            docs = self.queue_manager.list_documents(session_id) if self.queue_manager else []
            self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "documents": docs})
            return

        # 3.1.1 会議資料用語抽出状態取得: GET /api/session/<id>/documents/<doc_id>/extract (共有セッション限定)
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/extract"):
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンがありません。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。")
                return
            sub = path[len("/api/session/"):-len("/extract")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            if session_id != self.sharing_manager.shared_session_id or session_id != sess.get("session_id"):
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッションです。")
                return
            res = self.queue_manager.get_document_extraction_status(session_id, doc_id) if self.queue_manager else {}
            self.send_json(HTTPStatus.OK, res)
            return

        # 3.2 参加国一覧取得: GET /api/session/<id>/countries (共有セッション限定)
        if path.startswith("/api/session/") and path.endswith("/countries"):
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンがありません。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。")
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/countries")])
            if session_id != self.sharing_manager.shared_session_id or session_id != sess.get("session_id"):
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッションです。")
                return
            res = self.queue_manager.get_session_countries(session_id) if self.queue_manager else {"countries": [], "country_codes": []}
            self.send_json(HTTPStatus.OK, res)
            return

        # 3.3 発言音声再聴: GET /api/session/<id>/utterance/<utt_id>/audio (共有セッション限定)
        if path.startswith("/api/session/") and "/utterance/" in path and path.endswith("/audio"):
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンがありません。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。")
                return
            sub = path[len("/api/session/"):-len("/audio")]
            parts = sub.split("/utterance/")
            session_id, utt_id = urllib.parse.unquote(parts[0]), parts[1]
            if session_id != self.sharing_manager.shared_session_id or session_id != sess.get("session_id"):
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッションです。")
                return
            audio_path = self.queue_manager.get_utterance_audio(session_id, utt_id) if self.queue_manager else None
            if not audio_path or not audio_path.is_file():
                self.send_error_json(HTTPStatus.NOT_FOUND, "音声は保存されていません。")
                return
            file_bytes = audio_path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "audio/wav" if audio_path.suffix.lower() == ".wav" else "audio/webm")
            self.send_header("Content-Length", str(len(file_bytes)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(file_bytes)
            self.wfile.flush()
            return

                # 4. セッション一覧取得: GET /api/sessions (過去会議一覧の漏洩防止・共有中セッションのみ返却)
        if path == "/api/sessions":
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが必要です。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。")
                return
            manager_session_id = self.sharing_manager.shared_session_id
            token_session_id = sess.get("session_id")
            if not manager_session_id or not token_session_id or manager_session_id != token_session_id:
                safe_sessions = []
            else:
                safe_sessions = [{"name": manager_session_id}]
            self.send_json(HTTPStatus.OK, {"ok": True, "sessions": safe_sessions})
            return

        # それ以外のすべてのGET（他ファイルや内部情報）は 403 Forbidden
        self.send_error_json(HTTPStatus.FORBIDDEN, "LANクライアントからはこのエンドポイントへのアクセスは許可されていません。")

    def do_POST(self):
        if not self.validate_strict_same_origin():
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なOriginヘッダーです（クロスオリジン拒否）。")
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なContent-Lengthヘッダーです。")
            return

        # 1. 接続コード参加: POST /api/share/join
        if path == "/api/share/join":
            try:
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                code_input = body.get("code", "")
                sess, err = self.sharing_manager.authenticate_code(code_input, self.client_address[0])
                if not sess:
                    if "ロックアウト" in (err or "") or "上限" in (err or ""):
                        self.send_error_json(HTTPStatus.TOO_MANY_REQUESTS, err)
                    else:
                        self.send_error_json(HTTPStatus.UNAUTHORIZED, err or "接続コードが無効です。")
                    return
                self.send_json(HTTPStatus.OK, {
                    "ok": True,
                    "token": sess["token"],
                    "role": sess["role"],
                    "session_id": sess.get("session_id")
                })
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"参加リクエストの解析に失敗しました: {exc}")
            return

        # 2. 退出: POST /api/share/leave
        if path == "/api/share/leave":
            token = self.extract_token()
            if token:
                self.sharing_manager.remove_token(token)
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        # 3. 発言ブロック更新・操作（要エディタートークン）
        if path.startswith("/api/session/") and "/blocks/" in path:
            token = self.extract_token()
            if not token:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが必要です。")
                return
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                self.send_error_json(HTTPStatus.UNAUTHORIZED, "認証トークンが無効または期限切れです。")
                return
            if sess.get("role") != "editor" and path.split("/blocks/")[1] != "copy":
                self.send_error_json(HTTPStatus.FORBIDDEN, "閲覧専用（Viewer）権限のため、発言ブロックの変更操作は許可されていません。")
                return

            action = path.split("/blocks/")[1]
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len(f"/blocks/{action}")])

            # 重大セキュリティガード（Fail-Closed二重検証）:
            # 1. マネージャーの共有セッションIDとトークンに紐づくセッションIDの両方が存在すること
            # 2. リクエストされた session_id がマネージャーの共有IDおよびトークン認可IDの双方と完全一致すること
            manager_session_id = self.sharing_manager.shared_session_id
            token_session_id = sess.get("session_id")
            if not manager_session_id or not token_session_id:
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象セッションが未設定または無効です。")
                return
            if session_id != manager_session_id or session_id != token_session_id:
                self.send_error_json(HTTPStatus.FORBIDDEN, "共有対象外のセッション（他会議の議事録データ）です。変更は拒否されました。")
                return

            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")

                if action == "update":
                    updated = self.queue_manager.update_block(session_id, body.get("block_id"), text=body.get("text"), is_transcribed=body.get("is_transcribed"), base_text=body.get("base_text"), expected_version=body.get("expected_version"))
                    self.send_json(HTTPStatus.OK, {"ok": True, "block": updated})
                    return
                elif action == "copy":
                    res = self.queue_manager.reserve_block_copy(session_id, body.get("block_id"))
                    self.send_json(HTTPStatus.OK, res)
                    return
                elif action == "resolve-candidate":
                    updated = self.queue_manager.resolve_block_candidate(session_id, body.get("block_id"), action=body.get("action", "apply"), expected_version=body.get("expected_version"), base_text=body.get("base_text"), custom_text=body.get("custom_text"))
                    self.send_json(HTTPStatus.OK, {"ok": True, "block": updated})
                    return
                elif action == "split":
                    parts = self.queue_manager.split_block(session_id, body.get("block_id"), first_text=body.get("first_text"), second_text=body.get("second_text"), split_index=body.get("split_index"))
                    self.send_json(HTTPStatus.OK, {"ok": True, "blocks": parts})
                    return
                elif action == "merge":
                    merged = self.queue_manager.merge_block(session_id, body.get("block_id"), target_id=body.get("target_id"))
                    self.send_json(HTTPStatus.OK, {"ok": True, "block": merged})
                    return
                elif action == "clear":
                    self.queue_manager.clear_blocks_view(session_id)
                    self.send_json(HTTPStatus.OK, {"ok": True, "message": "画面表示をクリアしました。"})
                    return
                elif action == "restore":
                    restored = self.queue_manager.restore_blocks(session_id)
                    self.send_json(HTTPStatus.OK, {"ok": True, "blocks": restored})
                    return
            except ConflictError as exc:
                self.send_error_json(HTTPStatus.CONFLICT, str(exc))
                return
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return

        # それ以外のすべてのPOST（録音/chunk, /speech-chunk, /cue-down, /cue-up, /models/..., /save等）は 403 Forbidden
        self.send_error_json(HTTPStatus.FORBIDDEN, "LANクライアントからはこの操作は許可されていません（ホストMac限定です）。")


class MunRequestHandler(SimpleHTTPRequestHandler):
    """ローカル専用HTTPリクエストハンドラ"""

    def __init__(self, *args, app_config=None, queue_manager=None, key_monitor=None, sharing_manager=None, whisper_downloader=None, ollama_puller=None, component_installer_instance=None, **kwargs):
        self.app_config = app_config or {}
        self.queue_manager = queue_manager
        self.key_monitor = key_monitor
        self.sharing_manager = sharing_manager or SharingManager()
        self.whisper_downloader = whisper_downloader or model_manager.WhisperDownloadManager()
        self.ollama_puller = ollama_puller or model_manager.OllamaPullManager()
        self.component_installer = component_installer_instance or component_installer.ComponentInstaller(PROJECT_DIR)
        directory = str(self.app_config.get("web_dir", DEFAULT_WEB_DIR))
        super().__init__(*args, directory=directory, **kwargs)

    def is_loopback(self) -> bool:
        client_ip = self.client_address[0]
        return client_ip in ("127.0.0.1", "::1", "localhost")

    def validate_host_and_origin(self) -> bool:
        host_hdr = self.headers.get("Host", "").strip()
        if not host_hdr:
            return False

        parsed_host = urllib.parse.urlsplit(f"//{host_hdr}")
        hostname = (parsed_host.hostname or "").lower()
        if hostname not in ("localhost", "127.0.0.1", "::1"):
            return False

        sec_fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
        if sec_fetch_site == "cross-site":
            return False

        origin = self.headers.get("Origin")
        if origin:
            parsed_origin = urllib.parse.urlsplit(origin)
            if parsed_origin.scheme not in ("http", "https"):
                return False
            origin_netloc = parsed_origin.netloc.lower()
            host_netloc = host_hdr.lower()
            if ":" not in origin_netloc and parsed_origin.scheme == "http":
                origin_netloc += ":80"
            if ":" not in host_netloc:
                host_netloc += ":80"
            if origin_netloc != host_netloc:
                return False

        referer = self.headers.get("Referer")
        if referer and not origin:
            parsed_ref = urllib.parse.urlsplit(referer)
            if parsed_ref.scheme not in ("http", "https"):
                return False
            ref_netloc = parsed_ref.netloc.lower()
            host_netloc = host_hdr.lower()
            if ":" not in ref_netloc and parsed_ref.scheme == "http":
                ref_netloc += ":80"
            if ":" not in host_netloc:
                host_netloc += ":80"
            if ref_netloc != host_netloc:
                return False

        return True

    def _verify_host_csrf(self) -> bool:
        if not self.require_host_csrf():
            self.send_error_json(HTTPStatus.FORBIDDEN, "CSRFトークンが無効または未指定です。")
            return False
        return True

    def require_host_csrf(self) -> bool:
        token = self.headers.get("X-Host-CSRF-Token", "").strip()
        expected = getattr(self.sharing_manager, "host_csrf_token", "")
        if not token or not expected or token != expected:
            return False
        return True

    def check_helper_or_csrf_access(self) -> tuple:
        if not self.is_loopback():
            return False, "ローカルループバック (127.0.0.1) からのみアクセス可能です。"

        has_browser_headers = bool(
            self.headers.get("Origin") or
            self.headers.get("Referer") or
            self.headers.get("Sec-Fetch-Site") or
            self.headers.get("Sec-Fetch-Mode")
        )

        csrf = self.headers.get("X-Host-CSRF-Token", "").strip()
        expected = getattr(self.sharing_manager, "host_csrf_token", "")

        if has_browser_headers:
            if not csrf or not expected or csrf != expected:
                return False, "CSRFトークンが未指定または無効です。"
        else:
            if csrf and expected and csrf != expected:
                return False, "認証トークンが無効です。"

        return True, None

    def check_client_access(self, require_edit: bool = False, require_host: bool = False) -> tuple:
        client_ip = self.client_address[0]
        is_loopback = client_ip in ("127.0.0.1", "::1", "localhost")

        if require_host and not is_loopback:
            return False, "この操作はホストMacのみ実行可能です（2台目クライアントからは実行できません）。", None

        token = self.headers.get("X-Share-Token")
        if not token:
            auth_hdr = self.headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()
        if not token:
            cookie_hdr = self.headers.get("Cookie", "")
            for part in cookie_hdr.split(";"):
                if "share_token=" in part:
                    token = part.split("share_token=")[1].strip()
                    break

        if token or not is_loopback:
            if not self.sharing_manager.enabled:
                return False, "2台目共有モードが無効です。ホストMacで共有を開始してください。", None
            if not token:
                return False, "認証トークンがありません。接続コードを入力して参加してください。", None
            sess = self.sharing_manager.validate_token(token)
            if not sess:
                return False, "認証トークンが無効または有効期限切れです。再接続してください。", None
            if require_edit and sess.get("role") != "editor":
                return False, "閲覧専用（Viewer）権限のため、編集操作は許可されていません。", None
            return True, None, sess

        return True, None, {"role": "host", "ip": client_ip}


    def send_json(self, status: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def send_error_json(self, status: int, message: str):
        self.send_json(status, {"ok": False, "error": message})

    def do_GET(self):
        if not self.validate_host_and_origin():
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なHostまたはOriginヘッダーです。")
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path == "/api/meeting-import/prompt":
            allowed, reason, _ = self.check_client_access(require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            try:
                kind = urllib.parse.parse_qs(parsed.query).get("kind", ["all"])[0]
                prompt = meeting_import.PROMPT if kind == "all" else meeting_import.make_prompt(kind)
                self.send_json(HTTPStatus.OK, {"ok": True, "prompt": prompt})
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 共有ステータス確認API (ホスト限定・join_code開示)
        if path == "/api/share/status":
            self.send_json(HTTPStatus.OK, {"ok": True, **self.sharing_manager.get_status(is_host=True)})
            return

        # 外部アクセス（LANクライアント）の基本ガード
        if not self.is_loopback() and not self.sharing_manager.enabled:
            self.send_error_json(HTTPStatus.FORBIDDEN, "2台目共有モードが無効です。ホストMac側で共有を開始してください。")
            return

        # 発言ブロック一覧取得API: GET /api/session/<id>/blocks
        if path.startswith("/api/session/") and path.endswith("/blocks"):
            allowed, reason, sess = self.check_client_access(require_edit=False)
            if not allowed:
                self.send_error_json(HTTPStatus.UNAUTHORIZED if "認証" in (reason or "") else HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            blocks = []
            if self.queue_manager:
                blocks = self.queue_manager.get_blocks(session_id)
            self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "blocks": blocks})
            return

        if path in ("", "/", "/index.html"):
            index_path = self.app_config.get("web_dir", DEFAULT_WEB_DIR) / "index.html"
            if index_path.is_file():
                html_text = index_path.read_text(encoding="utf-8")
                # ホスト専用 CSRF トークンを安全に注入
                csrf_token = getattr(self.sharing_manager, "host_csrf_token", "")
                injection = f'<script>window.__HOST_CSRF_TOKEN__ = "{csrf_token}";</script>\n</head>'
                html_text = html_text.replace("</head>", injection)
                content = html_text.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(content)
                self.wfile.flush()
                return
            else:
                self.send_error_json(HTTPStatus.NOT_FOUND, "index.htmlが見つかりません。")
                return

        if path == "/api/status":
            status_data = check_system_status(
                self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL),
                self.app_config.get("whisper_cmd", "whisper-cli"),
                self.app_config.get("ffmpeg_cmd", "ffmpeg"),
                self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL),
                self.app_config.get("llm_model", DEFAULT_LLM_MODEL),
            )
            self.send_json(HTTPStatus.OK, status_data)
            return

        # システム環境診断およびセットアップ状況: GET /api/setup/status
        if path == "/api/setup/status":
            setup_status = get_system_setup_status(self.app_config, self.key_monitor)
            self.send_json(HTTPStatus.OK, setup_status)
            return

        # 保存先および基本設定取得: GET /api/settings
        if path == "/api/settings":
            whisper_model_path = Path(self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL))
            models_dir = Path(self.app_config.get("models_dir", whisper_model_path.parent if whisper_model_path.parent.is_dir() else DEFAULT_MODELS_DIR))
            data_dir = Path(self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "settings": {
                    "data_dir": str(data_dir),
                    "models_dir": str(models_dir),
                    "whisper_model": str(whisper_model_path),
                    "whisper_cmd": self.app_config.get("whisper_cmd", "whisper-cli"),
                    "ffmpeg_cmd": self.app_config.get("ffmpeg_cmd", "ffmpeg"),
                    "ollama_url": self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL),
                    "llm_model": self.app_config.get("llm_model", DEFAULT_LLM_MODEL)
                },
                "default_settings": {
                    "data_dir": str(DEFAULT_DATA_DIR),
                    "models_dir": str(DEFAULT_MODELS_DIR),
                    "whisper_model": str(DEFAULT_WHISPER_MODEL),
                    "whisper_cmd": "whisper-cli",
                    "ffmpeg_cmd": "ffmpeg",
                    "ollama_url": DEFAULT_OLLAMA_URL,
                    "llm_model": DEFAULT_LLM_MODEL
                },
                "storage": {
                    "data_dir_free_gb": settings_manager.get_disk_free_gb(data_dir),
                    "models_dir_free_gb": settings_manager.get_disk_free_gb(models_dir),
                    "data_dir_writable": settings_manager.is_directory_writable(data_dir),
                    "models_dir_writable": settings_manager.is_directory_writable(models_dir)
                }
            })
            return

        # Whisperモデルダウンロード進捗取得: GET /api/models/whisper/download/status
        if path == "/api/models/whisper/download/status":
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "download": self.whisper_downloader.get_status()
            })
            return

        # OllamaモデルPull進捗取得: GET /api/models/llm/pull/status
        if path == "/api/models/llm/pull/status":
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "pull": self.ollama_puller.get_status()
            })
            return

        # 外部コンポーネント自動インストール進捗取得: GET /api/setup/install-component/status
        if path == "/api/setup/install-component/status":
            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "installer": self.component_installer.get_status()
            })
            return

        if path == "/api/sessions":
            sessions = list_sessions(self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            self.send_json(HTTPStatus.OK, sessions)
            return

        # /api/session/<id>/cue-status (キーポーリング専用の超軽量インメモリ状態API)
        if path.startswith("/api/session/") and path.endswith("/cue-status"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/cue-status")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                qs = urllib.parse.parse_qs(parsed.query)
                since_ms = qs.get("since_ms", [None])[0]
                if self.queue_manager:
                    cue_status = self.queue_manager.get_session_cue_status(session_id, since_ms=since_ms)
                    self.send_json(HTTPStatus.OK, cue_status)
                    return
                else:
                    self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "キュー管理マネージャーが初期化されていません。")
                    return
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
                return

        # /api/session/<id>/status
        if path.startswith("/api/session/") and path.endswith("/status"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/status")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if self.queue_manager:
                    status_info = self.queue_manager.get_session_status(session_id)
                    self.send_json(HTTPStatus.OK, status_info)
                    return
                else:
                    detail = get_session_detail(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                    self.send_json(HTTPStatus.OK, detail)
                    return
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
                return

        # 会議資料一覧取得: GET /api/session/<id>/documents
        if path.startswith("/api/session/") and path.endswith("/documents"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/documents")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                docs = self.queue_manager.list_documents(session_id) if self.queue_manager else []
                self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "documents": docs})
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return

        # 外部LLM用プロンプト取得: GET /api/session/<id>/documents/<doc_id>/external-prompt
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/external-prompt"):
            sub = path[len("/api/session/"):-len("/external-prompt")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                p = self.queue_manager.export_external_prompt(session_id, doc_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "doc_id": doc_id, "prompt": p})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議資料用語抽出状態取得: GET /api/session/<id>/documents/<doc_id>/extract
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/extract"):
            sub = path[len("/api/session/"):-len("/extract")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                res = self.queue_manager.get_document_extraction_status(session_id, doc_id)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議資料詳細取得: GET /api/session/<id>/documents/<doc_id>
        if path.startswith("/api/session/") and "/documents/" in path:
            sub = path[len("/api/session/"):]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                doc = self.queue_manager.get_document(session_id, doc_id)
                if not doc:
                    self.send_error_json(HTTPStatus.NOT_FOUND, f"資料 {doc_id} が見つかりません。")
                    return
                self.send_json(HTTPStatus.OK, {"ok": True, "document": doc})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if path.startswith("/api/session/") and path.endswith("/meeting-import/contexts"):
            allowed, reason, _ = self.check_client_access(require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/meeting-import/contexts")])
            try:
                data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
                sanitize_session_id(session_id, data_dir)
                saved = dictionary.load_session_dictionary(session_id, data_dir)
                self.send_json(HTTPStatus.OK, {"ok": True, "contexts": saved.get("external_context", [])})
            except (ValueError, OSError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 参加国一覧取得: GET /api/session/<id>/countries
        if path.startswith("/api/session/") and path.endswith("/countries"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/countries")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                res = self.queue_manager.get_session_countries(session_id)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議設定取得: GET /api/session/<id>/settings
        if path.startswith("/api/session/") and path.endswith("/settings"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/settings")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                res = self.queue_manager.get_session_settings(session_id)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 発言音声再聴取得: GET /api/session/<id>/utterance/<utt_id>/audio
        if path.startswith("/api/session/") and "/utterance/" in path and path.endswith("/audio"):
            sub = path[len("/api/session/"):-len("/audio")]
            parts = sub.split("/utterance/")
            session_id, utt_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                audio_path = self.queue_manager.get_utterance_audio(session_id, utt_id)
                if not audio_path or not audio_path.is_file():
                    self.send_error_json(HTTPStatus.NOT_FOUND, "音声は保存されていません（削除済みまたは未保存）。")
                    return
                content_bytes = audio_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "audio/wav" if audio_path.suffix.lower() == ".wav" else "audio/webm")
                self.send_header("Content-Length", str(len(content_bytes)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(content_bytes)
                self.wfile.flush()
                return
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

                # /api/session/<id>
        if path.startswith("/api/session/"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):])
            try:
                detail = get_session_detail(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                self.send_json(HTTPStatus.OK, detail)
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.NOT_FOUND, str(exc))
            return

        if path == "/api/dictionary/import-capabilities":
            self.send_json(HTTPStatus.OK, {"ok": True, "keep_existing": True})
            return

        # 辞書関連 GET API
        if path == "/api/dictionary/common":
            cdict = dictionary.load_common_dictionary(self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
            self.send_json(HTTPStatus.OK, cdict)
            return

        if path.startswith("/api/dictionary/session/"):
            session_id = urllib.parse.unquote(path[len("/api/dictionary/session/"):])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                sdict = dictionary.load_session_dictionary(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                self.send_json(HTTPStatus.OK, sdict)
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if path.startswith("/api/dictionary/merged/"):
            session_id = urllib.parse.unquote(path[len("/api/dictionary/merged/"):])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                cdict = dictionary.load_common_dictionary(self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
                sdict = dictionary.load_session_dictionary(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                merged = dictionary.merge_dictionaries(cdict, sdict)
                self.send_json(HTTPStatus.OK, merged)
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 貼付キュー GET API
        if path.startswith("/api/session/") and path.endswith("/paste-queue"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/paste-queue")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if self.queue_manager:
                    items = self.queue_manager.get_paste_queue(session_id)
                    self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "paste_queue": items})
                else:
                    self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "paste_queue": []})
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # モデル管理一覧取得: GET /api/models
        if path == "/api/models":
            ollama_url = self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL)
            current_llm = self.app_config.get("llm_model", DEFAULT_LLM_MODEL)
            installed_llms = get_installed_ollama_models(ollama_url)

            whisper_model_path = Path(self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL))
            models_dir = Path(self.app_config.get("models_dir", whisper_model_path.parent if whisper_model_path.parent.is_dir() else DEFAULT_MODELS_DIR))
            available_whisper = get_available_whisper_models(models_dir)

            available_filenames = {m["filename"] for m in available_whisper}
            whisper_catalog = []
            for item in model_manager.WHISPER_MODELS_CATALOG:
                c = dict(item)
                c["is_downloaded"] = c["filename"] in available_filenames
                whisper_catalog.append(c)

            llm_catalog = []
            for item in model_manager.OLLAMA_MODELS_CATALOG:
                c = dict(item)
                c["is_installed"] = any(c["model"] in inst for inst in installed_llms)
                llm_catalog.append(c)

            self.send_json(HTTPStatus.OK, {
                "ok": True,
                "llm": {
                    "current": current_llm,
                    "installed": installed_llms,
                    "ollama_url": ollama_url,
                    "is_available": current_llm in installed_llms or any(current_llm in m for m in installed_llms),
                    "catalog": llm_catalog,
                    "pull_status": self.ollama_puller.get_status()
                },
                "whisper": {
                    "current_filename": whisper_model_path.name,
                    "current_path": str(whisper_model_path),
                    "is_available": whisper_model_path.is_file(),
                    "available": available_whisper,
                    "catalog": whisper_catalog,
                    "models_dir": str(models_dir),
                    "download_status": self.whisper_downloader.get_status()
                }
            })
            return

        # Macグローバルキー支援状態取得: GET /api/mac-keys/status
        if path in ("/api/mac-keys/status", "/api/keys/status"):
            if self.key_monitor:
                status = self.key_monitor.get_status()
            else:
                status = mac_key_helper.MacGlobalKeyMonitor().get_status()
            self.send_json(HTTPStatus.OK, status)
            return

        # Mac発言キー動作モード取得: GET /api/mac-keys/mode (ホスト限定)
        if path in ("/api/mac-keys/mode", "/api/keys/mode"):
            mode = "toggle"
            if self.key_monitor:
                mode = getattr(self.key_monitor, "cue_mode", "toggle")
            self.send_json(HTTPStatus.OK, {"ok": True, "cue_mode": mode})
            return

        super().do_GET()

    def do_POST(self):
        if not self.validate_host_and_origin():
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なHostまたはOriginヘッダーです。")
            return

        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なContent-Lengthヘッダーです。")
            return

        # サーバーを終了する。録音や処理中は、完了を待ってから終了する。
        if path == "/api/shutdown":
            if not self._verify_host_csrf():
                return
            if self.queue_manager and self.queue_manager.is_busy():
                self.send_error_json(HTTPStatus.CONFLICT, "発言の仕上げ処理または文字化が完了してから終了してください。")
                return
            if self.whisper_downloader.get_status().get("status") in ("downloading", "verifying") or self.ollama_puller.get_status().get("status") == "pulling":
                self.send_error_json(HTTPStatus.CONFLICT, "モデルのダウンロードが完了してから終了してください。")
                return
            self.send_json(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        # 1. 2台目共有 参加API: POST /api/share/join (ゲスト端末の接続コード認証)
        if path == "/api/share/join":
            if not self.sharing_manager.enabled:
                self.send_error_json(HTTPStatus.FORBIDDEN, "2台目共有モードが無効です。ホストMac側で共有を開始してください。")
                return
            try:
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                code_input = body.get("code", "")
                sess = self.sharing_manager.authenticate_code(code_input, self.client_address[0])
                if not sess:
                    self.send_error_json(HTTPStatus.UNAUTHORIZED, "接続コードが正しくありません。ホストMac画面に表示されたコードを確認してください。")
                    return
                self.send_json(HTTPStatus.OK, {
                    "ok": True,
                    "token": sess["token"],
                    "role": sess["role"],
                    "session_id": sess.get("session_id")
                })
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"参加リクエストの解析に失敗しました: {exc}")
            return

        # 2. 2台目共有 退出API: POST /api/share/leave
        if path == "/api/share/leave":
            token = self.headers.get("X-Share-Token", "")
            self.sharing_manager.remove_token(token)
            self.send_json(HTTPStatus.OK, {"ok": True})
            return

        # 3. 共有モード切替API: POST /api/share/toggle (ホスト限定・CSRF必須)
        if path == "/api/share/toggle":
            allowed, reason, _ = self.check_client_access(require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            if not self.require_host_csrf():
                self.send_error_json(HTTPStatus.FORBIDDEN, "CSRFトークンが未指定または無効です。")
                return
            try:
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                enabled = body.get("enabled", False)
                role = body.get("role", "editor")
                session_id = body.get("session_id")

                if enabled:
                    if not session_id or not isinstance(session_id, str) or not session_id.strip():
                        self.send_error_json(HTTPStatus.BAD_REQUEST, "共有対象の会議セッションID（session_id）を明示的に指定してください。")
                        return
                    session_id = session_id.strip()
                    data_dir = self.app_config.get("data_dir", DEFAULT_DATA_DIR)
                    sanitize_session_id(session_id, data_dir)
                    sess_path = Path(data_dir) / session_id
                    if not sess_path.is_dir():
                        self.send_error_json(HTTPStatus.BAD_REQUEST, f"指定されたセッション {session_id} が存在しません。")
                        return
                else:
                    session_id = None

                res = self.sharing_manager.toggle_sharing(
                    enabled,
                    role,
                    session_id=session_id,
                    app_config=self.app_config,
                    queue_manager=self.queue_manager
                )
                self.send_json(HTTPStatus.OK, {"ok": True, **res})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 4. 共有強制切断API: POST /api/share/terminate (ホスト限定・CSRF必須)
        if path == "/api/share/terminate":
            allowed, reason, _ = self.check_client_access(require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            if not self.require_host_csrf():
                self.send_error_json(HTTPStatus.FORBIDDEN, "CSRFトークンが未指定または無効です。")
                return
            self.sharing_manager.terminate_all()
            self.send_json(HTTPStatus.OK, {"ok": True, "message": "全2台目接続を安全に切断しました。"})
            return

        # 発言ブロックコピー予約API: POST /api/session/<id>/blocks/<block_id>/copy または /blocks/copy
        if path.startswith("/api/session/") and "/blocks/" in path and path.endswith("/copy"):
            allowed, reason, _ = self.check_client_access()
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            parts = path.split("/blocks/")
            session_id = urllib.parse.unquote(parts[0][len("/api/session/"):])
            sub = parts[1][:-len("/copy")]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                block_id = sub if sub else payload.get("block_id")
                if not block_id:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "block_id が指定されていません。")
                    return
                if self.queue_manager:
                    res = self.queue_manager.reserve_block_copy(session_id, block_id)
                    self.send_json(HTTPStatus.OK, res)
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 5. 発言ブロック更新API: POST /api/session/<id>/blocks/update
        if path.startswith("/api/session/") and path.endswith("/blocks/update"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks/update")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                if not self.queue_manager:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "キューマネージャーが初期化されていません。")
                    return
                updated = self.queue_manager.update_block(
                    session_id,
                    body.get("block_id"),
                    text=body.get("text"),
                    is_transcribed=body.get("is_transcribed"),
                    base_text=body.get("base_text"),
                    expected_version=body.get("expected_version")
                )
                self.send_json(HTTPStatus.OK, {"ok": True, "block": updated})
            except ConflictError as exc:
                self.send_error_json(HTTPStatus.CONFLICT, str(exc))
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 6. 発言ブロック分割API: POST /api/session/<id>/blocks/split
        if path.startswith("/api/session/") and path.endswith("/blocks/split"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks/split")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                if not self.queue_manager:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "キューマネージャーが初期化されていません。")
                    return
                parts = self.queue_manager.split_block(
                    session_id,
                    body.get("block_id"),
                    first_text=body.get("first_text"),
                    second_text=body.get("second_text"),
                    split_index=body.get("split_index")
                )
                self.send_json(HTTPStatus.OK, {"ok": True, "blocks": parts})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 7. 発言ブロック結合API: POST /api/session/<id>/blocks/merge
        if path.startswith("/api/session/") and path.endswith("/blocks/merge"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks/merge")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                if not self.queue_manager:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "キューマネージャーが初期化されていません。")
                    return
                merged = self.queue_manager.merge_block(
                    session_id,
                    body.get("block_id"),
                    target_id=body.get("target_id")
                )
                self.send_json(HTTPStatus.OK, {"ok": True, "block": merged})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 8. 発言ブロック画面クリアAPI: POST /api/session/<id>/blocks/clear
        if path.startswith("/api/session/") and path.endswith("/blocks/clear"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks/clear")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if not self.queue_manager:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "キューマネージャーが初期化されていません。")
                    return
                self.queue_manager.clear_blocks_view(session_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "message": "画面表示をクリアしました（サーバー保存履歴は保護されています）。"})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 9. 発言ブロック復元API: POST /api/session/<id>/blocks/restore
        if path.startswith("/api/session/") and path.endswith("/blocks/restore"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/blocks/restore")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if not self.queue_manager:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "キューマネージャーが初期化されていません。")
                    return
                restored = self.queue_manager.restore_blocks(session_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "blocks": restored})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # セッション開始: POST /api/session/start (新規作成会議は既定で high_accuracy)
        if path == "/api/session/start":
            session_dir = create_unique_session_dir(self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            session_id = session_dir.name
            if self.queue_manager:
                state = self.queue_manager.get_or_create_session(session_id)
                with self.queue_manager.lock:
                    state["meta"]["recording_mode"] = "high_accuracy"
                    self.queue_manager._save_meta_unlocked(state)
            self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id})
            return

        # 録音中の画面をグローバルキーの対象として指定する。
        if path.startswith("/api/session/") and path.endswith("/activate"):
            allowed, reason = self.check_helper_or_csrf_access()
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/activate")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                self.queue_manager.get_or_create_session(session_id)
                with self.queue_manager.lock:
                    self.queue_manager.active_session_id = session_id
                self.send_json(HTTPStatus.OK, {"ok": True})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 音声チャンク送信: POST /api/session/<id>/chunk
        if path.startswith("/api/session/") and path.endswith("/chunk"):
            allowed, reason, _ = self.check_client_access(require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/chunk")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return

            if content_length <= 0:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "チャンク音声データが空です。")
                return

            try:
                chunk_index = sanitize_chunk_index(self.headers.get("X-Chunk-Index", 0))
                duration_sec = float(self.headers.get("X-Duration-Sec", 0.0))
                is_final = self.headers.get("X-Is-Final", "false").lower() == "true"
            except ValueError as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"ヘッダー値が不正です: {exc}")
                return

            audio_bytes = self.rfile.read(content_length)

            ext = ".webm"
            content_type = self.headers.get("Content-Type", "")
            if "wav" in content_type:
                ext = ".wav"
            elif "ogg" in content_type:
                ext = ".ogg"
            elif "mp4" in content_type:
                ext = ".mp4"

            if self.queue_manager:
                res = self.queue_manager.enqueue_chunk(session_id, chunk_index, audio_bytes, duration_sec, is_final, file_ext=ext)
                self.send_json(HTTPStatus.OK, res)
            else:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            return

        # 録音停止: POST /api/session/<id>/stop
        if path.startswith("/api/session/") and path.endswith("/stop"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/stop")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if self.queue_manager:
                    res = self.queue_manager.stop_recording(session_id)
                    self.send_json(HTTPStatus.OK, res)
                else:
                    self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id})
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 失敗チャンク再試行: POST /api/session/<id>/retry
        if path.startswith("/api/session/") and path.endswith("/retry"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/retry")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                chunk_index = None
                if content_length > 0:
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if "chunk_index" in payload:
                        chunk_index = sanitize_chunk_index(payload["chunk_index"])
                if self.queue_manager:
                    res = self.queue_manager.retry_failed(session_id, chunk_index)
                    self.send_json(HTTPStatus.OK, res)
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 既存の一括文字起こし (ドラッグ＆ドロップ後方互換用)
        if path == "/api/transcribe":
            if content_length <= 0:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "音声データが送信されていません。")
                return
            audio_bytes = self.rfile.read(content_length)
            raw_filename = self.headers.get("X-Audio-Filename", "recording.webm")
            filename = urllib.parse.unquote(raw_filename)

            try:
                result = handle_transcribe_audio(
                    audio_bytes=audio_bytes,
                    original_filename=filename,
                    whisper_model=self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL),
                    whisper_cmd=self.app_config.get("whisper_cmd", "whisper-cli"),
                    ffmpeg_cmd=self.app_config.get("ffmpeg_cmd", "ffmpeg"),
                    ollama_url=self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL),
                    llm_model=self.app_config.get("llm_model", DEFAULT_LLM_MODEL),
                    data_dir=self.app_config.get("data_dir", DEFAULT_DATA_DIR),
                )
                self.send_json(HTTPStatus.OK, result)
            except Exception as exc:
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
            return

        # 手動編集の保存
        if path == "/api/save":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"JSON解析エラー: {exc}")
                return

            session_id = payload.get("session_id", "")
            edited_text = payload.get("edited_text", "")
            try:
                res = save_session_edit(session_id, edited_text, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                self.send_json(HTTPStatus.OK, res)
            except (ValueError, FileNotFoundError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 辞書関連 POST API
        if path == "/api/dictionary/common":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                dictionary.save_common_dictionary(payload, self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
                self.send_json(HTTPStatus.OK, {"ok": True, "message": "共通辞書を保存しました。"})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"共通辞書保存エラー: {exc}")
            return

        if path.startswith("/api/dictionary/session/"):
            session_id = urllib.parse.unquote(path[len("/api/dictionary/session/"):])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                dictionary.save_session_dictionary(session_id, payload, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "message": "会議別辞書を保存しました。"})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"会議別辞書保存エラー: {exc}")
            return

        if path == "/api/dictionary/import":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                target_type = payload.get("target", "session")  # common or session
                session_id = payload.get("session_id", "")
                json_content = payload.get("json_content", "")
                keep_existing = payload.get("keep_existing", False) is True

                if target_type == "common":
                    target_dict = dictionary.load_common_dictionary(self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
                    updated, conflicts = dictionary.import_dictionary_json(json_content, target_dict, keep_existing=keep_existing)
                    dictionary.save_common_dictionary(updated, self.app_config.get("common_dict_path", DEFAULT_COMMON_DICT_PATH))
                else:
                    sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                    target_dict = dictionary.load_session_dictionary(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                    updated, conflicts = dictionary.import_dictionary_json(json_content, target_dict, keep_existing=keep_existing)
                    dictionary.save_session_dictionary(session_id, updated, self.app_config.get("data_dir", DEFAULT_DATA_DIR))

                self.send_json(HTTPStatus.OK, {
                    "ok": True,
                    "target": target_type,
                    "imported_entries": len(updated.get("entries", [])),
                    "conflicts": conflicts
                })
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, f"辞書インポートエラー: {exc}")
            return

        # 発言ブロック完成版候補解決API: POST /api/session/<id>/blocks/<block_id>/resolve-candidate または /blocks/resolve-candidate
        if path.startswith("/api/session/") and "/blocks/" in path and path.endswith("/resolve-candidate"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            parts = path.split("/blocks/")
            session_id = urllib.parse.unquote(parts[0][len("/api/session/"):])
            sub = parts[1][:-len("/resolve-candidate")]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                block_id = sub if sub else payload.get("block_id")
                if not block_id:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "block_id が指定されていません。")
                    return
                action = payload.get("action", "apply")
                exp_ver = payload.get("expected_version")
                b_text = payload.get("base_text")
                c_text = payload.get("custom_text")
                if self.queue_manager:
                    res = self.queue_manager.resolve_block_candidate(
                        session_id, block_id, action=action, expected_version=exp_ver, base_text=b_text, custom_text=c_text
                    )
                    self.send_json(HTTPStatus.OK, {"ok": True, "block": res})
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except ConflictError as exc:
                self.send_error_json(HTTPStatus.CONFLICT, str(exc))
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 発言終了・最終音声送信完了通知: POST /api/session/<id>/utterance/finish または /utterance/<utt_id>/finish
        if path.startswith("/api/session/") and ("/utterance/finish" in path or ("/utterance/" in path and path.endswith("/finish"))):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                if "/utterance/finish" in path:
                    session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/utterance/finish")])
                    utterance_id = payload.get("utterance_id")
                else:
                    sub = path[len("/api/session/"):-len("/finish")]
                    session_id, utterance_id = [urllib.parse.unquote(p) for p in sub.split("/utterance/")]

                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                total_chunks = payload.get("total_chunks")
                if not utterance_id:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "utterance_id が指定されていません。")
                    return
                if self.queue_manager:
                    res = self.queue_manager.finish_utterance(session_id, utterance_id, total_chunks)
                    self.send_json(HTTPStatus.OK, res)
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 発言再試行: POST /api/session/<id>/utterance/<utt_id>/retry
        if path.startswith("/api/session/") and "/utterance/" in path and path.endswith("/retry"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            try:
                sub = path[len("/api/session/"):-len("/retry")]
                session_id, utterance_id = [urllib.parse.unquote(p) for p in sub.split("/utterance/")]
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if self.queue_manager:
                    res = self.queue_manager.retry_utterance(session_id, utterance_id)
                    self.send_json(HTTPStatus.OK, res)
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議資料登録: POST /api/session/<id>/documents
        if path.startswith("/api/session/") and path.endswith("/documents"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/documents")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                title = payload.get("title", "")
                doc_content = payload.get("content", "")
                res = self.queue_manager.register_document(session_id, title, doc_content)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議資料用語・コンテキスト抽出: POST /api/session/<id>/documents/<doc_id>/extract
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/extract"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            sub = path[len("/api/session/"):-len("/extract")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                is_async = bool(payload.get("async", False)) or ("async=1" in self.path or "async=true" in self.path)
                res = self.queue_manager.extract_document_terms(session_id, doc_id, async_mode=is_async)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 抽出用語・背景要約の確定登録: POST /api/session/<id>/documents/<doc_id>/confirm-terms
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/confirm-terms"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            sub = path[len("/api/session/"):-len("/confirm-terms")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                terms = payload.get("terms", [])
                ctx_summary = payload.get("context_summary")
                ctx_summaries = payload.get("context_summaries")
                overwrite = bool(payload.get("overwrite", False))
                res = self.queue_manager.confirm_document_terms(
                    session_id, doc_id, terms,
                    context_summary=ctx_summary,
                    context_summaries=ctx_summaries,
                    overwrite=overwrite
                )
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 議事録由来等の用語直接確定登録（資料なし）: POST /api/session/<id>/terms/confirm
        if path.startswith("/api/session/") and path.endswith("/terms/confirm"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/terms/confirm")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                terms = payload.get("terms", [])
                overwrite = bool(payload.get("overwrite", False))
                res = self.queue_manager.confirm_terms(session_id, terms, overwrite=overwrite)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 外部LLM回答インポート: POST /api/session/<id>/documents/<doc_id>/import-external-response
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/import-external-response"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            sub = path[len("/api/session/"):-len("/import-external-response")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                text_input = payload.get("text", "")
                res = self.queue_manager.import_external_response(session_id, doc_id, text_input)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議資料削除（POST経由）: POST /api/session/<id>/documents/<doc_id>/delete
        if path.startswith("/api/session/") and "/documents/" in path and path.endswith("/delete"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            sub = path[len("/api/session/"):-len("/delete")]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                deleted = self.queue_manager.delete_document(session_id, doc_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "deleted": deleted, "doc_id": doc_id})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 確認済み議事録からの用語候補抽出: POST /api/session/<id>/terms/suggest-from-minutes
        if path.startswith("/api/session/") and path.endswith("/terms/suggest-from-minutes"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/terms/suggest-from-minutes")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                suggs = self.queue_manager.suggest_minutes_terms(session_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "session_id": session_id, "suggestions": suggs})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        if path.startswith("/api/session/") and path.endswith("/meeting-import/remove-context"):
            allowed, reason, _ = self.check_client_access(require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            if not self._verify_host_csrf():
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/meeting-import/remove-context")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length else {}
                self.send_json(HTTPStatus.OK, self.queue_manager.remove_meeting_context(session_id, payload.get("text")))
            except (ValueError, TypeError, OSError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 外部LLMの会議情報を確認し、一括登録する。
        if path.startswith("/api/session/") and (path.endswith("/meeting-import/preview") or path.endswith("/meeting-import/confirm")):
            allowed, reason, sess = self.check_client_access(require_edit=True, require_host=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            if not self._verify_host_csrf():
                return
            suffix = "/meeting-import/preview" if path.endswith("/preview") else "/meeting-import/confirm"
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len(suffix)])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length else {}
                text = payload.get("text", "")
                kind = payload.get("kind", "all")
                if suffix.endswith("preview"):
                    res = {"ok": True, **meeting_import.parse_scoped(text, kind)}
                else:
                    res = self.queue_manager.confirm_meeting_import(session_id, text, kind)
                self.send_json(HTTPStatus.OK, res)
            except (ValueError, TypeError, OSError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 参加国・コード一括登録: POST /api/session/<id>/countries
        if path.startswith("/api/session/") and path.endswith("/countries"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/countries")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                countries_data = payload.get("countries", payload.get("country_codes", payload.get("text", [])))
                res = self.queue_manager.set_session_countries(session_id, countries_data)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議設定更新: POST /api/session/<id>/settings
        if path.startswith("/api/session/") and path.endswith("/settings"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/settings")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                res = self.queue_manager.update_session_settings(session_id, payload)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 一括WAV音声送信（高精度録音モード等）: POST /api/session/<id>/utterance/audio
        if path.startswith("/api/session/") and path.endswith("/utterance/audio"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/utterance/audio")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if content_length > MAX_AUDIO_UPLOAD_SIZE:
                    self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"音声サイズが上限（{MAX_AUDIO_UPLOAD_SIZE // (1024*1024)}MB）を超えています。")
                    return
                content_type = self.headers.get("Content-Type", "")
                audio_bytes = None
                label = ""
                utterance_id = None
                file_ext = ".wav"

                if "application/json" in content_type:
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                    b64_audio = payload.get("audio_base64", "")
                    if b64_audio:
                        import base64
                        audio_bytes = base64.b64decode(b64_audio)
                    label = payload.get("label", "")
                    utterance_id = payload.get("utterance_id")
                    file_ext = payload.get("file_ext", ".wav")
                else:
                    audio_bytes = self.rfile.read(content_length)
                    if "audio/webm" in content_type:
                        file_ext = ".webm"
                    label = urllib.parse.unquote(self.headers.get("X-Speech-Label", ""))
                    utterance_id = self.headers.get("X-Utterance-Id")

                if not audio_bytes:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "音声データが含まれていません。")
                    return

                res = self.queue_manager.enqueue_utterance_audio(session_id, audio_bytes, utterance_id=utterance_id, label=label, file_ext=file_ext)
                self.send_json(HTTPStatus.OK, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 発言音声削除（POST経由）: POST /api/session/<id>/utterance/<utt_id>/audio/delete
        if path.startswith("/api/session/") and "/utterance/" in path and path.endswith("/audio/delete"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            sub = path[len("/api/session/"):-len("/audio/delete")]
            parts = sub.split("/utterance/")
            session_id, utt_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                deleted = self.queue_manager.delete_utterance_audio(session_id, utt_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "deleted": deleted, "utterance_id": utt_id})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議全音声削除（POST経由）: POST /api/session/<id>/audio/delete
        if path.startswith("/api/session/") and path.endswith("/audio/delete"):
            allowed, reason, sess = self.check_client_access(require_edit=True)
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/audio/delete")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                count = self.queue_manager.delete_all_session_audio(session_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "deleted_count": count})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

                # グローバルキー押下開始通知: POST /api/session/<id>/cue-down
        if path.startswith("/api/session/") and path.endswith("/cue-down"):
            allowed, reason = self.check_helper_or_csrf_access()
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/cue-down")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                press_time = float(payload.get("press_time_ms", time.time() * 1000.0))
                if self.queue_manager:
                    res = self.queue_manager.start_utterance(session_id, press_time)
                    state = self.queue_manager.get_or_create_session(session_id)
                    self.send_json(HTTPStatus.OK, {
                        "ok": True,
                        "utterance_id": res["utterance_id"],
                        "block_id": res["block_id"],
                        "active_cue_hold": state["meta"].get("active_cue_hold", {})
                    })
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # グローバルキー短押し/キャンセル通知: POST /api/session/<id>/cue-up
        if path.startswith("/api/session/") and path.endswith("/cue-up"):
            allowed, reason = self.check_helper_or_csrf_access()
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/cue-up")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                if self.queue_manager:
                    release_time = float(payload.get("release_time_ms", payload.get("cancel_time_ms", time.time() * 1000.0)))
                    self.queue_manager.end_utterance(session_id, release_time)
                    self.send_json(HTTPStatus.OK, {"ok": True})
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 発言区間キュー投入: POST /api/session/<id>/speech-range
        if path.startswith("/api/session/") and path.endswith("/speech-range"):
            allowed, reason = self.check_helper_or_csrf_access()
            if not allowed:
                self.send_error_json(HTTPStatus.FORBIDDEN, reason)
                return
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/speech-range")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                press_time = float(payload.get("press_time_ms", 0.0))
                release_time = float(payload.get("release_time_ms", press_time + 1000.0))
                # 0秒マージンを有効にするため in 判定
                pre_margin = float(payload["pre_margin_sec"]) if "pre_margin_sec" in payload else 2.0
                post_margin = float(payload["post_margin_sec"]) if "post_margin_sec" in payload else 2.0
                label = payload.get("label", "")

                if self.queue_manager:
                    state = self.queue_manager.get_or_create_session(session_id)
                    if state["meta"].get("active_utterance"):
                        self.queue_manager.end_utterance(session_id, release_time)
                        item = None
                    else:
                        item = self.queue_manager.add_speech_segment(session_id, press_time, release_time, pre_margin, post_margin, label)
                    self.send_json(HTTPStatus.OK, {"ok": True, "item": item})
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # Macグローバルキー支援状態取得: POST /api/mac-keys/status
        if path == "/api/mac-keys/status":
            if self.key_monitor:
                status = self.key_monitor.get_status()
            else:
                status = mac_key_helper.MacGlobalKeyMonitor().get_status()
            self.send_json(HTTPStatus.OK, status)
            return

        # Mac発言キー動作モード設定: POST /api/mac-keys/mode (ホスト限定・CSRF検証)
        if path in ("/api/mac-keys/mode", "/api/keys/mode"):
            if not self.require_host_csrf():
                self.send_error_json(HTTPStatus.FORBIDDEN, "CSRFトークンが未指定または無効です。")
                return
            try:
                body = json.loads(self.rfile.read(content_length).decode("utf-8") or "{}")
                mode = body.get("cue_mode", "").strip()
                if mode not in ("hold", "toggle"):
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "不正な発言モードです。'hold' または 'toggle' を指定してください。")
                    return
                if self.key_monitor:
                    self.key_monitor.set_cue_mode(mode)
                self.send_json(HTTPStatus.OK, {"ok": True, "cue_mode": mode})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 貼付キュー貼付確定: POST /api/session/<id>/paste-queue/confirm
        if path.startswith("/api/session/") and path.endswith("/paste-queue/confirm"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/paste-queue/confirm")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                item_id = payload.get("id")
                success = bool(payload.get("success", True))
                if self.queue_manager:
                    confirmed = self.queue_manager.confirm_paste_item(session_id, item_id, success)
                    self.send_json(HTTPStatus.OK, {"ok": True, "item": confirmed})
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

                # 貼付キュー先頭貼付: POST /api/session/<id>/paste-queue/paste-head
        if path.startswith("/api/session/") and path.endswith("/paste-queue/paste-head"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/paste-queue/paste-head")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                if self.queue_manager:
                    res = self.queue_manager.paste_head_item(session_id)
                    status_code = HTTPStatus.OK if res.get("ok") else HTTPStatus.CONFLICT
                    self.send_json(status_code, res)
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 貼付キューアイテム更新: POST /api/session/<id>/paste-queue/update
        if path.startswith("/api/session/") and path.endswith("/paste-queue/update"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/paste-queue/update")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                item_id = payload.get("id")
                text = payload.get("text")
                state_str = payload.get("state")
                label = payload.get("label")

                if self.queue_manager:
                    updated = self.queue_manager.update_paste_queue_item(session_id, item_id, text, state_str, label)
                    self.send_json(HTTPStatus.OK, {"ok": True, "item": updated})
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 貼付キュー並べ替え: POST /api/session/<id>/paste-queue/reorder
        if path.startswith("/api/session/") and path.endswith("/paste-queue/reorder"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/paste-queue/reorder")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                order_ids = payload.get("order_ids", [])
                if self.queue_manager:
                    reordered = self.queue_manager.reorder_paste_queue(session_id, order_ids)
                    self.send_json(HTTPStatus.OK, {"ok": True, "paste_queue": reordered})
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

                # 選択発言区間の音声処理登録: POST /api/session/<id>/speech-chunk
        if path.startswith("/api/session/") and path.endswith("/speech-chunk"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/speech-chunk")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                content_type = self.headers.get("Content-Type", "")

                audio_bytes = None
                duration_sec = 15.0
                label = ""

                if "application/json" in content_type:
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                    b64_audio = payload.get("audio_base64")
                    if b64_audio:
                        import base64
                        audio_bytes = base64.b64decode(b64_audio)
                    duration_sec = float(payload.get("duration_sec", 15.0))
                    label = payload.get("label", "")
                else:
                    audio_bytes = self.rfile.read(content_length)
                    dur_header = self.headers.get("X-Audio-Duration")
                    if dur_header:
                        try:
                            duration_sec = float(dur_header)
                        except ValueError:
                            pass
                    label_header = self.headers.get("X-Speech-Label")
                    if label_header:
                        label = urllib.parse.unquote(label_header)

                if not audio_bytes:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "音声データが含まれていません。")
                    return

                seg_id = self.headers.get("X-Speech-Segment-Id")
                file_ext = ".wav" if (audio_bytes.startswith(b"RIFF") or "audio/wav" in content_type or "audio/wave" in content_type) else ".webm"

                if self.queue_manager:
                    state = self.queue_manager.get_or_create_session(session_id)
                    chunk_index = state["meta"].get("total_chunks", 0)
                    utt_id = self.headers.get("X-Utterance-Id")
                    seq_hdr = self.headers.get("X-Chunk-Seq")
                    seq_idx = int(seq_hdr) if seq_hdr and seq_hdr.isdigit() else None
                    res = self.queue_manager.enqueue_chunk(
                        session_id, chunk_index, audio_bytes, duration_sec, is_final=False, file_ext=file_ext,
                        utterance_id=utt_id, seq_index=seq_idx
                    )
                    if seg_id:
                        with self.queue_manager.lock:
                            for s_item in state["meta"].get("speech_segments", []):
                                if s_item.get("id") == seg_id:
                                    s_item["chunk_index"] = chunk_index
                                    s_item["state"] = "processing"
                                    s_item["needs_audio"] = False
                            for p_item in state["meta"].get("paste_queue", []):
                                if p_item.get("id") == seg_id:
                                    p_item["chunk_index"] = chunk_index
                                    p_item["state"] = "preparing"
                                    p_item["needs_audio"] = False
                            if not self.queue_manager._save_meta_unlocked(state):
                                raise IOError("セグメントメタ情報の保存に失敗しました。ディスク容量を確認してください。")
                    if label or seg_id:
                        chunks_dir = state["session_dir"] / "chunks"
                        cfile = chunks_dir / f"chunk_{chunk_index:04d}.json"
                        if cfile.is_file():
                            cdata = json.loads(cfile.read_text(encoding="utf-8"))
                            if label:
                                cdata["label"] = label
                            if seg_id:
                                cdata["segment_id"] = seg_id
                            cfile.write_text(json.dumps(cdata, ensure_ascii=False, indent=2), encoding="utf-8")
                    self.send_json(HTTPStatus.OK, res)
                else:
                    self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "QueueManagerが稼働していません。")
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # LLMモデル切り替え: POST /api/models/llm/select
        if path == "/api/models/llm/select":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                model = payload.get("model", "").strip()
                if not model:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "モデル名が指定されていません。")
                    return
                installed = get_installed_ollama_models(self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL))
                if installed and (model not in installed and not any(model in m for m in installed)):
                    self.send_error_json(HTTPStatus.BAD_REQUEST, f"指定されたモデル '{model}' はOllamaにインストールされていません。")
                    return
                self.app_config["llm_model"] = model
                if self.app_config.get("config_file"):
                    settings_manager.save_config(self.app_config["config_file"], self.app_config)
                self.send_json(HTTPStatus.OK, {"ok": True, "selected_model": model, "notice": "次回の整文処理から適用されます。"})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # LLMモデル事前ロード: POST /api/models/llm/preload
        if path == "/api/models/llm/preload":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                model = payload.get("model") or self.app_config.get("llm_model", DEFAULT_LLM_MODEL)
                res = preload_ollama_model(model, self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL))
                status = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY
                self.send_json(status, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # LLMモデルメモリ解放: POST /api/models/llm/unload
        if path == "/api/models/llm/unload":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                model = payload.get("model") or self.app_config.get("llm_model", DEFAULT_LLM_MODEL)
                res = unload_ollama_model(model, self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL))
                status = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_GATEWAY
                self.send_json(status, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 外部コンポーネント自動ダウンロード・セットアップ: POST /api/setup/install-component (ホスト限定・CSRF検証)
        if path == "/api/setup/install-component":
            if not self._verify_host_csrf():
                return
            self.send_error_json(HTTPStatus.NOT_IMPLEMENTED, "自動導入は準備中です。配布ガイドの手順をご利用ください。")
            return

        # 保存先・環境設定変更API: POST /api/settings (ホスト限定・CSRF検証)
        if path == "/api/settings":
            if not self._verify_host_csrf():
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}

                # 競合防止チェック: 処理中の保存先変更やモデル設定変更を遮断
                if "models_dir" in payload and self.whisper_downloader.get_status().get("status") == "downloading":
                    self.send_error_json(HTTPStatus.CONFLICT, "Whisperモデルのダウンロード処理中のため、モデル保存先の変更はできません。")
                    return

                if ("ollama_url" in payload or "llm_model" in payload) and self.ollama_puller.get_status().get("status") == "pulling":
                    self.send_error_json(HTTPStatus.CONFLICT, "LLMモデルのPull処理中のため、Ollama接続先またはモデルの変更はできません。")
                    return

                if "data_dir" in payload and self.queue_manager:
                    # アクティブセッションのチャンク処理状態確認
                    active_sess = self.queue_manager.get_active_session_id()
                    if active_sess:
                        st = self.queue_manager.get_session_status(active_sess)
                        if st.get("is_processing"):
                            self.send_error_json(HTTPStatus.CONFLICT, "音声文字起こし処理中のため、議事録保存先の変更はできません。")
                            return

                ok, updated_conf, err_msg = settings_manager.validate_and_apply_settings(self.app_config, payload, PROJECT_DIR)
                if not ok:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, err_msg or "設定値が無効です。")
                    return

                self.app_config.update(updated_conf)
                conf_file = self.app_config.get("config_file", PROJECT_DIR / "config.json")
                saved_ok = settings_manager.save_config(conf_file, self.app_config)
                self.send_json(HTTPStatus.OK, {
                    "ok": True,
                    "saved_to_disk": saved_ok,
                    "settings": {
                        "data_dir": str(self.app_config.get("data_dir", DEFAULT_DATA_DIR)),
                        "models_dir": str(self.app_config.get("models_dir", DEFAULT_MODELS_DIR)),
                        "whisper_model": str(self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL)),
                        "whisper_cmd": self.app_config.get("whisper_cmd", "whisper-cli"),
                        "ffmpeg_cmd": self.app_config.get("ffmpeg_cmd", "ffmpeg"),
                        "ollama_url": self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL),
                        "llm_model": self.app_config.get("llm_model", DEFAULT_LLM_MODEL)
                    },
                    "notice": "設定を更新・永続化しました。既存データは安全に保護されています。"
                })
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # Whisperモデルダウンロード開始: POST /api/models/whisper/download (ホスト限定・CSRF検証)
        if path == "/api/models/whisper/download":
            if not self._verify_host_csrf():
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                filename = payload.get("filename", "").strip()
                force = bool(payload.get("force", False))
                if not filename:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "ダウンロードするWhisperモデルのファイル名が指定されていません。")
                    return
                whisper_model_path = Path(self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL))
                models_dir = Path(self.app_config.get("models_dir", whisper_model_path.parent if whisper_model_path.parent.is_dir() else DEFAULT_MODELS_DIR))
                res = self.whisper_downloader.start_download(filename, models_dir, force_overwrite=force)
                status = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST
                self.send_json(status, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # Whisperモデルダウンロードキャンセル: POST /api/models/whisper/download/cancel (ホスト限定・CSRF検証)
        if path == "/api/models/whisper/download/cancel":
            if not self._verify_host_csrf():
                return
            res = self.whisper_downloader.cancel()
            status = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(status, res)
            return

        # OllamaモデルPull開始: POST /api/models/llm/pull (ホスト限定・CSRF検証)
        if path == "/api/models/llm/pull":
            if not self._verify_host_csrf():
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                model_name = payload.get("model", "").strip()
                if not model_name:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "PullするLLMモデル名が指定されていません。")
                    return
                ollama_url = self.app_config.get("ollama_url", DEFAULT_OLLAMA_URL)
                res = self.ollama_puller.start_pull(model_name, ollama_url)
                status = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST
                self.send_json(status, res)
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # OllamaモデルPullキャンセル: POST /api/models/llm/pull/cancel (ホスト限定・CSRF検証)
        if path == "/api/models/llm/pull/cancel":
            if not self._verify_host_csrf():
                return
            res = self.ollama_puller.cancel()
            status = HTTPStatus.OK if res.get("ok") else HTTPStatus.BAD_REQUEST
            self.send_json(status, res)
            return

        # Whisperモデル切り替え: POST /api/models/whisper/select
        if path == "/api/models/whisper/select":
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8")) if content_length > 0 else {}
                filename = payload.get("filename", "").strip()
                if not filename:
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "Whisperモデルファイル名が指定されていません。")
                    return
                whisper_model_path = Path(self.app_config.get("whisper_model", DEFAULT_WHISPER_MODEL))
                models_dir = Path(self.app_config.get("models_dir", whisper_model_path.parent))
                filename = model_manager.sanitize_filename(filename)
                candidate = models_dir / filename
                if not candidate.is_file():
                    self.send_error_json(HTTPStatus.NOT_FOUND, f"ローカルモデルファイル '{filename}' が存在しません。外部自動ダウンロードは行いません。")
                    return
                self.app_config["whisper_model"] = candidate
                if self.app_config.get("config_file"):
                    settings_manager.save_config(self.app_config["config_file"], self.app_config)
                self.send_json(HTTPStatus.OK, {"ok": True, "selected_whisper_model": candidate.name, "notice": "次回の音声認識処理から適用されます。"})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "エンドポイントが見つかりません。")


    def do_DELETE(self):
        if not self.validate_host_and_origin():
            self.send_error_json(HTTPStatus.BAD_REQUEST, "不正なHostまたはOriginヘッダーです。")
            return
        if not self._verify_host_csrf():
            return
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        # 資料削除: DELETE /api/session/<id>/documents/<doc_id>
        if path.startswith("/api/session/") and "/documents/" in path:
            sub = path[len("/api/session/"):]
            parts = sub.split("/documents/")
            session_id, doc_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                res = self.queue_manager.delete_document(session_id, doc_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "deleted": res, "doc_id": doc_id})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 発言音声削除: DELETE /api/session/<id>/utterance/<utt_id>/audio
        if path.startswith("/api/session/") and "/utterance/" in path and path.endswith("/audio"):
            sub = path[len("/api/session/"):-len("/audio")]
            parts = sub.split("/utterance/")
            session_id, utt_id = urllib.parse.unquote(parts[0]), parts[1]
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                res = self.queue_manager.delete_utterance_audio(session_id, utt_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "deleted": res, "utterance_id": utt_id})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        # 会議全音声削除: DELETE /api/session/<id>/audio
        if path.startswith("/api/session/") and path.endswith("/audio"):
            session_id = urllib.parse.unquote(path[len("/api/session/"):-len("/audio")])
            try:
                sanitize_session_id(session_id, self.app_config.get("data_dir", DEFAULT_DATA_DIR))
                count = self.queue_manager.delete_all_session_audio(session_id)
                self.send_json(HTTPStatus.OK, {"ok": True, "deleted_count": count})
            except Exception as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return

        self.send_error_json(HTTPStatus.NOT_FOUND, "エンドポイントが見つかりません。")

def run_server(
    port: int = 8000,
    host: str = "127.0.0.1",
    whisper_model: Optional[Path] = None,
    whisper_cmd: str = "whisper-cli",
    ffmpeg_cmd: str = "ffmpeg",
    ollama_url: str = DEFAULT_OLLAMA_URL,
    llm_model: str = DEFAULT_LLM_MODEL,
    data_dir: Optional[Path] = None,
    models_dir: Optional[Path] = None,
    web_dir: Path = DEFAULT_WEB_DIR,
    open_browser: bool = True
):
    """ローカルWebサーバーを起動します（デフォルト共有OFF・接続コード認証式）。"""
    # 保存済み設定のロード
    loaded_config = settings_manager.load_config(PROJECT_DIR / "config.json", PROJECT_DIR)

    actual_data_dir = Path(data_dir if data_dir is not None else loaded_config.get("data_dir", DEFAULT_DATA_DIR)).resolve()
    actual_data_dir.mkdir(parents=True, exist_ok=True)

    actual_models_dir = Path(models_dir if models_dir is not None else loaded_config.get("models_dir", DEFAULT_MODELS_DIR)).resolve()
    actual_models_dir.mkdir(parents=True, exist_ok=True)

    actual_whisper_model = Path(whisper_model if whisper_model is not None else loaded_config.get("whisper_model", actual_models_dir / "ggml-large-v3-turbo.bin")).resolve()
    actual_whisper_cmd = whisper_cmd if whisper_cmd != "whisper-cli" else loaded_config.get("whisper_cmd", "whisper-cli")
    actual_ffmpeg_cmd = ffmpeg_cmd if ffmpeg_cmd != "ffmpeg" else loaded_config.get("ffmpeg_cmd", "ffmpeg")
    actual_ollama_url = ollama_url if ollama_url != DEFAULT_OLLAMA_URL else loaded_config.get("ollama_url", DEFAULT_OLLAMA_URL)
    actual_llm_model = llm_model if llm_model != DEFAULT_LLM_MODEL else loaded_config.get("llm_model", DEFAULT_LLM_MODEL)

    mun_minutes.validate_ollama_url(actual_ollama_url)

    config = {
        "config_file": PROJECT_DIR / "config.json",
        "whisper_model": actual_whisper_model,
        "models_dir": actual_models_dir,
        "whisper_cmd": actual_whisper_cmd,
        "ffmpeg_cmd": actual_ffmpeg_cmd,
        "ollama_url": actual_ollama_url,
        "llm_model": actual_llm_model,
        "data_dir": actual_data_dir,
        "web_dir": Path(web_dir)
    }

    queue_manager = SessionQueueManager(config)
    sharing_manager = SharingManager(host_port=port)
    url = f"http://127.0.0.1:{port}"

    whisper_downloader = model_manager.WhisperDownloadManager()
    ollama_puller = model_manager.OllamaPullManager()
    comp_installer = component_installer.ComponentInstaller(PROJECT_DIR)

    if sys.platform == "darwin":
        key_monitor = mac_key_helper.MacGlobalKeyMonitor(
            server_url=url,
            session_id_getter=queue_manager.get_active_session_id,
            auth_token=sharing_manager.host_csrf_token
        )
    elif sys.platform == "win32":
        key_monitor = win_key_helper.WinGlobalKeyMonitor(
            server_url=url,
            session_id_getter=queue_manager.get_active_session_id,
            auth_token=sharing_manager.host_csrf_token
        )
    else:
        key_monitor = None

    if key_monitor:
        key_monitor.start()

    handler_factory = lambda *args, **kwargs: MunRequestHandler(
        *args,
        app_config=config,
        queue_manager=queue_manager,
        key_monitor=key_monitor,
        sharing_manager=sharing_manager,
        whisper_downloader=whisper_downloader,
        ollama_puller=ollama_puller,
        component_installer_instance=comp_installer,
        **kwargs
    )
    server_address = (host, port)

    try:
        httpd = HTTPServer(server_address, handler_factory)
    except OSError as e:
        print(f"ポート {port} のバインドに失敗しました: {e}", file=sys.stderr)
        if key_monitor:
            key_monitor.stop()
        queue_manager.stop()
        return 1
    print(f"=== 模擬国連 議事録支援システム (区間ストリーミング試用版) ===")
    print(f"サーバー稼働中: {url}")
    print(f"バインド先: 127.0.0.1 (完全ローカル限定 / 外部送信なし)")
    print(f"Whisperモデル: {config['whisper_model']}")
    print(f"Ollama接続先: {config['ollama_url']} (モデル: {config['llm_model']})")
    print(f"データ保存先: {config['data_dir']}")
    print(f"区間分割・順序保証キュー: 稼働中")
    print(f"終了するには Ctrl+C を押してください。\n")

    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しています…")
    finally:
        sharing_manager.terminate_all()
        if key_monitor:
            key_monitor.stop()
        if whisper_downloader:
            whisper_downloader.cancel()
        if ollama_puller:
            ollama_puller.cancel()
        queue_manager.stop()
        httpd.server_close()
        print("サーバーを安全に停止しました。")
    return 0


def main():
    parser = argparse.ArgumentParser(description="模擬国連 議事録支援システムのローカルWebサーバーを起動します。")
    parser.add_argument("--host", default="127.0.0.1", help="サーバーバインドアドレス (デフォルト: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="サーバーポート番号 (デフォルト: 8000)")
    parser.add_argument("--whisper-model", type=Path, default=DEFAULT_WHISPER_MODEL, help="Whisperモデルファイル")
    parser.add_argument("--whisper", default="whisper-cli", help="whisper.cppのコマンド名またはパス")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpegのコマンド名またはパス")
    parser.add_argument("--ollama-url", default=DEFAULT_OLLAMA_URL, help="OllamaのURL (http://127.0.0.1:11434)")
    parser.add_argument("--llm-model", default=DEFAULT_LLM_MODEL, help="Ollamaのモデル名")
    parser.add_argument("--data-dir", type=Path, default=None, help="議事録データの保存フォルダ")
    parser.add_argument("--models-dir", type=Path, default=None, help="Whisperモデルの保存フォルダ")
    parser.add_argument("--no-browser", action="store_true", help="起動時にブラウザを自動で開かない")
    args = parser.parse_args()

    return run_server(
        port=args.port,
        host=args.host,
        whisper_model=args.whisper_model if "--whisper-model" in sys.argv else None,
        whisper_cmd=args.whisper,
        ffmpeg_cmd=args.ffmpeg,
        ollama_url=args.ollama_url,
        llm_model=args.llm_model,
        data_dir=args.data_dir,
        models_dir=args.models_dir,
        open_browser=not args.no_browser
    )


if __name__ == "__main__":
    sys.exit(main())
