"""模擬国連 2階層辞書管理モジュール (共通辞書 + 会議別辞書)
- 共通辞書 (全会議適用) と 会議別辞書 (各セッション専用) の2階層管理
- 辞書の編集、追加、JSON取込/書出、衝突検知
- ASR (Whisper) および 整文 (Qwen3) への少量の安全な候補適用
- 「AI用 BGとはなにか.md」を参考にした模擬国連基本用語のドラフト候補を初期搭載
  ※基礎辞書は今後のユーザーとの共同ブラッシュアップを前提とし、勝手に確定しない「候補」として管理
"""
import datetime
from difflib import SequenceMatcher
import json
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_COMMON_DICT_PATH = PROJECT_DIR / "辞書" / "共通辞書.json"

INITIAL_MUN_CANDIDATES = [
    {
        "id": "cand_01",
        "surface": "BG",
        "reading": "びーじー",
        "replacement": "BG（バックグラウンドガイド）",
        "category": "会議基本",
        "description": "議題解説冊子。参加者のリサーチ土台となる文書。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_02",
        "surface": "フロント",
        "reading": "ふろんと",
        "replacement": "フロント（議長陣・会議監督）",
        "category": "会議運営",
        "description": "会議監督（ディレクター）や議長団など会議を設計・主導するメンバー。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_03",
        "surface": "デリ",
        "reading": "でり",
        "replacement": "デリゲート（担当国代表）",
        "category": "参加者",
        "description": "各国の担当代表（Delegate）。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_04",
        "surface": "議場",
        "reading": "ぎじょう",
        "replacement": "議場",
        "category": "会議基本",
        "description": "国連総会、安全保障理事会など議題が討議される会議場。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_05",
        "surface": "モデ",
        "reading": "もで",
        "replacement": "モデレート・コーカス",
        "category": "動議・討議",
        "description": "議長主導の公式着席討議形式。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_06",
        "surface": "アンモデ",
        "reading": "あんもで",
        "replacement": "アンモデレート・コーカス",
        "category": "動議・討議",
        "description": "自由離席討議・ロビー活動・非公式交渉形式。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_07",
        "surface": "コーカス",
        "reading": "こーかす",
        "replacement": "コーカス",
        "category": "動議・討議",
        "description": "会議中の討議・交渉時間。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_08",
        "surface": "WP",
        "reading": "だぶりゅーぴー",
        "replacement": "ワーキングペーパー（Working Paper）",
        "category": "文書",
        "description": "作業文書。決議案草案の基礎となる非公式文書。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_09",
        "surface": "DR",
        "reading": "でぃーあーる",
        "replacement": "ドラフトレゾリューション（Draft Resolution）",
        "category": "文書",
        "description": "決議案草案。議長に提出・承認された公式草案。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_10",
        "surface": "レゾ",
        "reading": "れぞ",
        "replacement": "決議案（Resolution）",
        "category": "文書",
        "description": "会議の最終成果物たる公式決議。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_11",
        "surface": "修正案",
        "reading": "しゅうせいあん",
        "replacement": "修正案（Amendment）",
        "category": "文書",
        "description": "提出済み決議案に対する修正動議。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_12",
        "surface": "スポンサー",
        "reading": "すぽんさー",
        "replacement": "主提案国（Sponsor）",
        "category": "決議案",
        "description": "決議案の主執筆国・主提案国。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_13",
        "surface": "シグナトリー",
        "reading": "しぐなとりー",
        "replacement": "賛同国（Signatory）",
        "category": "決議案",
        "description": "議題討議への上程を支持する国（決議自体の賛否とは独立）。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_14",
        "surface": "マンデート",
        "reading": "まんでーと",
        "replacement": "マンデート（権限・信託）",
        "category": "会議基本",
        "description": "機関に与えられた権限・活動範囲・信託。",
        "enabled": True,
        "status": "candidate"
    },
    {
        "id": "cand_15",
        "surface": "ロールコール",
        "reading": "ろーるこーる",
        "replacement": "ロールコール（点呼投票）",
        "category": "投票",
        "description": "各国代表を順に点呼して賛否を記録する公式投票。",
        "enabled": True,
        "status": "candidate"
    }
]


# 会議をまたいで使う基本表記。名称は発話どおりに保つ。
MUN_COMMON_EXTRA = [
    ("動議", "どうぎ", "会議進行"), ("発言権", "はつげんけん", "会議進行"),
    ("発言権譲渡", "はつげんけんじょうと", "会議進行"), ("議長", "ぎちょう", "会議進行"),
    ("公式討議", "こうしきとうぎ", "会議進行"), ("非公式討議", "ひこうしきとうぎ", "会議進行"),
    ("決議案", "けつぎあん", "文書"), ("共同提案国", "きょうどうていあんこく", "文書"),
    ("採択", "さいたく", "投票"), ("賛成", "さんせい", "投票"),
    ("反対", "はんたい", "投票"), ("棄権", "きけん", "投票"),
    ("過半数", "かはんすう", "投票"), ("国連憲章", "こくれんけんしょう", "国連"),
    ("安全保障理事会", "あんぜんほしょうりじかい", "国連"), ("国連総会", "こくれんそうかい", "国連"),
    ("議題設定", "ぎだいせってい", "会議進行"), ("議事進行", "ぎじしんこう", "会議進行"),
    ("議事進行に関する動議", "ぎじしんこうにかんするどうぎ", "会議進行"),
    ("ポイントオブオーダー", "ぽいんとおぶおーだー", "会議進行"),
    ("ポイントオブインフォメーション", "ぽいんとおぶいんふぉめーしょん", "会議進行"),
    ("発言時間", "はつげんじかん", "会議進行"), ("発言者リスト", "はつげんしゃりすと", "会議進行"),
    ("定足数", "ていそくすう", "会議進行"), ("休会", "きゅうかい", "会議進行"),
    ("閉会", "へいかい", "会議進行"), ("延長動議", "えんちょうどうぎ", "会議進行"),
    ("大論点", "だいろんてん", "討議"), ("小論点", "しょうろんてん", "討議"),
    ("合意形成", "ごういけいせい", "討議"), ("コンセンサス", "こんせんさす", "討議"),
    ("懸念", "けねん", "討議"), ("反論", "はんろん", "討議"), ("情報共有", "じょうほうきょうゆう", "討議"),
    ("草案", "そうあん", "文書"), ("作業文書", "さぎょうぶんしょ", "文書"),
    ("決議草案", "けつぎそうあん", "文書"), ("前文", "ぜんぶん", "文書"),
    ("主文", "しゅぶん", "文書"), ("前文条項", "ぜんぶんじょうこう", "文書"),
    ("主文条項", "しゅぶんじょうこう", "文書"), ("共同提案", "きょうどうていあん", "文書"),
    ("署名国", "しょめいこく", "文書"), ("単純過半数", "たんじゅんかはんすう", "投票"),
    ("3分の2", "さんぶんのに", "投票"), ("満場一致", "まんじょういっち", "投票"),
    ("拒否権", "きょひけん", "投票"), ("投票権", "とうひょうけん", "投票"),
    ("オブザーバー", "おぶざーばー", "参加者"), ("加盟国", "かめいこく", "参加者"),
    ("国際連合", "こくさいれんごう", "国連"), ("国際法", "こくさいほう", "国連"),
    ("主権", "しゅけん", "国連"), ("内政不干渉", "ないせいふかんしょう", "国連"),
    ("安全保障", "あんぜんほしょう", "国連"), ("人道支援", "じんどうしえん", "国連"),
    ("人道的介入", "じんどうてきかいにゅう", "国連"), ("保護する責任", "ほごするせきにん", "国連")
]

def _extra_common_entries():
    return [{"id": f"mun_v2_{i}", "surface": name, "reading": reading,
             "replacement": name, "category": category, "description": "",
             "enabled": True, "status": "candidate"}
            for i, (name, reading, category) in enumerate(MUN_COMMON_EXTRA)]


def create_initial_common_dictionary() -> Dict[str, Any]:
    return {
        "name": "模擬国連 共通辞書 (基礎候補ドラフト)",
        "version": "1.0",
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "description": "「AI用 BGとはなにか.md」を参考にした模擬国連基本用語のドラフト候補。今後のユーザーとの共同作業でブラッシュアップする前提の候補群です。",
        "is_draft_candidate": True,
        "mun_common_version": 4,
        "entries": [{**e, "replacement": e["surface"], "description": ""} for e in INITIAL_MUN_CANDIDATES] + _extra_common_entries()
    }


def _write_dictionary_atomically(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".dictionary-", delete=False) as tmp:
            temp_path = Path(tmp.name)
            json.dump(data, tmp, ensure_ascii=False, indent=2)
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _read_existing_dictionary(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            raise ValueError("辞書の形式が不正です")
        return data
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"辞書を読み込めません。元のファイルは保持されています: {path.name}") from exc


def load_common_dictionary(path: Optional[Path] = None) -> Dict[str, Any]:
    target_path = Path(path) if path else DEFAULT_COMMON_DICT_PATH
    if target_path.is_file():
        current = _read_existing_dictionary(target_path)
        if current.get("mun_common_version", 0) < 4:
            entries = current.setdefault("entries", [])
            surfaces = {e.get("surface") for e in entries if isinstance(e, dict)}
            entries.extend(e for e in _extra_common_entries() if e["surface"] not in surfaces)
            current["mun_common_version"] = 4
            _write_dictionary_atomically(target_path, current)
        return current

    initial_dict = create_initial_common_dictionary()
    _write_dictionary_atomically(target_path, initial_dict)
    return initial_dict


def save_common_dictionary(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    target_path = Path(path) if path else DEFAULT_COMMON_DICT_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    data["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_dictionary_atomically(target_path, data)


def load_session_dictionary(session_id: str, data_dir: Path) -> Dict[str, Any]:
    session_dir = (data_dir / session_id).resolve()
    dict_path = session_dir / "会議別辞書.json"
    if dict_path.is_file():
        return _read_existing_dictionary(dict_path)
    return {
        "name": f"会議別辞書 ({session_id})",
        "session_id": session_id,
        "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "description": "本セッション（会議）限定の固有名詞・国名・議題特有の用語辞書です。",
        "entries": []
    }


def save_session_dictionary(session_id: str, data: Dict[str, Any], data_dir: Path) -> None:
    session_dir = (data_dir / session_id).resolve()
    session_dir.mkdir(parents=True, exist_ok=True)
    dict_path = session_dir / "会議別辞書.json"
    data["session_id"] = session_id
    data["updated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _write_dictionary_atomically(dict_path, data)


def detect_conflicts(common_entries: List[Dict[str, Any]], session_entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    conflicts = []
    common_map = {}
    for entry in common_entries:
        surf = entry.get("surface", "").strip()
        if surf:
            common_map[surf] = entry

    for s_entry in session_entries:
        surf = s_entry.get("surface", "").strip()
        if not surf:
            continue
        if surf in common_map:
            c_entry = common_map[surf]
            c_rep = c_entry.get("replacement", "").strip()
            s_rep = s_entry.get("replacement", "").strip()
            if c_rep != s_rep:
                conflicts.append({
                    "surface": surf,
                    "type": "replacement_conflict",
                    "common_entry": c_entry,
                    "session_entry": s_entry,
                    "resolution": "session_override",
                    "message": f"用語「{surf}」の置換先が共通辞書（{c_rep}）と会議別辞書（{s_rep}）で異なります。会議別辞書の設定が優先適用されます。"
                })
    return conflicts


def merge_dictionaries(common_dict: Dict[str, Any], session_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    common_entries = [dict(e) for e in common_dict.get("entries", [])]
    session_entries = [dict(e) for e in (session_dict.get("entries", []) if session_dict else [])]

    conflicts = detect_conflicts(common_entries, session_entries)

    merged_map = {}
    for entry in common_entries:
        surf = entry.get("surface", "").strip()
        if surf:
            entry["tier"] = "common"
            merged_map[surf] = entry

    for entry in session_entries:
        surf = entry.get("surface", "").strip()
        if surf:
            entry["tier"] = "session"
            merged_map[surf] = entry

    merged_entries = list(merged_map.values())
    return {
        "entries": merged_entries,
        "conflicts": conflicts,
        "total_common": len(common_entries),
        "total_session": len(session_entries),
        "total_merged": len(merged_entries)
    }


def validate_and_normalize_countries(countries_input: Any) -> Tuple[List[Dict[str, str]], List[str], List[str]]:
    """
    参加国の国名・コードの一括検証と正規化。
    現存・歴史的・会議独自コードを明示的に受け付け、誤推測を防止。
    """
    normalized: List[Dict[str, str]] = []
    codes: List[str] = []
    warnings: List[str] = []
    seen_codes = set()

    if isinstance(countries_input, str):
        lines = countries_input.strip().splitlines()
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", "//")):
                continue
            match = re.match(r"^([A-Za-z0-9_\-]{2,6})[:：\s\t]+(.+)$", line)
            if match:
                code = match.group(1).upper()
                name_part = match.group(2).strip()
                c_type = "standard"
                if "歴史" in name_part or "旧" in name_part:
                    c_type = "historical"
                elif "独自" in name_part or "NGO" in name_part or "国際機関" in name_part:
                    c_type = "custom"
                clean_name = re.sub(r"[\(（].*?[\)）]", "", name_part).strip()
                if code in seen_codes:
                    warnings.append(f"重複コード: {code}")
                    continue
                seen_codes.add(code)
                codes.append(code)
                normalized.append({"code": code, "name": clean_name or name_part, "type": c_type})
            else:
                warnings.append(f"解釈できない行: {line}")

    elif isinstance(countries_input, list):
        for item in countries_input:
            if isinstance(item, str):
                c = item.strip().upper()
                if re.fullmatch(r"[A-Za-z0-9_\-]{2,6}", c) and not re.search(r"\s", c):
                    if c not in seen_codes:
                        seen_codes.add(c)
                        codes.append(c)
                        normalized.append({"code": c, "name": c, "type": "standard"})
                else:
                    warnings.append(f"無効なコード形式: {item}")
            elif isinstance(item, dict):
                code = str(item.get("code", "")).strip().upper()
                name = str(item.get("name", "")).strip()
                c_type = str(item.get("type", "standard")).strip()
                if not re.fullmatch(r"[A-Za-z0-9_\-]{2,6}", code) or re.search(r"\s", code):
                    warnings.append(f"無効なコード形式: {code}")
                    continue
                if code in seen_codes:
                    warnings.append(f"重複コード: {code}")
                    continue
                seen_codes.add(code)
                codes.append(code)
                country = {"code": code, "name": name or code, "type": c_type}
                if isinstance(item.get("aliases"), list):
                    country["aliases"] = [a.strip() for a in item["aliases"] if isinstance(a, str) and 2 <= len(a.strip()) <= 100][:20]
                normalized.append(country)

    return normalized, codes, warnings


def country_code_prompts(session_dict: Dict[str, Any], recognized_text: str = "") -> Tuple[str, str]:
    """会議で選択した国コード・国名を候補として渡す。発言者の推測は行わない。"""
    countries = session_dict.get("countries", [])
    value = session_dict.get("country_codes", [])

    codes = []
    if isinstance(value, list):
        codes = list(dict.fromkeys(c.upper() for c in value if isinstance(c, str) and re.fullmatch(r"[A-Za-z0-9_\-]{2,6}", c.strip()) and not re.search(r"\s", c.strip())))

    named_countries = []
    if isinstance(countries, list):
        for item in countries:
            if isinstance(item, dict):
                c = str(item.get("code", "")).strip().upper()
                n = str(item.get("name", "")).strip()
                if c and n and c != n:
                    named_countries.append((n, c))
                    aliases = item.get("aliases", [])
                    if isinstance(aliases, list):
                        for alias in aliases:
                            if isinstance(alias, str) and 2 <= len(alias.strip()) <= 100 and alias.strip() != n:
                                named_countries.append((alias.strip(), c))
                if c and c not in codes and not re.search(r"\s", c):
                    codes.append(c)

    if not codes and not named_countries:
        return "", ""

    joined_codes = ", ".join(codes)
    # 音声認識には読み上げられる国名を渡す。コード化は後段の整文で行う。
    asr_names = []
    for name, _code in named_countries:
        if len(', '.join(asr_names + [name])) > 80:
            break
        asr_names.append(name)
    asr_prompt = ', '.join(asr_names)

    if named_countries:
        def variants_for(name):
            variants = [name]
            for suffix in ("合衆国", "共和国", "王国", "連邦"):
                if name.endswith(suffix) and len(name) - len(suffix) >= 2:
                    variants.append(name[:-len(suffix)])
            return variants
        def mentioned(item):
            return any(variant in recognized_text for variant in variants_for(item[0]))
        ordered = sorted(named_countries, key=lambda item: not mentioned(item)) if recognized_text else named_countries
        mapping_lines = [f"- {name} → {code}" for name, code in ordered[:25]]
        example_name, example_code = ordered[0]
        priority_instructions = []
        if recognized_text:
            for name, code in ordered[:25]:
                match = next((v for v in variants_for(name) if re.search(re.escape(v) + r'(?![語人製産籍])', recognized_text)), None)
                if match:
                    priority_instructions.append(f"入力に{match}と書かれた場合、その文字を{code}に置換してから出力してください。")
            if not priority_instructions:
                return asr_prompt, ""
        polish_prompt = (
            "【参加国表記ルール（明示登録国のみ）】\n"
            "以下の登録された参加国について、文脈上特定できる場合は国名の文字表記を指定のコードに変換してください。主体を変える操作ではありません。\n"
            f"表記の例：入力「{example_name}は発言します」→出力「{example_code}は発言します」。例の文言を発言に追加しないでください。\n"
            + "\n".join(mapping_lines) + "\n"
            "※登録されていない国名や、文脈上国名を特定できない場合は勝手にコード変換せず、発言のまま表記してください。\n"
            "発言者や国を推測して追加せず、判別できない場合は認識結果を維持する。"
        )
        if priority_instructions:
            polish_prompt += "\n最重要：" + "".join(priority_instructions)
    else:
        if recognized_text:
            return asr_prompt, ""
        polish_prompt = (
            "この会議の参加国コード（ISO 3166-1 alpha-3等）の候補: " + joined_codes +
            "。音声の文脈で参加国の国名を特定できる場合は対応するコード表記を優先。"
            "発言者や国を推測して追加せず、判別できない場合は認識結果を維持する。"
        )

    return asr_prompt, polish_prompt


def get_asr_prompt(merged_dict: Dict[str, Any], max_chars: int = 100) -> str:
    entries = sorted(merged_dict.get("entries", []), key=lambda e: e.get("tier") != "session")
    session_entries = [e for e in entries if e.get("tier") == "session" and e.get("enabled", True) and e.get("surface", "").strip()]
    if session_entries:
        entries = session_entries
    selected = []
    current_len = 0
    for e in entries:
        if not e.get("enabled", True):
            continue
        surf = e.get("surface", "").strip()
        if not surf:
            continue
        clean_surf = surf.replace(",", " ").strip()
        if current_len + len(clean_surf) + 2 > max_chars:
            break
        selected.append(clean_surf)
        current_len += len(clean_surf) + 2

    return ", ".join(selected)


def get_polishing_prompt(merged_dict: Dict[str, Any], max_entries: int = 15, recognized_text: str = "") -> str:
    international_categories = {"国連", "国際法", "条約名", "機関名"}
    international_markers = ("国際", "国家", "国連", "条約", "政府", "安保理", "世界", "紛争", "戦争", "軍事", "主権", "人権", "人道", "介入", "侵略", "平和", "ICC", "ICJ")
    international_context = any(marker in recognized_text for marker in international_markers)
    def eligible(entry):
        surface = entry.get("surface", "").strip()
        if not recognized_text or surface in recognized_text:
            return True
        return entry.get("category") not in international_categories or international_context
    def relevance(surface):
        if not recognized_text or not surface:
            return 0.0
        if surface in recognized_text:
            return 1.0
        # 文字起こしで一部が誤認された用語も候補に残す。置換は行わない。
        if len(surface) < 3 or len(surface) > 32:
            return 0.0
        source = recognized_text[:4000]
        best = 0.0
        position = source.find(surface[0])
        checked = 0
        while position >= 0 and checked < 80:
            for width in (len(surface)-1, len(surface), len(surface)+1):
                fragment = source[position:position + width]
                if len(set(surface) & set(fragment)) >= 2:
                    best = max(best, SequenceMatcher(None, surface, fragment, autojunk=False).ratio())
            position = source.find(surface[0], position + 1)
            checked += 1
        return best if best >= 0.4 else 0.0
    def priority(entry):
        surface = entry.get("surface", "").strip()
        return (-relevance(surface), entry.get("tier") != "session")
    entries = sorted(merged_dict.get("entries", []), key=priority)
    active = [e for e in entries if e.get("enabled", True) and e.get("replacement") and eligible(e)]
    if not active:
        return ""

    lines = [
        "【用語表記統一ルール（辞書指定）】",
        "発言内に以下の用語が現れた場合、文脈に沿って指定の表記を優先してください。",
        "ただし、発言者の立場・主張・賛否・数値・条約番号の意味は絶対に変更しないでください。"
    ]
    candidates = []
    source = recognized_text[:4000]
    for e in active[:max_entries]:
        surf = e.get("surface", "").strip()
        rep = e.get("replacement", "").strip()
        desc = e.get("description", "").strip()
        if desc:
            lines.append(f"- {surf} → {rep} （{desc}）")
        else:
            lines.append(f"- {surf} → {rep}")
        if source and surf and surf not in source and 3 <= len(surf) <= 32:
            pos = source.find(surf[0])
            checked = 0
            while pos >= 0 and checked < 80:
                fragment = source[pos:pos + len(surf)]
                if len(fragment) == len(surf) and re.fullmatch(r"[一-龥ぁ-ゖァ-ヺー]+", fragment):
                    score = SequenceMatcher(None, surf, fragment, autojunk=False).ratio()
                    if score >= 0.4 and len(set(surf) & set(fragment)) >= 2:
                        candidates.append((score, fragment, rep))
                pos = source.find(surf[0], pos + 1)
                checked += 1
    if candidates:
        lines.append("【音声認識の誤認候補】文脈に合う場合のみ検討し、内容を勝手に追加しないでください。")
        seen_pairs = set()
        for _, fragment, rep in sorted(candidates, reverse=True):
            if (fragment, rep) not in seen_pairs:
                lines.append(f"- 認識「{fragment}」の候補「{rep}」")
                seen_pairs.add((fragment, rep))
                if len(seen_pairs) >= 3:
                    break

    return "\n".join(lines)


def import_dictionary_json(json_content: str, target_dict: Dict[str, Any], keep_existing: bool = False) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    parsed = json.loads(json_content)
    incoming_entries = []
    if isinstance(parsed, list):
        incoming_entries = parsed
    elif isinstance(parsed, dict) and "entries" in parsed:
        incoming_entries = parsed["entries"]
    else:
        raise ValueError('取り込みJSONは辞書エントリの配列または {"entries": [...]} 形式である必要があります。')

    if keep_existing:
        if not isinstance(incoming_entries, list):
            raise ValueError("entries は配列で指定してください。")
        for index, item in enumerate(incoming_entries, 1):
            if not isinstance(item, dict) or not isinstance(item.get("surface"), str) or not item["surface"].strip():
                raise ValueError(f"{index}件目の見出し語（surface）を確認してください。")
            for key in ("replacement", "reading", "category", "description"):
                if key in item and not isinstance(item[key], str):
                    raise ValueError(f"{index}件目の {key} は文字列で指定してください。")
            if "enabled" in item and not isinstance(item["enabled"], bool):
                raise ValueError(f"{index}件目の enabled は true または false で指定してください。")

    existing_entries = target_dict.get("entries", [])
    existing_map = {e.get("surface", "").strip(): e for e in existing_entries if e.get("surface")}

    conflicts = []
    imported_count = 0
    now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for item in incoming_entries:
        if not isinstance(item, dict):
            continue
        surf = item.get("surface", "").strip()
        if not surf:
            continue
        rep = item.get("replacement", "").strip() or surf

        if surf in existing_map:
            old_item = existing_map[surf]
            if old_item.get("replacement", "").strip() != rep:
                conflicts.append({
                    "surface": surf,
                    "existing_replacement": old_item.get("replacement"),
                    "incoming_replacement": rep,
                    "message": f"「{surf}」の置換先が既存（{old_item.get('replacement')}）と取り込みデータ（{rep}）で競合しています。"
                })
            if keep_existing:
                continue
            old_item["reading"] = item.get("reading", old_item.get("reading", ""))
            old_item["replacement"] = rep
            old_item["category"] = item.get("category", old_item.get("category", "インポート"))
            old_item["description"] = item.get("description", old_item.get("description", ""))
            old_item["enabled"] = bool(item.get("enabled", True))
            old_item["updated_at"] = now_ts
        else:
            new_entry = {
                "id": item.get("id") or f"entry_{len(existing_entries) + imported_count + 1:04d}",
                "surface": surf,
                "reading": item.get("reading", ""),
                "replacement": rep,
                "category": item.get("category", "インポート"),
                "description": item.get("description", ""),
                "enabled": bool(item.get("enabled", True)),
                "status": item.get("status", "imported"),
                "created_at": now_ts
            }
            existing_entries.append(new_entry)
            existing_map[surf] = new_entry
            imported_count += 1

    target_dict["entries"] = existing_entries
    target_dict["updated_at"] = now_ts
    return target_dict, conflicts


def export_dictionary_json(dict_data: Dict[str, Any]) -> str:
    return json.dumps(dict_data, ensure_ascii=False, indent=2)
