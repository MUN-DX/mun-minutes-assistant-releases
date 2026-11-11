"""模擬国連 議事録支援システム - 設定およびストレージ管理モジュール"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import mun_minutes


def get_disk_free_gb(target_path: Path) -> Optional[float]:
    """指定パスのドライブ空き容量（GB）を取得します。"""
    try:
        p = Path(target_path).resolve()
        while not p.exists() and p != p.parent:
            p = p.parent
        if p.exists():
            usage = shutil.disk_usage(p)
            return round(usage.free / (1024 ** 3), 1)
    except Exception:
        pass
    return None


def get_disk_total_gb(target_path: Path) -> Optional[float]:
    """指定パスのドライブ総容量（GB）を取得します。"""
    try:
        p = Path(target_path).resolve()
        while not p.exists() and p != p.parent:
            p = p.parent
        if p.exists():
            usage = shutil.disk_usage(p)
            return round(usage.total / (1024 ** 3), 1)
    except Exception:
        pass
    return None


def is_directory_writable(target_path: Path) -> bool:
    """指定パスへの書き込み権限を安全に確認します（一時プローブ作成後に即削除）。"""
    try:
        target_path = Path(target_path)
        target_path.mkdir(parents=True, exist_ok=True)
        probe = target_path / f".write_test_{os.getpid()}_{id(target_path)}.tmp"
        probe.write_text("write_ok", encoding="utf-8")
        if probe.exists():
            probe.unlink(missing_ok=True)
            return True
    except Exception:
        pass
    return False


def load_config(config_file: Path, project_dir: Path) -> Dict[str, Any]:
    """config.json から設定を安全に読み込みます。未設定時はデフォルト値を返します。"""
    defaults = {
        "data_dir": project_dir / "議事録データ",
        "models_dir": project_dir / "モデル",
        "whisper_model": project_dir / "モデル" / "ggml-large-v3-turbo.bin",
        "whisper_cmd": "whisper-cli",
        "ffmpeg_cmd": "ffmpeg",
        "ollama_url": "http://127.0.0.1:11434",
        "llm_model": "qwen3:4b-instruct-2507-q4_K_M",
        "common_dict_path": project_dir / "辞書" / "共通辞書.json",
    }

    if not config_file.is_file():
        return defaults

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            saved = json.load(f)
        if not isinstance(saved, dict):
            return defaults

        loaded = dict(defaults)
        if "data_dir" in saved and saved["data_dir"]:
            loaded["data_dir"] = Path(saved["data_dir"]).resolve()
        if "models_dir" in saved and saved["models_dir"]:
            loaded["models_dir"] = Path(saved["models_dir"]).resolve()
        if "whisper_model" in saved and saved["whisper_model"]:
            loaded["whisper_model"] = Path(saved["whisper_model"]).resolve()
        if "whisper_cmd" in saved and isinstance(saved["whisper_cmd"], str):
            loaded["whisper_cmd"] = saved["whisper_cmd"].strip()
        if "ffmpeg_cmd" in saved and isinstance(saved["ffmpeg_cmd"], str):
            loaded["ffmpeg_cmd"] = saved["ffmpeg_cmd"].strip()
        if "ollama_url" in saved and isinstance(saved["ollama_url"], str):
            loaded["ollama_url"] = saved["ollama_url"].strip()
        if "llm_model" in saved and isinstance(saved["llm_model"], str):
            loaded["llm_model"] = saved["llm_model"].strip()
        return loaded
    except Exception:
        return defaults


def save_config(config_file: Path, config_data: Dict[str, Any]) -> bool:
    """設定を config.json にアトミックに保存します（既存ファイル破損防止）。"""
    try:
        data_to_save = {
            "data_dir": str(Path(config_data["data_dir"]).resolve()) if "data_dir" in config_data else None,
            "models_dir": str(Path(config_data["models_dir"]).resolve()) if "models_dir" in config_data else None,
            "whisper_model": str(Path(config_data["whisper_model"]).resolve()) if "whisper_model" in config_data else None,
            "whisper_cmd": str(config_data.get("whisper_cmd", "whisper-cli")),
            "ffmpeg_cmd": str(config_data.get("ffmpeg_cmd", "ffmpeg")),
            "ollama_url": str(config_data.get("ollama_url", "http://127.0.0.1:11434")),
            "llm_model": str(config_data.get("llm_model", "qwen3:4b-instruct-2507-q4_K_M")),
        }
        # 親ディレクトリ確認
        config_file.parent.mkdir(parents=True, exist_ok=True)
        # 一時ファイルへ書き込み後にアトミックリネーム
        tmp_file = config_file.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data_to_save, f, ensure_ascii=False, indent=2)
        tmp_file.replace(config_file)
        return True
    except Exception as exc:
        print(f"設定保存エラー: {exc}")
        return False


def validate_and_apply_settings(
    current_config: Dict[str, Any],
    new_settings: Dict[str, Any],
    project_dir: Path
) -> Tuple[bool, Dict[str, Any], Optional[str]]:
    """
    新しい設定値を検証し、安全であることが確認できた場合のみ設定を更新します。
    ※既存データ・音声・モデルの削除・上書きは絶対に行いません。
    """
    updated = dict(current_config)

    # 1. 議事録保存先 (data_dir) の検証
    if "data_dir" in new_settings:
        raw_path = str(new_settings["data_dir"]).strip()
        if not raw_path:
            return False, current_config, "議事録保存先パスが空です。"
        target = Path(raw_path).expanduser().resolve()
        if target.is_file():
            return False, current_config, "指定された議事録保存先はファイルです。ディレクトリを指定してください。"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return False, current_config, f"議事録保存先ディレクトリの作成に失敗しました: {exc}"
        if not is_directory_writable(target):
            return False, current_config, f"議事録保存先 {target} への書き込み権限がありません。"
        updated["data_dir"] = target

    # 2. モデル保存先 (models_dir) の検証
    if "models_dir" in new_settings:
        raw_path = str(new_settings["models_dir"]).strip()
        if not raw_path:
            return False, current_config, "モデル保存先パスが空です。"
        target = Path(raw_path).expanduser().resolve()
        if target.is_file():
            return False, current_config, "指定されたモデル保存先はファイルです。ディレクトリを指定してください。"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return False, current_config, f"モデル保存先ディレクトリの作成に失敗しました: {exc}"
        if not is_directory_writable(target):
            return False, current_config, f"モデル保存先 {target} への書き込み権限がありません。"
        updated["models_dir"] = target

        # もし新しいモデルディレクトリに現在のwhisper_modelと同名ファイルがあれば切り替え
        curr_wm = Path(updated.get("whisper_model", project_dir / "モデル" / "ggml-large-v3-turbo.bin"))
        candidate = target / curr_wm.name
        if candidate.is_file():
            updated["whisper_model"] = candidate
        elif not curr_wm.is_file():
            # 新ディレクトリ内で最初に見つかった .bin モデルがあればそれを設定
            bins = list(target.glob("*.bin"))
            if bins:
                updated["whisper_model"] = bins[0]

    # 3. Ollama URL の検証 (loopback限定・リダイレクト/クレデンシャル遮断)
    if "ollama_url" in new_settings:
        raw_url = str(new_settings["ollama_url"]).strip()
        if raw_url:
            try:
                validated_url = mun_minutes.validate_ollama_url(raw_url)
                updated["ollama_url"] = validated_url
            except Exception as exc:
                return False, current_config, f"無効なOllama URLです ({exc})。http://127.0.0.1 のみ許可されます。"

    # 4. コマンド名 (whisper_cmd, ffmpeg_cmd) の検証 (シェルメタ文字禁止)
    unsafe_pattern = re.compile(r"[;&|`$<>]|(\\n)")
    for cmd_key in ("whisper_cmd", "ffmpeg_cmd"):
        if cmd_key in new_settings:
            val = str(new_settings[cmd_key]).strip()
            if not val:
                return False, current_config, f"{cmd_key} が空です。"
            if unsafe_pattern.search(val):
                return False, current_config, f"{cmd_key} に不正な文字が含まれています。"
            updated[cmd_key] = val

    # 5. LLMモデル名の検証
    if "llm_model" in new_settings:
        val = str(new_settings["llm_model"]).strip()
        if not val or unsafe_pattern.search(val):
            return False, current_config, "無効なLLMモデル名です。"
        updated["llm_model"] = val

    return True, updated, None
