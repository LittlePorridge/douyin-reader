# 📺 抖音阅读器 (douyin-reader)

自动抓取抖音博主视频 → AI 语音转写 → LLM 生成摘要/要点/知识点 → 本地 Web 浏览。

---

## 👀 只想浏览内容？（3 分钟上手）

如果你只想看已生成的好内容，不需要自己抓取视频，按以下步骤操作：

### 第 1 步：安装 Python 依赖

```bash
git clone https://github.com/LittlePorridge/douyin-reader.git
cd douyin-reader
pip install fastapi uvicorn jinja2 pyyaml
```

### 第 2 步：初始化数据库 & 导入数据

```bash
# 创建数据库表
python3 -c "import sys; sys.path.insert(0,'.'); from src.config import load_config; from src.db import init_db; cfg=load_config(); init_db(cfg)"

# 导入预置数据（6 个博主、278 条视频、143 条摘要）
sqlite3 data/douyin-reader.db < seed/seed.sql
```

### 第 3 步：启动 Web

```bash
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000 ，即可浏览博主卡片 → 视频列表 → 视频详情（摘要、正文、要点、知识点、完整文字稿）。

> 不需要 MediaCrawler、不需要 API Key、不需要下载视频，纯浏览模式。

---

## ✨ 完整功能

- **博主管理**：添加博主、分类、备注，自动抓取头像和简介
- **视频抓取**：全量/部分抓取博主视频，支持增量去重
- **AI 转写**：本地 Whisper / Groq Whisper / 小米 MiMo ASR，可切换
- **LLM 摘要**：DeepSeek / GLM / 豆包 / Qwen 等多模型对比，可切换
- **Web 界面**：博主卡片首页、视频列表、详情页（摘要+正文+要点+知识点+文字稿）
- **批量操作**：Web 上选择视频批量转写/摘要/重置/删除
- **任务管理**：多任务并行、实时进度、一键停止
- **磁盘监控**：查看 mp4/wav/文字占用，一键清理

## 🚀 完整部署（抓取 + 转写 + 摘要）

### 1. 安装 MediaCrawler

```bash
# 下载 MediaCrawler
curl -sL -o /tmp/mc.zip "https://codeload.github.com/NanmiCoder/MediaCrawler/zip/refs/heads/main"
unzip -q -o /tmp/mc.zip -d /tmp/
mv /tmp/MediaCrawler-main MediaCrawler
rm /tmp/mc.zip

# 安装依赖
cd MediaCrawler && uv sync && uv run playwright install chromium && cd ..

# 打补丁（支持限制抓取数量 + 博主信息获取）
python3 scripts/patch_mediacrawler.py
```

### 2. 配置 API Key

在项目根目录创建 `.env` 文件：

```bash
DEEPSEEK_API_KEY=sk-你的deepseek-key
OPENCODE_GO_API_KEY=sk-你的opencode-go-key
MIMO_API_KEY=sk-你的mimo-key           # 可选，小米 ASR
VOLC_API_KEY=你的火山引擎-key            # 可选，火山引擎 LLM
GROQ_API_KEY=你的groq-key              # 可选，Groq Whisper
```

复制配置模板：

```bash
cp docs/llm_providers.yaml.example data/llm_providers.yaml
cp docs/asr_config.yaml.example data/asr_config.yaml
```

### 3. 导入种子数据（可选，同上）

```bash
python3 -c "import sys; sys.path.insert(0,'.'); from src.config import load_config; from src.db import init_db; cfg=load_config(); init_db(cfg)"
sqlite3 data/douyin-reader.db < seed/seed.sql
```

### 4. 启动 Web

```bash
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

### 5. 添加博主并跑流程

**Web 界面**：
1. 首页点「添加博主」，填入博主主页 URL
2. 进入博主详情页，点「跑流程」
3. 选择执行阶段（抓取/转写/摘要）、ASR 引擎、LLM 引擎
4. 点击开始，实时查看进度

**命令行**：

```bash
# 添加博主
python -m src.orchestrator add-creator --url "https://www.douyin.com/user/MS4w..." --nickname "博主名"

# 一键跑全流程（抓取→转写→摘要）
python -m src.orchestrator run --url "https://www.douyin.com/user/MS4w..."

# 只抓取 10 条先试水
python -m src.orchestrator run --url "..." --max-videos 10

# 只跑转写（用 MiMo ASR）
python -m src.orchestrator transcribe --batch 5

# 只跑摘要
python -m src.orchestrator summarize --yes

# 查看状态
python -m src.orchestrator status
```

## 📖 使用手册

详细操作指南见 [USAGE.md](USAGE.md)，包含：
- Web 界面操作说明
- 命令行完整参考
- LLM/ASR 配置方法
- 常见问题排查

## 🏗️ 架构

```
[MediaCrawler]  →  [importer]  →  [SQLite]  ←  [transcribe_worker]
   (爬虫)        (jsonl→db)    (主表 videos)      (ASR 转写)
                                                     ↓
                            [SQLite] ← [summarize_worker] ← llm_client
                                                     ↓
                            [SQLite] ← [FastAPI Web] ← 浏览器
```

### 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| 爬取 | MediaCrawler + Playwright | 抖音 Web 端爬虫 |
| ASR | faster-whisper / Groq / MiMo | 多 provider 可切换 |
| LLM | DeepSeek / GLM / 豆包 / Qwen 等 | 多 provider 可切换，OpenAI/Anthropic 兼容 |
| 存储 | SQLite | 轻量单文件 |
| Web | FastAPI + Jinja2 | 本地浏览 |
| 调度 | 手动 / Web 触发 | 多任务并行 |

### 视频处理状态机

```
new → downloaded → transcribing → transcribed → summarizing → done
                        ↓ 失败           ↓ 失败
                  transcribe_failed  summarize_failed
```

### 目录结构

```
douyin-reader/
├── README.md                # 本文件
├── USAGE.md                 # 使用手册
├── CHANGELOG.md             # 问题记录与修复日志
├── seed/seed.sql            # 种子数据（博主+视频+摘要）
├── docs/                    # 架构文档、配置模板
├── scripts/                 # 部署脚本、补丁、数据导出
├── src/                     # 后端代码
│   ├── orchestrator.py      # CLI 入口 + 编排器
│   ├── transcribe_worker.py # ASR 转写
│   ├── summarize_worker.py  # LLM 摘要
│   ├── llm_client.py        # LLM 抽象层
│   ├── asr_client.py        # ASR 抽象层
│   └── ...
└── web/                     # FastAPI Web 服务
```

## ⚠️ 免责声明

- 本项目仅供个人学习和研究使用
- 爬虫部分依赖 MediaCrawler 开源项目，遵守其 Non-Commercial Learning License
- 请遵守抖音平台使用条款，合理控制请求频率
- 不得用于商业用途或大规模爬取

## 📄 License

本项目代码遵循 MIT License。MediaCrawler 部分遵循其原始 License。