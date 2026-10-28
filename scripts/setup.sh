#!/bin/bash
# 一键设置：下载指定版本的 MediaCrawler 并安装依赖。
# 用法：MEDIACRAWLER_REF=<commit-sha> bash scripts/setup.sh
#   - MEDIACRAWLER_REF 默认 main，建议固定为具体 commit SHA，保证可复现
#   - 已存在 MediaCrawler/ 目录时不会重新下载（如需更新请先自行备份 data/，再删除该目录）
set -euo pipefail
cd "$(dirname "$0")/.."
echo "=== douyin-reader setup ==="

REF="${MEDIACRAWLER_REF:-main}"
REPO="NanmiCoder/MediaCrawler"

# 1. 下载 MediaCrawler（目录存在即跳过；原判断条件 `-d MediaCrawler/main.py` 永远为假，会每次重下）
if [ -d MediaCrawler ]; then
    echo "[1/3] MediaCrawler/ 已存在，跳过下载"
else
    echo "[1/3] downloading MediaCrawler@${REF} ..."
    if [ "$REF" = "main" ]; then
        URL="https://codeload.github.com/${REPO}/zip/refs/heads/main"
    else
        URL="https://codeload.github.com/${REPO}/zip/${REF}"
    fi
    TMP="$(mktemp -d)"
    curl -fsSL -o "$TMP/mc.zip" "$URL"
    unzip -q "$TMP/mc.zip" -d "$TMP"
    SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d -name 'MediaCrawler-*' -print -quit)"
    if [ -z "$SRC" ]; then
        echo "解压后未找到 MediaCrawler 目录"; exit 1
    fi
    mv "$SRC" MediaCrawler
    rm -rf "$TMP"
    echo "$REF" > MediaCrawler/.douyin-reader-ref
    echo "       MediaCrawler@${REF} downloaded"
fi

# 2. 安装依赖（MediaCrawler 自带 uv.lock）
echo "[2/3] installing MediaCrawler dependencies..."
(cd MediaCrawler && uv sync && uv run playwright install chromium)

# 3. 不再对 MediaCrawler 源码打补丁：条数上限与跳过已下载视频由 scripts/mediacrawler_launcher.py 运行时完成
echo "[3/3] 无需补丁（运行时由 scripts/mediacrawler_launcher.py 处理）"

echo ""
echo "=== Setup complete ==="
echo "Next:"
echo "  1. 安装本项目依赖：python3 -m pip install -r requirements.txt"
echo "  2. 在 .env 中填写所需的 API key"
echo "  3. 启动 Web（仅本机）：python3 -m uvicorn web.app:app --host 127.0.0.1 --port 8000"
