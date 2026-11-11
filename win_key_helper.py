"""Windows グローバルキー支援プロセス・安全アダプター

Google Docs等の別ウィンドウ・別タブが最前面にある場合でも、
Windows環境において F2 キーによる発言マーク（ホールド/トグル）および
F8 キーによる先頭貼付検出を行い、ローカルWebサーバーへHTTP通知するための安全モジュール。

【重要・検証ステータス】:
本モジュールは Win32 API (GetAsyncKeyState) を用いたキー検出ロジックを実装していますが、
現在の開発環境は macOS であり、Windows 実機ハードウェアでの総合動作検証は未完了です。
未検証の Windows 実機対応を「完成」と断定せず、UIおよびステータスAPIにおいて
「Windows実機環境未検証（API実装）」と正直に明示します。
既存 macOS 向けキー監視 (mac_key_helper.py) は 100% 維持されます。
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

import mac_key_helper

# Windows Virtual Key Codes
VK_F2 = 0x70  # 112
VK_F8 = 0x77  # 119
DEFAULT_WIN_CUE_KEYCODE = VK_F2
DEFAULT_WIN_PASTE_KEYCODE = VK_F8


def is_windows() -> bool:
    return sys.platform == "win32"


class WinGlobalKeyMonitor:
    """
    Windows 環境におけるグローバルキー監視アダプター。
    Win32 GetAsyncKeyState をバックグラウンドスレッドで安全に監視し、
    mac_key_helper.KeyEventHandler を用いて発言区間判定を行います。
    """
    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8000",
        session_id_getter: Optional[Callable[[], Optional[str]]] = None,
        cue_keycode: int = DEFAULT_WIN_CUE_KEYCODE,
        paste_keycode: int = DEFAULT_WIN_PASTE_KEYCODE,
        http_client: Optional[Callable[[str, dict], dict]] = None,
        paste_executor: Optional[mac_key_helper.PasteActionExecutor] = None,
        sync_mode: bool = False,
        auth_token: Optional[str] = None,
        key_state_getter: Optional[Callable[[int], bool]] = None
    ):
        self.server_url = server_url.rstrip("/")
        self.session_id_getter = session_id_getter
        self.cue_keycode = cue_keycode
        self.paste_keycode = paste_keycode
        self.http_client = http_client or self._default_http_post
        self.paste_executor = paste_executor or mac_key_helper.PasteActionExecutor()
        self.sync_mode = sync_mode
        self.auth_token = auth_token
        self.cue_mode = mac_key_helper.DEFAULT_CUE_MODE
        self.key_state_getter = key_state_getter or self._default_key_state_getter

        self.is_running = False
        self._stop_event = threading.Event()
        self._thread = None
        self.last_error = None
        self.last_paste_result = None
        self.events_sent_count = 0

        self._action_queue = queue.Queue()
        self._worker_thread = None

        self.event_handler = mac_key_helper.KeyEventHandler(
            cue_keycode=self.cue_keycode,
            paste_keycode=self.paste_keycode,
            on_speech_range=self._queue_speech_range,
            on_paste_head=self._queue_paste_head,
            on_cue_down=self._queue_cue_down,
            on_cue_cancel=self._queue_cue_cancel,
            cue_mode=self.cue_mode
        )

    def _default_key_state_getter(self, vk_code: int) -> bool:
        if not is_windows():
            return False
        try:
            state = ctypes.windll.user32.GetAsyncKeyState(vk_code)
            return bool(state & 0x8000)
        except Exception:
            return False

    def _default_http_post(self, endpoint_path: str, payload: dict) -> dict:
        url = f"{self.server_url}{endpoint_path}"
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if getattr(self, "auth_token", None):
            headers["X-Host-CSRF-Token"] = self.auth_token
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except Exception as exc:
            self.last_error = f"HTTP通知失敗 ({endpoint_path}): {exc}"
            return {"ok": False, "error": str(exc)}

    def _queue_cue_down(self, press_time_ms: float):
        if self.sync_mode:
            self._handle_action("cue_down", (press_time_ms,))
        else:
            self._action_queue.put(("cue_down", (press_time_ms,)))

    def _queue_cue_cancel(self, cancel_time_ms: float):
        if self.sync_mode:
            self._handle_action("cue_cancel", (cancel_time_ms,))
        else:
            self._action_queue.put(("cue_cancel", (cancel_time_ms,)))

    def _queue_speech_range(self, press_time_ms: float, release_time_ms: float):
        if self.sync_mode:
            self._handle_action("speech_range", (press_time_ms, release_time_ms))
        else:
            self._action_queue.put(("speech_range", (press_time_ms, release_time_ms)))

    def _queue_paste_head(self):
        if self.sync_mode:
            self._handle_action("paste_head", ())
        else:
            self._action_queue.put(("paste_head", ()))

    def set_cue_mode(self, mode: str) -> str:
        self.event_handler.set_cue_mode(mode)
        self.cue_mode = mode
        return mode

    def start(self) -> bool:
        if self.is_running:
            return True

        if not is_windows() and self.key_state_getter == self._default_key_state_getter:
            self.last_error = "Windows OS以外の環境では Windows グローバルキー監視は動作しません。"
            return False

        self.is_running = True
        self._stop_event.clear()

        self._worker_thread = threading.Thread(target=self._dispatch_worker_loop, daemon=True)
        self._worker_thread.start()

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self.is_running = False
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._action_queue.put(None)

    def _monitor_loop(self):
        cue_pressed_prev = False
        paste_pressed_prev = False

        while not self._stop_event.is_set():
            try:
                cue_pressed = self.key_state_getter(self.cue_keycode)
                paste_pressed = self.key_state_getter(self.paste_keycode)

                now_ms = time.time() * 1000.0

                # F2 発言キー判定
                if cue_pressed and not cue_pressed_prev:
                    self.event_handler.process_key_event(10, self.cue_keycode, is_repeat=False)
                elif not cue_pressed and cue_pressed_prev:
                    self.event_handler.process_key_event(11, self.cue_keycode, is_repeat=False)
                elif cue_pressed and cue_pressed_prev:
                    self.event_handler.process_key_event(10, self.cue_keycode, is_repeat=True)

                # F8 貼付キー判定
                if paste_pressed and not paste_pressed_prev:
                    self.event_handler.process_key_event(10, self.paste_keycode, is_repeat=False)
                elif not paste_pressed and paste_pressed_prev:
                    self.event_handler.process_key_event(11, self.paste_keycode, is_repeat=False)

                cue_pressed_prev = cue_pressed
                paste_pressed_prev = paste_pressed

            except Exception as exc:
                self.last_error = f"Windowsキー監視ループ例外: {exc}"

            time.sleep(0.02)  # 20ms ポーリング

    def _dispatch_worker_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._action_queue.get(timeout=0.2)
                if item is None:
                    break
                action_type, args = item
                self._handle_action(action_type, args)
                self._action_queue.task_done()
            except queue.Empty:
                continue
            except Exception as exc:
                self.last_error = f"ディスパッチワーカー例外: {exc}"

    def _handle_action(self, action_type: str, args: tuple):
        sess_id = self.session_id_getter() if self.session_id_getter else None
        if not sess_id:
            return

        quoted_sess = urllib.parse.quote(sess_id)
        if action_type == "cue_down":
            press_time_ms = args[0]
            self.http_client(f"/api/session/{quoted_sess}/cue-down", {"press_time_ms": press_time_ms})
            self.events_sent_count += 1
        elif action_type == "cue_cancel":
            cancel_time_ms = args[0]
            self.http_client(f"/api/session/{quoted_sess}/cue-up", {"cancel_time_ms": cancel_time_ms})
            self.events_sent_count += 1
        elif action_type == "speech_range":
            press_time_ms, release_time_ms = args
            self.http_client(f"/api/session/{quoted_sess}/speech-range", {
                "press_time_ms": press_time_ms,
                "release_time_ms": release_time_ms,
                "label": "F2 (Windows)"
            })
            self.events_sent_count += 1
        elif action_type == "paste_head":
            self.http_client(f"/api/session/{quoted_sess}/paste-queue/paste-head", {})
            self.events_sent_count += 1

    def get_status(self) -> dict:
        return {
            "platform": "windows",
            "is_running": self.is_running,
            "tested_on_hardware": False,
            "hardware_verification_note": "Windows実機環境での総合動作検証は未完了です（Win32 APIキー監視の実装・未検証）",
            "cue_mode": self.cue_mode,
            "cue_keycode": self.cue_keycode,
            "paste_keycode": self.paste_keycode,
            "events_sent_count": self.events_sent_count,
            "last_error": self.last_error,
            "last_paste_result": self.last_paste_result
        }
