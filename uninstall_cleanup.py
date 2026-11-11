"""Remove only application-owned runtime directories after explicit confirmation."""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main():
    print('議事録支援：削除のお手伝い')
    print('アプリ内に追加された実行用ファイル（.runtime と .venv）だけを削除します。')
    print('議事録、音声、辞書、モデル、設定、他のアプリで使う Ollama は残します。')
    print('最後にアプリ本体を削除するには、必要なデータを別の場所に保存してから')
    print(f'フォルダを自分でゴミ箱に移動してください: {ROOT}')
    try:
        answer = input('実行用ファイルを削除しますか？ はい の場合だけ Y を入力して Enter: ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print('\n何も削除しませんでした。')
        return 0
    if answer != 'y':
        print('何も削除しませんでした。')
        return 0
    active_python = Path(sys.executable).resolve()
    for name in ('.runtime', '.venv'):
        target = ROOT / name
        if not target.exists():
            continue
        if target.is_symlink() or not target.is_dir():
            print(f'{name}: フォルダを確認できないため残しました。')
            continue
        try:
            if active_python.is_relative_to(target.resolve()):
                print(f'{name}: 現在使用中のため残しました。終了後に手動で削除できます。')
                continue
        except AttributeError:  # Python 3.8
            if target.resolve() in active_python.parents:
                print(f'{name}: 現在使用中のため残しました。')
                continue
        try:
            shutil.rmtree(target)
            print(f'{name}: 削除しました。')
        except OSError as error:
            print(f'{name}: 自動削除できませんでした。{error}')
    print('終了しました。データを残す場合はフォルダを削除する前に別の場所へコピーしてください。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
