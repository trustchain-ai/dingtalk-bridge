#!/usr/bin/env bash
# 启动钉钉 <-> Claude 桥接(入站 Stream 模式)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "首次运行:创建 venv 并安装依赖…"
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f .env ]; then
  echo "缺少 .env —— 请先: cp .env.example .env 并填入 AppKey/AppSecret" >&2
  exit 1
fi

exec ./.venv/bin/python bridge.py
