# 外部ソフトウェアとデータ

この配布物には、Pythonのソースコード、画面、国コードデータ、辞書の入力例を含みます。Python本体、FFmpeg、whisper.cpp、Ollama、音声認識モデル、整文モデルの実体は同梱していません。各ソフトウェア・モデルは、導入時にそれぞれの配布元と利用条件を確認してください。

## 国コードデータ

`web/iso-countries.json`はpycountry 24.6.1のISO 3166-1 alpha-2 / alpha-3データから生成されています。pycountryはDebian iso-codesのデータを取り込みます。

- 出典: https://github.com/pycountry/pycountry
- ライセンス: LGPL-2.1-only
- 出典の記録: `web/ISO_COUNTRIES_SOURCE.txt`
- ライセンス本文: `web/ISO_COUNTRIES_LICENSE.txt`

## 別途導入するソフトウェア・モデル

- Python: https://www.python.org/
- FFmpeg: https://ffmpeg.org/
- whisper.cpp: https://github.com/ggml-org/whisper.cpp
- Ollama: https://ollama.com/
- Whisperモデル: https://huggingface.co/ggerganov/whisper.cpp
- Qwenモデル: https://ollama.com/library/qwen3

アプリ本体のオープンソースライセンスは、現時点では設定していません。外部ソフトウェア・モデル・データのライセンスは、アプリ本体とは別に適用されます。
