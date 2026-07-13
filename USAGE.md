# 抖音阅读器 - 使用手册

> 自动抓取抖音博主视频 → AI 转写文字 → LLM 生成摘要/要点/知识点 → 本地 Web 浏览

## 目录

- [快速开始](#快速开始)
- [Web 界面操作](#web-界面操作)
- [命令行操作](#命令行操作)
- [LLM 配置](#llm-配置)
- [常见问题](#常见问题)
- [项目结构](#项目结构)

---

## 快速开始

### 1. 环境准备

```bash
# 确认已安装
python3 --version    # >= 3.11
node --version       # >= 16
uv --version         # Python 包管理

# 进入项目目录
cd douyin-reader
```

### 2. 配置 LLM API Key

在项目根目录创建 `.env` 文件：

```bash
DEEPSEEK_API_KEY=sk-你的deepseek-key
OPENCODE_GO_API_KEY=sk-你的opencode-go-key
```

### 3. 启动 Web 界面

```bash
python -m uvicorn web.app:app --host 127.0.0.1 --port 8000
```

浏览器打开 http://127.0.0.1:8000

### 4. 添加博主并跑全流程

**方式 A：Web 界面（推荐）**
1. 点击右上角「添加博主」
2. 填入博主主页 URL、昵称、类型、备注
3. 进入博主详情页，点击「跑流程」

**方式 B：命令行**
```bash
python -m src.orchestrator run --url "https://www.douyin.com/user/MS4w..." --nickname "清华学霸"
```

首次使用需要扫码登录抖音（程序会自动弹出浏览器）。

---

## Web 界面操作

### 首页 `/`

- 博主卡片网格：头像、昵称、类型标签、备注标签、简介
- 每张卡片显示视频总数、已完成数、待处理数、进度条
- 顶部按钮：
  - **添加博主**：弹出表单，填 URL + 昵称 + 类型 + 备注
  - **全部跑**：依次跑所有已添加博主的全流程
  - **重新认证**：清除抖音登录态（下次需重新扫码）

### 博主详情页 `/creator/<id>`

- 博主信息栏：头像、昵称、类型、备注、简介、抖音主页链接
- 操作按钮：
  - **跑流程**：弹出选项窗口，可选择：
    - 执行阶段：抓取 / ASR 转写 / LLM 摘要（勾选框，可任意组合）
    - 抓取数量：0=全部，或指定条数
    - 登录方式：复用登录态 / 扫码登录
    - 转写上限 / 摘要上限：防止一次处理太多
  - **编辑**：修改昵称、类型、备注
  - **刷新信息**：重新从抖音获取头像和简介
- 状态筛选：点击状态标签筛选视频
- 视频列表：标题、摘要预览、点赞数、视频时长、ASR 耗时、LLM 耗时

### 视频详情页 `/video/<id>`

- 视频元信息：发布日期、点赞、原视频链接
- 耗时展示：⏱ 视频时长 · 🎙 ASR 转写耗时 · ✍️ LLM 写作耗时
- LLM 版本切换条：点击不同 provider 切换查看（★ 为当前默认版本）
- 内容展示：
  - **摘要**：200-300 字总结
  - **正文**：800-1500 字完整文章
  - **要点**：3-5 条核心要点
  - **值得探究的知识点**：1-3 个深入方向建议
  - **完整文字稿**：带时间戳的 ASR 原文（可折叠）

---

## 命令行操作

### 添加博主

```bash
python -m src.orchestrator add-creator \
  --url "https://www.douyin.com/user/MS4w..." \
  --nickname "清华学霸"
```

### 一键跑单个博主（抓取→转写→摘要）

```bash
# 用 URL（自动解析 sec_user_id + 添加博主）
python -m src.orchestrator run --url "https://www.douyin.com/user/MS4w..."

# 只抓 10 条视频（先跑通流程）
python -m src.orchestrator run --url "..." --max-videos 10

# 只跑抓取+转写，不跑摘要（先看文字稿）
python -m src.orchestrator run --url "..." --stages crawl,transcribe

# 只跑摘要（已转写的视频批量生成摘要）
python -m src.orchestrator run --url "..." --stages summarize

# 首次或登录态过期时扫码登录
python -m src.orchestrator run --url "..." --login-type qrcode
```

### 分阶段独立操作

```bash
# 只抓取+下载视频
python -m src.orchestrator crawl --sec-user-id MS4w... --max-videos 20

# 只跑 ASR 转写（默认上限 20 条，避免 CPU 长时间占满）
python -m src.orchestrator transcribe --batch 5

# 只跑 LLM 摘要（默认上限 10 条，避免一次烧太多钱）
python -m src.orchestrator summarize --yes

# 查看全局状态
python -m src.orchestrator status
```

### 跑所有已添加的博主

```bash
python -m src.orchestrator run-all --max-videos 10
```

### 管理博主

```bash
# 列出所有博主
python -m src.orchestrator list-creators

# 编辑博主类型/备注
python -m src.orchestrator edit-creator --sec-user-id MS4w... --category "学习方法" --note "重点跟踪"

# 刷新博主头像和简介
python -m src.orchestrator refresh-info --sec-user-id MS4w...

# 清除抖音登录态（下次需重新扫码）
python -m src.orchestrator reauth
```

### 重置视频状态

```bash
# 把失败的视频重置为待处理
python -m src.orchestrator reset --aweme-id 7636... --status downloaded
```

---

## LLM 配置

配置文件：`data/llm_providers.yaml`

### 切换默认 LLM

改 `active_provider` 字段即可：

```yaml
active_provider: opencode_go_deepseek_v4_pro  # 默认展示这个版本
```

### 可用 Provider

| Provider 名 | 模型 | 协议 | 说明 |
|-------------|------|------|------|
| `deepseek_chat` | deepseek-chat | OpenAI | DeepSeek 官方，速度快 |
| `opencode_go_glm52` | glm-5.2 | OpenAI | 智谱 GLM，via opencode |
| `opencode_go_deepseek_v4_pro` | deepseek-v4-pro | OpenAI | DeepSeek V4 Pro，文章最长 |
| `opencode_go_mimo_v25_pro` | mimo-v2.5-pro | OpenAI | 小米 MiMo |
| `opencode_go_minimax_m3` | minimax-m3 | Anthropic | MiniMax |
| `opencode_go_qwen37_max` | qwen3.7-max | Anthropic | 通义千问 |

### 对单个视频跑所有 LLM 对比

```bash
python -m src.summarize_worker <aweme_id> --all-providers
python -m src.summarize_worker <aweme_id> --all-providers --force  # 强制重跑
```

在 Web 视频详情页可通过版本切换条对比不同 LLM 的输出。

### 添加新 Provider

在 `data/llm_providers.yaml` 的 `providers` 下添加：

```yaml
  my_new_provider:
    api_base: https://api.example.com/v1
    api_key_env: MY_API_KEY          # 环境变量名
    model: my-model
    temperature: 0.3
    max_tokens: 8000
    json_mode: true                   # 支持 JSON mode 则开
    protocol: openai                  # openai 或 anthropic
```

在 `.env` 里加对应的环境变量即可。

---

## 常见问题

### Q: 首次跑流程弹出浏览器要扫码？

正常。MediaCrawler 需要登录抖音账号。扫码后登录态缓存到 `MediaCrawler/browser_data/`，下次免登录。

### Q: 登录态过期了怎么办？

**Web**：点击首页右上角「重新认证」，然后跑流程时选择扫码登录。
**CLI**：
```bash
python -m src.orchestrator reauth
python -m src.orchestrator run --url "..." --login-type qrcode
```

### Q: ASR 转写很慢？

首次使用需下载 Whisper large-v3 模型（~3G），下载后缓存。单条 3 分钟视频转写约 1-3 分钟（CPU）。

如需加速：
- 安装 ffmpeg：`brew install ffmpeg`（Mac），可大幅加速音频提取
- 降低模型：改 `src/transcribe_worker.py` 中 `model_size` 为 `"medium"` 或 `"small"`

### Q: 某个 LLM 摘要失败？

- 查看视频详情页的状态标签
- 用 CLI 重置：`python -m src.orchestrator reset --aweme-id <id> --status transcribed`
- 重新跑：`python -m src.orchestrator process`

### Q: 怎么加多个博主？

**Web**：首页点「添加博主」重复添加即可。
**CLI**：
```bash
python -m src.orchestrator add-creator --url "博主A..." --nickname "A"
python -m src.orchestrator add-creator --url "博主B..." --nickname "B"
python -m src.orchestrator run-all  # 一键跑全部
```

### Q: 一次性太多视频操作会出问题吗？

系统内置了批次限制保护：

| 阶段 | 默认上限 | 原因 | 调整方式 |
|------|---------|------|---------|
| 抓取 | 0（全量） | 列表翻页快，2分钟可抓200条 | `--max-videos N` |
| ASR 转写 | 20 条/次 | CPU 密集，每条 1-10 分钟 | `--transcribe-limit N` |
| LLM 摘要 | 10 条/次 | API 费用，多 provider 时 ×6 倍 | `--summarize-limit N` |

**断点续跑**：状态机保证中断后下次继续。已转写的不会重复转写，已摘要的不会重复调用 LLM。Web 上可随时点「停止任务」中断，已处理的数据保留。

**推荐流程**：
1. 首次：`run --max-videos 5` 先跑通 5 条全链路
2. 验证 OK 后：`run --stages crawl` 抓全量
3. 分批转写：`transcribe --limit 20`（可多次跑）
4. 分批摘要：`summarize --limit 10`（可多次跑）

### Q: 任务卡住了怎么办？

Web 界面任务进度条上有「⏹ 停止任务」按钮，点击即可终止。停止后：
- 已转写的视频保留，不会重复转写
- 已摘要的视频保留，不会重复花钱
- 卡在 `transcribing` / `summarizing` 的视频需要手动重置：
```bash
python -m src.orchestrator reset --aweme-id <id> --status transcribed
```

### Q: 视频文件占磁盘太大？

mp4 在 `MediaCrawler/data/douyin/videos/` 下。转写完成后可安全删除 mp4（文字稿已保存在 `data/text/`）：

```bash
# 谨慎操作：删除所有已完成视频的 mp4
sqlite3 data/douyin-reader.db "SELECT video_path FROM videos WHERE status='done'" | xargs rm -f
```

### Q: 抖音平台字幕能直接获取吗？

目前通过 MediaCrawler 检查，部分视频有 `is_subtitled=1` 字段，但大多数博主（包括测试博主）没有平台 CC 字幕。系统统一走 ASR 转写，保证一致性。

### Q: 如何分享给 Windows 用户？

**A 方案（推荐）**：打包数据给对方本地跑
1. 复制 `data/text/`、`data/summaries/`、`data/douyin-reader.db` 到对方机器
2. 对方安装 Python 依赖后只跑 Web：`python -m uvicorn web.app:app`

**B 方案**：部署到内网服务器（需额外开发）

---

## 项目结构

```
douyin-reader/
├── CLAUDE.md                # 项目规范
├── USAGE.md                 # 本手册
├── .env                     # API Key 配置（不入 git）
├── docs/
│   ├── architecture.md      # 架构设计
│   ├── schema.sql           # 数据库建表
│   └── llm_providers.yaml.example  # LLM 配置模板
├── MediaCrawler/            # 抖音爬虫（外部依赖，不修改）
├── data/                    # 所有产出数据（不入 git）
│   ├── douyin/jsonl/        # MediaCrawler 抓取的元数据
│   ├── douyin/videos/       # 下载的 mp4
│   ├── text/                # ASR 文字稿
│   ├── summaries/           # LLM 摘要 markdown
│   ├── douyin-reader.db     # SQLite 数据库
│   └── llm_providers.yaml   # LLM 配置
├── src/
│   ├── orchestrator.py      # 编排器（CLI 入口）
│   ├── importer.py          # jsonl → SQLite
│   ├── transcribe_worker.py # ASR 转写（faster-whisper）
│   ├── summarize_worker.py  # LLM 摘要
│   ├── llm_client.py        # LLM 抽象层
│   ├── db.py                # SQLite 操作
│   └── config.py            # 全局配置
├── web/
│   ├── app.py               # FastAPI Web 服务
│   └── templates/           # Jinja2 模板
│       ├── home.html        # 首页（博主卡片）
│       ├── creator.html     # 博主详情
│       └── detail.html      # 视频详情
└── scripts/
    └── run_creator.py       # 一键脚本
```

### 视频处理状态机

```
new → downloaded → transcribing → transcribed → summarizing → done
                        ↓ 失败           ↓ 失败
                  transcribe_failed  summarize_failed
```

- `new`：元数据已入库
- `downloaded`：mp4 确认存在
- `transcribed`：文字稿已生成
- `done`：摘要已生成
- `*_failed`：失败，下次自动重试

---

## 技术栈

| 组件 | 技术 | 说明 |
|------|------|------|
| 爬取 | MediaCrawler + Playwright | 抖音 Web 端爬虫 |
| ASR | faster-whisper large-v3 | 本地语音转文字 |
| LLM | DeepSeek / GLM / Qwen 等 | 多 provider 可切换 |
| 存储 | SQLite | 轻量单文件 |
| Web | FastAPI + Jinja2 | 本地浏览 |
| 调度 | 手动 / Web 触发 | MVP 阶段不做自动调度 |