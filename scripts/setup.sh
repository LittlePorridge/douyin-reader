#!/bin/bash
# 一键设置：下载 MediaCrawler + 打补丁
set -e
cd "$(dirname "$0")/.."
echo "=== douyin-reader setup ==="

# 1. 下载 MediaCrawler
if [ ! -d MediaCrawler/main.py ]; then
    echo "[1/3] downloading MediaCrawler..."
    curl -sL -o /tmp/mc.zip "https://codeload.github.com/NanmiCoder/MediaCrawler/zip/refs/heads/main"
    unzip -q -o /tmp/mc.zip -d /tmp/
    mv /tmp/MediaCrawler-main MediaCrawler
    rm /tmp/mc.zip
    echo "       MediaCrawler downloaded"
else
    echo "[1/3] MediaCrawler already exists, skip"
fi

# 2. 安装依赖
echo "[2/3] installing MediaCrawler dependencies..."
cd MediaCrawler
uv sync
uv run playwright install chromium
cd ..

# 3. 打补丁
echo "[3/3] applying patches..."
python3 scripts/patch_mediacrawler.py

echo ""
echo "=== Setup complete ==="
echo "Next:"
echo "  1. Edit .env with your API keys"
echo "  2. Start web: python -m uvicorn web.app:app --host 127.0.0.1 --port 8000"
echo "  3. Open http://127.0.0.1:8000"
