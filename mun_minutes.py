"""ローカル音声認識・整文の開発用コマンドおよびWebサーバー起動。"""
import argparse
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

PROMPT = ('あなたは日本語の模擬国連議事録の整文担当です。音声認識結果を、意味を変えずに読みやすく整えてください。'
          '誤字や漢字変換、言い淀み、重複や倒置を直し、意味を保って語順を整理してください。意味のまとまりで改行してください。'
          '賛否、条件、否定、程度、主体、対象、数字、条文番号を変えないでください。できませんでした等の否定表現はその表記も保持してください。'
          '文脈上不自然な音声認識の誤認は、辞書や会議背景と音や前後の文が合う場合に限り修正してください。引用符の中も対象です。'
          '意味や主語を推測して補わず、複数の解釈がある場合は原文を維持してください。話されていない内容を追加しないでください。'
          '別話者の意見を混ぜず、要約せず、省略せず、整文結果のみ出力してください。')


def validate_ollama_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or
            parsed.username is not None or parsed.password is not None or
            parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Ollamaの接続先は http://127.0.0.1 のみ指定できます。')
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError('ポート番号が不正です。') from exc
    return 'http://127.0.0.1' + (':' + str(port) if port else '')


def is_digital_silence(wav_path):
    """変換済みPCMが完全なデジタル無音か判定する。小さな音声は除外しない。"""
    with wave.open(str(wav_path), 'rb') as source:
        while True:
            frames = source.readframes(65536)
            if not frames:
                return True
            if any(frames):
                return False


def combine_wav_files(paths, destination, max_seconds=180):
    """連続した同形式PCMを連結する。非対応形式や長すぎる発言は False。"""
    params = None
    total_frames = 0
    try:
        for path in paths:
            with wave.open(str(path), 'rb') as source:
                current = (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype())
                if current[3] != 'NONE' or (params is not None and current != params):
                    return False
                params = current
                total_frames += source.getnframes()
        if not params or total_frames <= 0 or (max_seconds is not None and max_seconds > 0 and total_frames / params[2] > max_seconds):
            return False
        with wave.open(str(destination), 'wb') as output:
            output.setnchannels(params[0])
            output.setsampwidth(params[1])
            output.setframerate(params[2])
            for path in paths:
                with wave.open(str(path), 'rb') as source:
                    while True:
                        frames = source.readframes(65536)
                        if not frames:
                            break
                        output.writeframes(frames)
        return True
    except (OSError, EOFError, wave.Error):
        return False


def transcribe(audio, model, whisper, ffmpeg, prompt: str = ""):
    with tempfile.TemporaryDirectory(prefix='mun-minutes-') as folder:
        tmp = Path(folder)
        wav = tmp / 'input.wav'
        target = tmp / 'transcript'
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', str(audio), '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)], check=True)
        if is_digital_silence(wav):
            raise RuntimeError('無音のため文字起こしできません。マイクと録音範囲を確認してください。')
        cmd = [whisper, '-m', str(model), '-f', str(wav), '-l', 'ja', '-otxt', '-of', str(target)]
        if prompt and prompt.strip():
            cmd.extend(['--prompt', prompt.strip()])
        subprocess.run(cmd, check=True)
        text = target.with_suffix('.txt').read_text(encoding='utf-8').strip()
        if not text:
            raise RuntimeError('音声認識結果が空でした。')
        return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Ollamaのリダイレクトは許可されていません。')


def _polish_single_window(raw_chunk: str, base_url: str, model: str, dictionary_prompt: str = "") -> str:
    url = validate_ollama_url(base_url) + '/api/generate'
    system_text = PROMPT
    if dictionary_prompt and dictionary_prompt.strip():
        system_text += "\n\n" + dictionary_prompt.strip()

    prompt_text = '次の音声認識結果を整文してください。\n\n' + raw_chunk
    payload = {
        'model': model,
        'system': system_text,
        'prompt': prompt_text,
        'stream': False,
        'options': {'temperature': 0, 'num_ctx': 4096, 'num_predict': min(1400, max(256, len(raw_chunk) * 2 + 128)), 'repeat_penalty': 1.15},
        'keep_alive': '0s'
    }
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=300) as response:
        result = json.load(response)
    text = result.get('response', '').strip()
    if not text or result.get('done_reason') != 'stop':
        raise RuntimeError('整文が空か、途中で終了しました。原文を確認してください。')

    # 否定脱落・歪みリスクの検査（推測改変・一括置換は厳禁、危険時は原文保持）
    critical_negations = [
        "できませんでした",
        "防ぐことができませんでした",
        "十分に対応できない",
        "ありませんでした",
    ]
    for neg in critical_negations:
        if neg in raw_chunk and neg not in text:
            return raw_chunk

    return text


def polish(raw, base_url, model, dictionary_prompt: str = ""):
    if not raw or not raw.strip():
        return ""

    # 4kコンテキスト長文対策: 辞書や日本語トークンの余裕を残して分割する。
    MAX_WINDOW_CHARS = 600
    if len(raw) <= MAX_WINDOW_CHARS:
        return _polish_single_window(raw, base_url, model, dictionary_prompt=dictionary_prompt)

    # 句点または改行で安全に分割
    sentences = re.split(r'([。\n]+)', raw)
    windows = []
    current_win = ""
    for i in range(0, len(sentences), 2):
        sent = sentences[i]
        sep = sentences[i+1] if i+1 < len(sentences) else ""
        combined = sent + sep
        if len(current_win) + len(combined) > MAX_WINDOW_CHARS and current_win:
            windows.append(current_win.strip())
            current_win = combined
        else:
            current_win += combined
    if current_win.strip():
        windows.append(current_win.strip())

    polished_parts = []
    for win in windows:
        if len(win) > MAX_WINDOW_CHARS:
            raise RuntimeError('長い文を安全に分割できません。原文と音声を保持します。')
        part = _polish_single_window(win, base_url, model, dictionary_prompt=dictionary_prompt)
        if not part:
            raise RuntimeError('長文整文の一部が空になりました。原文を保持します。')
        polished_parts.append(part)

    return "".join(polished_parts)


def get_audio_duration(audio_path, ffmpeg_cmd='ffmpeg') -> float:
    """音声ファイルの長さを秒単位で返します。"""
    cmd = [ffmpeg_cmd, '-i', str(audio_path)]
    res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):(\d+\.\d+)', res.stderr)
    if m:
        h, mn, s = m.groups()
        return int(h) * 3600 + int(mn) * 60 + float(s)
    return 0.0


def detect_silence_points(audio_path, silence_db=-30.0, silence_dur=0.7, ffmpeg_cmd='ffmpeg') -> list:
    """ffmpegのsilencedetectを用いて無音終了位置（秒）のリストを返します。"""
    cmd = [ffmpeg_cmd, '-i', str(audio_path), '-af', f'silencedetect=noise={silence_db}dB:d={silence_dur}', '-f', 'null', '-']
    res = subprocess.run(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    points = []
    for line in res.stderr.splitlines():
        if 'silence_end:' in line:
            m = re.search(r'silence_end:\s*([\d\.]+)', line)
            if m:
                points.append(float(m.group(1)))
    return sorted(points)


def calculate_chunk_intervals(duration: float, silences: list, min_dur=10.0, target_dur=20.0, max_dur=30.0) -> list:
    """無音区間を優先しつつ15〜30秒程度の区間分割ポイントを計算します。"""
    if duration <= 0:
        return []
    intervals = []
    current_start = 0.0
    for s in silences:
        if s <= current_start + min_dur:
            continue
        if s - current_start >= target_dur:
            while current_start + max_dur < s:
                intervals.append((current_start, current_start + max_dur))
                current_start += max_dur
            if s - current_start >= min_dur:
                intervals.append((current_start, s))
                current_start = s
    while current_start < duration:
        end = min(current_start + max_dur, duration)
        if end - current_start >= 1.0:
            intervals.append((current_start, end))
            current_start = end
        else:
            if intervals:
                intervals[-1] = (intervals[-1][0], duration)
            else:
                intervals.append((current_start, duration))
            current_start = duration
            break
    if len(intervals) > 1 and (intervals[-1][1] - intervals[-1][0]) < 3.0:
        intervals[-2] = (intervals[-2][0], intervals[-1][1])
        intervals.pop()
    return intervals


def split_audio_into_chunks(audio_path, output_dir, min_dur=10.0, target_dur=20.0, max_dur=30.0, ffmpeg_cmd='ffmpeg') -> list:
    """音声を意味が途切れにくい区間（無音境界を優先し15〜30秒程度）単位で分割して保存します。"""
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = get_audio_duration(audio_path, ffmpeg_cmd=ffmpeg_cmd)
    if duration <= 0:
        raise ValueError('音声ファイルの長さを取得できませんでした。')

    silences = detect_silence_points(audio_path, ffmpeg_cmd=ffmpeg_cmd)
    intervals = calculate_chunk_intervals(duration, silences, min_dur=min_dur, target_dur=target_dur, max_dur=max_dur)

    chunk_paths = []
    for idx, (st, en) in enumerate(intervals):
        out_chunk = output_dir / f'chunk_{idx:04d}.wav'
        cmd = [
            ffmpeg_cmd, '-v', 'error', '-y',
            '-ss', f'{st:.3f}', '-to', f'{en:.3f}',
            '-i', str(audio_path),
            '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le',
            str(out_chunk)
        ]
        subprocess.run(cmd, check=True)
        chunk_paths.append(out_chunk)
    return chunk_paths


def process_audio_chunk(audio_chunk, whisper_model, whisper_cmd, ffmpeg_cmd, ollama_url, llm_model, asr_prompt: str = "", dictionary_prompt: str = ""):
    """単一の音声区間を認識・整文し、(生原文, 整文) のタプルを返します。"""
    raw = transcribe(audio_chunk, whisper_model, whisper_cmd, ffmpeg_cmd, prompt=asr_prompt)
    polished = polish(raw, ollama_url, llm_model, dictionary_prompt=dictionary_prompt)
    return raw, polished


def process_file(audio, output, model, whisper, ffmpeg, ollama_url, llm_model, asr_prompt: str = "", dictionary_prompt: str = ""):
    validate_ollama_url(ollama_url)
    audio, output, model = Path(audio), Path(output), Path(model)
    if not audio.is_file() or not model.is_file():
        raise FileNotFoundError('音声ファイルまたはWhisperモデルが見つかりません。')
    output.mkdir(parents=True, exist_ok=True)
    raw_path, polished_path = output / '原文.txt', output / '整文.txt'
    if raw_path.exists() or polished_path.exists():
        raise FileExistsError('出力先に結果があります。別のフォルダを指定してください。')
    print('音声を認識しています…', flush=True)
    raw = transcribe(audio, model, whisper, ffmpeg, prompt=asr_prompt)
    with raw_path.open('x', encoding='utf-8') as f:
        f.write(raw)
    print('認識原文を保存しました。整文しています…', flush=True)
    text = polish(raw, ollama_url, llm_model, dictionary_prompt=dictionary_prompt)
    with polished_path.open('x', encoding='utf-8') as f:
        f.write(text)
    return raw_path, polished_path


def main():
    parser = argparse.ArgumentParser(description='音声ファイルをローカルで文字起こし・整文します。またはローカルWebサーバー(Chrome UI)を起動します。')
    parser.add_argument('audio', type=Path, nargs='?', default=None, help='入力音声ファイル（CLI実行時）')
    parser.add_argument('--server', action='store_true', help='ローカルWebサーバー(Chrome UI)を起動します')
    parser.add_argument('--port', type=int, default=8000, help='Webサーバーポート番号')
    parser.add_argument('--output', type=Path, default=None, help='結果の保存先（CLI実行時）')
    parser.add_argument('--whisper-model', type=Path, default=Path(__file__).resolve().parent / 'モデル' / 'ggml-large-v3-turbo.bin', help='whisper.cppのモデルファイル')
    parser.add_argument('--whisper', default='whisper-cli', help='whisper.cppのコマンド')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='ffmpegのコマンド')
    parser.add_argument('--ollama-url', default='http://127.0.0.1:11434')
    parser.add_argument('--llm-model', default='qwen3:4b-instruct-2507-q4_K_M')
    parser.add_argument('--no-browser', action='store_true', help='ブラウザを自動で開かない')
    parser.add_argument('--split-test', action='store_true', help='音声を無音区間で分割してテスト保存')
    args = parser.parse_args()

    if args.split_test and args.audio:
        out_dir = args.output or Path('議事録データ') / 'split_test'
        chunks = split_audio_into_chunks(args.audio, out_dir, ffmpeg_cmd=args.ffmpeg)
        print(f'{len(chunks)} 個の区間に分割しました: {out_dir}')
        return 0

    if args.server or args.audio is None:
        import web_app
        return web_app.run_server(
            port=args.port,
            whisper_model=args.whisper_model,
            whisper_cmd=args.whisper,
            ffmpeg_cmd=args.ffmpeg,
            ollama_url=args.ollama_url,
            llm_model=args.llm_model,
            open_browser=not args.no_browser
        )

    if args.output is None:
        print('エラー: CLI実行には --output が必要です。', file=sys.stderr)
        return 1

    try:
        paths = process_file(args.audio, args.output, args.whisper_model, args.whisper, args.ffmpeg, args.ollama_url, args.llm_model)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('処理に失敗しました: {}'.format(exc), file=sys.stderr)
        return 1
    print('保存しました: {} / {}'.format(*paths))
    return 0


if __name__ == '__main__':
    sys.exit(main())


def merge_chunk_texts_with_overlap_detection(chunk_texts: list, has_audio_overlap: bool = False) -> str:
    """発言チャンクのテキストを安全に接続します。
    現フロントは非重複音声チャンクを送信するため、重複音声やタイムスタンプの証拠（has_audio_overlap=True）
    がない場合は文字列一致のみによる削除は絶対に行わず、話者の正当な反復・強調を保護します。
    重複音声の証拠がある場合のみ境界重複を解消します。
    """
    if not chunk_texts:
        return ""
    valid_chunks = [t.strip() for t in chunk_texts if t and t.strip()]
    if not valid_chunks:
        return ""
    if len(valid_chunks) == 1:
        return valid_chunks[0]

    if not has_audio_overlap:
        result = valid_chunks[0]
        for nxt in valid_chunks[1:]:
            result = result + nxt
        return result

    result = valid_chunks[0]
    for next_text in valid_chunks[1:]:
        max_overlap = min(len(result), len(next_text), 40)
        overlap_len = 0
        for l in range(max_overlap, 3, -1):
            suffix = result[-l:]
            prefix = next_text[:l]
            if suffix == prefix:
                overlap_len = l
                break

        if overlap_len > 0:
            result = result + next_text[overlap_len:]
        else:
            result = result + next_text

    return result


def compute_text_diff(old_text: str, new_text: str) -> dict:
    """既存テキストと完成版候補の差分を計算します。"""
    import difflib
    old_lines = (old_text or "").splitlines(keepends=True)
    new_lines = (new_text or "").splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile="現在（編集・下書き）",
        tofile="AI完成版候補",
        n=2
    ))
    unified = "".join(diff)
    return {
        "unified_diff": unified,
        "has_changes": (old_text or "").strip() != (new_text or "").strip(),
        "old_length": len(old_text or ""),
        "new_length": len(new_text or "")
    }
