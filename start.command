#!/bin/bash
# Mac 双击启动：阻止睡眠 + 启动网页和实时监控 + 自动打开浏览器
cd "$(dirname "$0")"
PORT=$(python3 -c "import sys;sys.path.insert(0,'.');from core.config import load_config;print(load_config()['server']['port'])" 2>/dev/null || echo 8686)
(sleep 2 && open "http://127.0.0.1:$PORT") &
echo "AI 量化决策台启动中…  关闭这个窗口即停止。"
exec caffeinate -i python3 app.py
