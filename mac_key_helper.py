"""macOS グローバルキー支援プロセス・安全アダプター

Google Docs等の別ウィンドウ・別タブが最前面にある場合でも、
ユーザーが設定したキー（例: Space長押しによる発言マーク）を検出して
ローカルWebサーバー (http://127.0.0.1:8000) へHTTP通知するための安全モジュール。

安全・セキュリティの原則:
1. 自動キーストローク注入（Cmd+V送信）は、ブラウザ内のメール・パスワード・別タブ等への
   誤入力およびクリップボード全形式（画像・リッチ形式）破壊を防ぐため、【既定で必ず無効】とします。
2. 対象DocsのURLおよび入力先フォーカスの安全性が100%確認できない場合は、キーストローク注入を絶対に実行しません。
3. 最少安全標準として「Web画面での明示的コピー → Google Docs上での手動ペースト（Cmd+V） → 確定ボタン」
   の確実なワークフローを推奨・維持します。
4. ユーザーの実クリップボードやOS権限をテスト等で無断操作・変更しません。
5. CGEventTapのコールバック内で同期I/OやブロッキングHTTP通信を行わず、非同期キューで安全に処理します。
"""
import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

# macOS Virtual Key Codes
DEFAULT_CUE_KEYCODE = 120  # F2 (Google Docsでの通常入力衝突を防ぐためSpaceは禁止)
DEFAULT_CUE_MODE = "toggle"  # "hold" (長押し) または "toggle" (トグル)
VALID_CUE_MODES = {"hold", "toggle"}
DEFAULT_PASTE_KEYCODE = 100  # F8
FORBIDDEN_CUE_KEYCODES = {49}  # 49 = Space (Docs入力衝突防止)

# macOS CoreGraphics Event Field Constants (from Xcode SDK CGEventTypes.h)
FIELD_KEYBOARD_AUTOREPEAT = 8
FIELD_KEYBOARD_KEYCODE = 9


def is_accessibility_trusted(prompt: bool = False) -> bool:
    """macOS のアクセシビリティ権限が付与されているか確認します。"""
    try:
        aserv = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        aserv.AXIsProcessTrusted.argtypes = []
        aserv.AXIsProcessTrusted.restype = ctypes.c_bool
        return aserv.AXIsProcessTrusted()
    except Exception as exc:
        print(f"アクセシビリティ権限確認エラー: {exc}", file=sys.stderr)
        return False


def default_verify_target_docs() -> dict:
    """
    対象ウィンドウ・タブ・フォーカスが意図した Google Docs であるかを厳格に検証します。
    ※OS外部からのブラウザ内タブURL取得やアクティブ要素フォーカス判定は、
      追加のオートメーション権限が必要であり、タイムアウトや取得不能時は安全に「未確認」とします。
    """
    # 既定では安全第一とし、未検証状態として拒否
    return {
        "is_verified_docs": False,
        "url": "",
        "target_element": "unknown",
        "reason": "docs_tab_and_focus_cannot_be_safely_verified"
    }


def default_clipboard_inspector() -> dict:
    """
    クリップボードの状態（リッチ形式の有無、変更カウンタ等）を検査します。
    """
    try:
        # pbpasteで文字列が取れるか確認
        res = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=0.5)
        text = res.stdout if res.returncode == 0 else ""
        return {
            "has_plain_text": bool(text),
            "is_rich_only": False,
            "change_count": time.time()  # 擬似タイムスタンプカウンタ
        }
    except Exception:
        return {
            "has_plain_text": False,
            "is_rich_only": True,
            "change_count": 0
        }


class PasteActionExecutor:
    """
    貼付アクションの安全実行器。
    
    安全ガード:
    1. 自動注入の既定無効: enable_auto_key_injection は既定 False。
    2. Docs URL・タブ・フォーカス検証: 検証関数が is_verified_docs: True を返さない限り注入を拒否。
    3. クリップボード全形式保全: リッチ形式（画像等）がある場合や、ユーザーが直後に新規コピーした場合は
       クリップボード破壊を防ぐため自動書き換え・旧内容の上書き復元を中止。
    4. 依存注入（DI）: モックを用いて安全かつ完全にテスト可能。
    """
    def __init__(
        self,
        enable_auto_key_injection: bool = False,
        target_docs_verifier: Optional[Callable[[], dict]] = None,
        clipboard_inspector: Optional[Callable[[], dict]] = None,
        clipboard_reader: Optional[Callable[[], str]] = None,
        clipboard_writer: Optional[Callable[[str], bool]] = None,
        key_injector: Optional[Callable[[], bool]] = None
    ):
        # 安全のため既定で自動注入は必ず無効
        self.enable_auto_key_injection = enable_auto_key_injection
        self.target_docs_verifier = target_docs_verifier or default_verify_target_docs
        self.clipboard_inspector = clipboard_inspector or default_clipboard_inspector
        self.clipboard_reader = clipboard_reader or (lambda: "")
        self.clipboard_writer = clipboard_writer or (lambda text: True)
        self.key_injector = key_injector or (lambda: False)
        self.last_execution = None

    def execute_paste(self, item: dict) -> dict:
        text = item.get("text", "") if item else ""
        if not text:
            res = {"ok": False, "reason": "no_text", "action": "ignored"}
            self.last_execution = res
            return res

        # 1. 自動注入フラグの確認（既定では安全のため必ず無効）
        if not self.enable_auto_key_injection:
            res = {
                "ok": False,
                "reason": "auto_injection_disabled",
                "action": "manual_paste_recommended",
                "notice": "安全ガード: メールや別タブ等への誤入力を防止するため、自動キーストローク注入は既定で無効化されています。Web画面の「Docs先頭貼付」からコピーし手動で貼り付けてください。"
            }
            self.last_execution = res
            return res

        # 2. 対象Docsタブ・URLおよび入力先フォーカスの検証
        docs_check = self.target_docs_verifier()
        if not docs_check.get("is_verified_docs"):
            res = {
                "ok": False,
                "reason": "docs_verification_failed",
                "action": "rejected_for_safety",
                "details": docs_check,
                "notice": f"安全ガード: 意図したGoogle Docsタブまたは安全な入力先セルが確認できないため自動注入を中止しました ({docs_check.get('reason')})。"
            }
            self.last_execution = res
            return res

        # 3. クリップボードのリッチ形式（画像・バイナリ等）検査
        clip_info = self.clipboard_inspector()
        if clip_info.get("is_rich_only"):
            res = {
                "ok": False,
                "reason": "rich_clipboard_cannot_preserve",
                "action": "rejected_for_safety",
                "notice": "安全ガード: クリップボードに画像等のリッチ形式データが存在するため、データ破壊を防ぐべく自動貼付を中止しました。"
            }
            self.last_execution = res
            return res

        initial_change_count = clip_info.get("change_count", 0)

        # 4. クリップボードへの発言テキスト書き込み
        write_ok = self.clipboard_writer(text)
        if not write_ok:
            res = {"ok": False, "reason": "clipboard_write_failed"}
            self.last_execution = res
            return res

        # 5. キーストローク注入 (Cmd+V)
        inject_ok = self.key_injector()
        if not inject_ok:
            res = {"ok": False, "reason": "key_injection_failed"}
            self.last_execution = res
            return res

        res = {
            "ok": True,
            "status": "injected",
            "item_id": item.get("id"),
            "target_url": docs_check.get("url", ""),
            "notice": "Docsへの注入を行いました。Docs上で内容を確認の上、Web画面で完了を確定してください。"
        }
        self.last_execution = res
        return res


class KeyEventHandler:
    """
    キーイベント（押下・離上・リピート）の判定、デバウンス、押しっぱなし処理を行い、
    発言範囲指定および先頭貼付のアクションを発火するハンドラー。
    """
    def __init__(
        self,
        cue_keycode: int = DEFAULT_CUE_KEYCODE,
        paste_keycode: int = DEFAULT_PASTE_KEYCODE,
        on_speech_range: Optional[Callable[[float, float], None]] = None,
        on_paste_head: Optional[Callable[[], None]] = None,
        on_cue_down: Optional[Callable[[float], None]] = None,
        on_cue_cancel: Optional[Callable[[float], None]] = None,
        min_hold_ms: float = 400.0,
        cue_mode: str = DEFAULT_CUE_MODE
    ):
        if cue_keycode in FORBIDDEN_CUE_KEYCODES:
            raise ValueError(f"キーコード {cue_keycode} (Space等) はGoogle Docsでの通常入力と衝突するため発言キーとして禁止されています。F2(120)等をご使用ください。")
        if cue_mode not in VALID_CUE_MODES:
            raise ValueError(f"不正な発言モード: {cue_mode}。'hold' または 'toggle' を指定してください。")
        self.cue_keycode = cue_keycode
        self.paste_keycode = paste_keycode
        self.on_speech_range = on_speech_range
        self.on_paste_head = on_paste_head
        self.on_cue_down = on_cue_down
        self.on_cue_cancel = on_cue_cancel
        self.min_hold_ms = min_hold_ms
        self.cue_mode = cue_mode

        self.is_holding = False
        self.press_time_ms = 0.0
        self.lock = threading.Lock()

    def set_cue_mode(self, mode: str):
        """発言キーの動作モードを切替（hold: 長押し, toggle: トグル）。"""
        if mode not in VALID_CUE_MODES:
            raise ValueError(f"不正な発言モード: {mode}。'hold' または 'toggle' を指定してください。")
        with self.lock:
            if self.cue_mode != mode:
                # 切替時にアクティブ中なら安全にキャンセル/解除
                if self.is_holding:
                    self.is_holding = False
                    if self.on_cue_cancel:
                        try:
                            self.on_cue_cancel(time.time() * 1000.0)
                        except Exception:
                            pass
                self.cue_mode = mode

    def process_key_event(self, event_type: int, keycode: int, is_repeat: bool = False):
        now_ms = time.time() * 1000.0

        with self.lock:
            # 1. KeyDown (10)
            if event_type == 10:
                if keycode == self.cue_keycode:
                    if is_repeat:
                        return
                    if self.cue_mode == "hold":
                        if self.is_holding:
                            return
                        self.is_holding = True
                        self.press_time_ms = now_ms
                        if self.on_cue_down:
                            try:
                                self.on_cue_down(self.press_time_ms)
                            except Exception as exc:
                                print(f"発言開始コールバック例外: {exc}", file=sys.stderr)
                    elif self.cue_mode == "toggle":
                        if not self.is_holding:
                            # トグル開始 (ON)
                            self.is_holding = True
                            self.press_time_ms = now_ms
                            if self.on_cue_down:
                                try:
                                    self.on_cue_down(self.press_time_ms)
                                except Exception as exc:
                                    print(f"発言開始コールバック例外: {exc}", file=sys.stderr)
                        else:
                            # トグル終了 (OFF)
                            self.is_holding = False
                            release_time_ms = now_ms
                            dur_ms = release_time_ms - self.press_time_ms
                            if dur_ms >= self.min_hold_ms:
                                if self.on_speech_range:
                                    try:
                                        self.on_speech_range(self.press_time_ms, release_time_ms)
                                    except Exception as exc:
                                        print(f"発言範囲コールバック例外: {exc}", file=sys.stderr)
                            else:
                                if self.on_cue_cancel:
                                    try:
                                        self.on_cue_cancel(release_time_ms)
                                    except Exception as exc:
                                        print(f"発言キャンセルコールバック例外: {exc}", file=sys.stderr)

                elif keycode == self.paste_keycode:
                    if is_repeat:
                        return
                    if self.on_paste_head:
                        try:
                            self.on_paste_head()
                        except Exception as exc:
                            print(f"貼付キーコールバック例外: {exc}", file=sys.stderr)

            # 2. KeyUp (11)
            elif event_type == 11:
                if keycode == self.cue_keycode:
                    if self.cue_mode == "hold":
                        if not self.is_holding:
                            return
                        self.is_holding = False
                        release_time_ms = now_ms
                        dur_ms = release_time_ms - self.press_time_ms

                        if dur_ms >= self.min_hold_ms:
                            if self.on_speech_range:
                                try:
                                    self.on_speech_range(self.press_time_ms, release_time_ms)
                                except Exception as exc:
                                    print(f"発言範囲コールバック例外: {exc}", file=sys.stderr)
                        else:
                            if self.on_cue_cancel:
                                try:
                                    self.on_cue_cancel(release_time_ms)
                                except Exception as exc:
                                    print(f"発言キャンセルコールバック例外: {exc}", file=sys.stderr)
                    elif self.cue_mode == "toggle":
                        # トグルモードではKeyUpによる発言解除は行わない
                        pass


class MacGlobalKeyMonitor:
    """
    macOS CGEventTap を利用したグローバルキー監視および安全貼付支援アダプター。
    """
    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8000",
        session_id_getter: Optional[Callable[[], Optional[str]]] = None,
        cue_keycode: int = DEFAULT_CUE_KEYCODE,
        paste_keycode: int = DEFAULT_PASTE_KEYCODE,
        http_client: Optional[Callable[[str, dict], dict]] = None,
        paste_executor: Optional[PasteActionExecutor] = None,
        sync_mode: bool = False,
        auth_token: Optional[str] = None
    ):
        self.server_url = server_url.rstrip("/")
        self.session_id_getter = session_id_getter
        self.cue_keycode = cue_keycode
        self.paste_keycode = paste_keycode
        self.http_client = http_client or self._default_http_post
        self.paste_executor = paste_executor or PasteActionExecutor()
        self.sync_mode = sync_mode
        self.auth_token = auth_token
        self.cue_mode = DEFAULT_CUE_MODE

        self.is_running = False
        self._tap_port = None
        self._stop_event = threading.Event()
        self._thread = None
        self.last_error = None
        self.last_paste_result = None
        self.events_sent_count = 0

        self._action_queue = queue.Queue()
        self._worker_thread = None

        self.event_handler = KeyEventHandler(
            cue_keycode=self.cue_keycode,
            paste_keycode=self.paste_keycode,
            on_speech_range=self._queue_speech_range,
            on_paste_head=self._queue_paste_head,
            on_cue_down=self._queue_cue_down,
            on_cue_cancel=self._queue_cue_cancel,
            cue_mode=self.cue_mode
        )

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

    def _handle_action(self, action_type: str, args: tuple):
        session_id = self.session_id_getter() if self.session_id_getter else None
        if not session_id:
            return

        if action_type == "cue_down":
            press_time_ms = args[0]
            payload = {"press_time_ms": press_time_ms, "is_holding": True}
            self.http_client(f"/api/session/{urllib.parse.quote(session_id)}/cue-down", payload)
            self.events_sent_count += 1

        elif action_type == "cue_cancel":
            cancel_time_ms = args[0]
            payload = {"cancel_time_ms": cancel_time_ms, "is_holding": False}
            self.http_client(f"/api/session/{urllib.parse.quote(session_id)}/cue-up", payload)
            self.events_sent_count += 1

        elif action_type == "speech_range":
            press_time_ms, release_time_ms = args
            payload = {
                "press_time_ms": press_time_ms,
                "release_time_ms": release_time_ms,
                "pre_margin_sec": 2.0,
                "post_margin_sec": 2.0,
                "label": f"グローバルキー発言 ({((release_time_ms - press_time_ms)/1000.0):.1f}s)"
            }
            self.http_client(f"/api/session/{urllib.parse.quote(session_id)}/speech-range", payload)
            self.events_sent_count += 1

        elif action_type == "paste_head":
            # 1. サーバーから先頭の準備完了発言を取得 (copied に遷移)
            paste_url = f"/api/session/{urllib.parse.quote(session_id)}/paste-queue/paste-head"
            res = self.http_client(paste_url, {})
            self.events_sent_count += 1

            target_item = res.get("copied_item") or res.get("pasted_item") or res.get("item")
            if res.get("ok") and target_item:
                # 2. 安全な貼付アクション実行器で検証・実行
                exec_res = self.paste_executor.execute_paste(target_item)
                self.last_paste_result = exec_res

                # 3. 自動注入が無効、またはDocs検証で拒否された場合は、キューアイテムを ready に戻す（保護）
                if not exec_res.get("ok"):
                    confirm_url = f"/api/session/{urllib.parse.quote(session_id)}/paste-queue/confirm"
                    self.http_client(confirm_url, {"id": target_item.get("id"), "success": False})
                    self.last_error = f"貼付安全ガード: {exec_res.get('reason')}"
                else:
                    self.last_error = None
            else:
                self.last_paste_result = {"ok": False, "reason": res.get("error", "queue_empty_or_not_ready")}

    def _dispatch_worker_loop(self):
        while not self._stop_event.is_set():
            try:
                item = self._action_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if item is None:
                break

            action_type, args = item
            try:
                self._handle_action(action_type, args)
            except Exception as exc:
                self.last_error = f"非同期ディスパッチエラー: {exc}"

    def get_status(self) -> dict:
        permission_granted = is_accessibility_trusted(prompt=False)
        can_capture = permission_granted and self.is_running and (self._tap_port is not None)

        status_str = "active" if can_capture else ("stopped" if self.is_running else "idle")
        if not permission_granted:
            status_str = "no_permission"

        return {
            "platform": "darwin",
            "is_running": self.is_running,
            "permission_granted": permission_granted,
            "can_capture_global_keys": can_capture,
            "status": status_str,
            "events_sent_count": self.events_sent_count,
            "last_error": self.last_error,
            "cue_keycode": self.cue_keycode,
            "paste_keycode": self.paste_keycode,
            "cue_mode": self.cue_mode,
            # 安全第一の明示: 自動キーストローク注入は既定で無効
            "auto_key_injection_enabled": self.paste_executor.enable_auto_key_injection,
            "paste_mode": "manual_paste_recommended" if not self.paste_executor.enable_auto_key_injection else "auto_injection_conditional",
            "last_paste_result": self.last_paste_result,
            "notice": "誤入力・クリップボード破損防止のため、Docs前面での自動キーストローク注入は既定で無効化されています。Web画面からの安全な手動ペーストを行ってください。"
        }

    def set_cue_mode(self, mode: str) -> bool:
        """F2キーの発言モードを設定します (hold または toggle)。"""
        if mode not in VALID_CUE_MODES:
            raise ValueError(f"不正な発言モード: {mode}。'hold' または 'toggle' を指定してください。")
        self.cue_mode = mode
        if hasattr(self, "event_handler") and self.event_handler:
            self.event_handler.set_cue_mode(mode)
        return True

    def start(self) -> bool:
        if self.is_running:
            return True

        permission_granted = is_accessibility_trusted(prompt=False)
        if not permission_granted:
            self.last_error = "macOSアクセシビリティ権限がありません。"
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
        self._tap_port = None
        if self._worker_thread and self._worker_thread.is_alive():
            self._action_queue.put(None)

    def _monitor_loop(self):
        try:
            cg = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")

            cg.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            cg.CGEventGetIntegerValueField.restype = ctypes.c_int64

            CGEventTapCallBack = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)

            cg.CGEventTapCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64, CGEventTapCallBack, ctypes.c_void_p]
            cg.CGEventTapCreate.restype = ctypes.c_void_p

            cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
            cg.CGEventTapEnable.restype = None

            cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
            cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p

            cf.CFRunLoopGetCurrent.argtypes = []
            cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p

            cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            cf.CFRunLoopAddSource.restype = None

            cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
            cf.CFRunLoopRunInMode.restype = ctypes.c_int32

            mode_common = ctypes.c_void_p.in_dll(cf, "kCFRunLoopCommonModes")
            mode_default = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

            def tap_callback(proxy, event_type, event, refcon):
                try:
                    if event_type in (10, 11):
                        keycode = cg.CGEventGetIntegerValueField(event, FIELD_KEYBOARD_KEYCODE)
                        autorepeat = cg.CGEventGetIntegerValueField(event, FIELD_KEYBOARD_AUTOREPEAT)
                        self.event_handler.process_key_event(event_type, int(keycode), is_repeat=bool(autorepeat))
                except Exception as ex:
                    print(f"イベントタップコールバック内エラー: {ex}", file=sys.stderr)
                return event

            cb = CGEventTapCallBack(tap_callback)

            mask = (1 << 10) | (1 << 11)
            port = cg.CGEventTapCreate(1, 0, 1, mask, cb, None)
            if not port:
                self.last_error = "CGEventTapCreate に失敗しました (権限不足またはOS拒否)。"
                self.is_running = False
                return

            self._tap_port = port
            run_loop_source = cf.CFMachPortCreateRunLoopSource(None, port, 0)
            current_run_loop = cf.CFRunLoopGetCurrent()
            cf.CFRunLoopAddSource(current_run_loop, run_loop_source, mode_common.value)
            cg.CGEventTapEnable(port, True)

            while not self._stop_event.is_set():
                cf.CFRunLoopRunInMode(mode_default.value, ctypes.c_double(0.5), False)

            cg.CGEventTapEnable(port, False)
        except Exception as exc:
            self.last_error = f"グローバルキー監視ループ異常: {exc}"
        finally:
            self.is_running = False
            self._tap_port = None
