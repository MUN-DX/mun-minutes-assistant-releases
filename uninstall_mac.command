#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 uninstall_cleanup.py
printf '\nEnter キーを押して閉じてください。'
read -r
