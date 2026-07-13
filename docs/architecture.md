# 架构设计

> 配套 CLAUDE.md（项目规范），本文是架构实现细节。

## 一、整体结构

```
[MediaCrawler]  →  [importer]  →  [SQLite]  ←  [transcribe_worker]
   (外部)        (jsonl→db)    (主表 videos)        (faster-whisper)
                                                       ↓
                            [SQLite] ← [summarize_worker] ← llm_client
                                                       ↓
                            [SQLite] ← [FastAPI web] ← 浏览器
                ↑
         [orchestrator.py 串起一次运行]
```

## 二、模块接口

### 2.1 编排器 `src/orchestrator.py`

单进程入口，按序执行整个 pipeline。CLI 子命令：

```bash
# 一次完整运行：抓取 → 导入 → 处理 web → 收尾
python -m src.orchestrator run --sec-user-id <id> [--max-new-videos N] [--no-crawl]

# 只跑处理 web（不抓新列表，处理已入库但未完成的视频）
python -m src.orchestrator process

# 只抓+导入新列表（不跑 ASR/LLM）
python -m src.orchestrator crawl --sec-user-id <id>

# 添加博主
python -m src.orchestrator add-creator --homepage-url <url> [--nickname <name>]

# 手动重置某条视频状态（恢复 manual_failed 等）
python -m src.orchestrator reset --aweme-id <id> --status <target_status>
```

执行流程（`run` 子命令）：

```
1. 读 creators 表 → 找到目标博主
2. crawler_runner.run(creator) → 产出 jsonl+mp4
3. importer.import_latest(creator) → upsert videos
4. loop:
   - transcribe_worker.tick() 处理 1 条 status=downloaded 的视频
   - summarize_worker.tick() 处理 1 条 status=transcribed 的视频
   - 若两边都无 pending，退出
5. 写 crawl_runs 记录，输出本次报告
```

### 2.2 抓取层 `src/crawler_runner.py`

```python
def run(creator: Creator) -> Path:
    """调 MediaCrawler 跑 creator 模式，返回产出的 jsonl 路径"""
    # 1. 在 MediaCrawler 目录下生成动态 config (避免改原文件)
    # 2. subprocess: uv run main.py --platform dy --type creator --lt cookie
    # 3. 找最新的 data/douyin/jsonl/creator_contents_*.jsonl 文件
```

**细节**：
- 不修改 `MediaCrawler/config/` 原文件，临时配置写到 `data/generated/dy_config_override.py`，靠 `PYTHONPATH` 注入或临时复制替换（先用 tempdir 复制替换最简单）
- 登录态复用 `MediaCrawler/browser_data/dy_user_data_dir`，已建立就不再扫码
- 增量抓取：MediaCrawler 本身不支持，但全量列表请求只要 2 分钟，bootstrap 后每次跑全量即可
- 限制条数：通过 `--max-new-videos N` 让 importer 提前停（不限制 MediaCrawler 抓列表，只限制 importer 把多少条 new 入库）

### 2.3 导入层 `src/importer.py`

```python
def import_latest(creator: Creator, jsonl_path: Path, max_new: int = 0) -> ImportResult:
    """读 jsonl 文件，upsert 到 videos 表
    - max_new > 0 时只允许新增 max_new 条 status=new 的视频
    - 已存在的 aweme_id 不更新 status，但更新 stats（点赞评论数会变）
    - 返回 import_result{new_count, existing_count}
    """
```

每条 jsonl 记录字段（MediaCrawler 产出）：
- `aweme_id` (主键候选)
- `title`, `desc`, `create_time` (unix 秒)
- `cover_url`, `video_download_url`, `aweme_url`
- `liked_count`, `collected_count`, `comment_count`, `share_count`
- 移除：`creator_hash`、`nickname`（脱敏字段，不要）

`status` 初始值：先置 `new`，由 transcribe_worker 启动时检查 mp4 落地后改 `downloaded`。
为什么不导入时直接判 `downloaded`？因为导入阶段是「我知道有这个视频」，下载阶段是「文件确实可用」。后面可能有 mp4 损坏/被删的情况，下载阶段严格判定更安全。

### 2.4 ASR Worker `src/transcribe_worker.py`

```python
def tick(db: Session, max_process: int = 5) -> int:
    """处理一批 status=downloaded 的视频，返回处理条数"""

def transcribe_one(video: Video) -> None:
    """处理单条视频：
    1. status → transcribing (原子操作，避免重入)
    2. ffmpeg 抽音频: mp4 → wav (16kHz mono pcm_s16le)
    3. faster-whisper large-v3-turbo 转写
    4. 文字稿带时间戳写到 data/text/<aweme_id>.txt
    5. status → transcribed
    6. 失败 → status=transcribe_failed, retry_count += 1
    """
```

**音频抽取命令**：
```bash
ffmpeg -i data/douyin/videos/<aweme_id>/video.mp4 \
       -vn -acodec pcm_s16le -ar 16000 -ac 1 \
       data/audio/<aweme_id>.wav
```

**文字稿格式**（written text 文件示例）：
```
[00:00:00.000 --> 00:00:03.200] 今天讲个学习方法
[00:00:03.200 --> 00:00:07.500] 第一点是持续输出
...
```

**ASR 模型管理**：
- 通过 HuggingFace cache 自动下载，模型 repo `Systran/faster-whisper-large-v3-turbo`
- 首次运行下载约 1.5G，缓存到 `~/.cache/huggingface/hub/`
- `device="cpu"`, `compute_type="int8"`（Mac M1+ 可调 `compute_type="int8_float16"` 加速）

### 2.5 Summarize Worker `src/summarize_worker.py`

```python
def tick(db: Session, max_process: int = 5) -> int:
    """处理一批 status=transcribed 的视频"""

def summarize_one(video: Video) -> None:
    """1. status → summarizing (原子)
       2. 读 transcript 文字稿
       3. prompt 构造
       4. llm_client.chat(prompt) → 期望 JSON 输出
       5. 校验 JSON，失败重试 1 次（temperature 调低）
       6. 写回 videos.summary, key_points, knowledge_points (JSON string)
       7. 写 data/summaries/<aweme_id>.md (markdown 展示版)
       8. status → done
       失败 → status=summarize_failed
    """
```

**Prompt 模板**（固定 system）：
```
你是一个学习方法与知识体系的拆解专家，擅长从短视频文字稿中提炼结构化内容。
请基于以下视频文字稿输出严格的 JSON，schema 如下：

{
  "summary": "200-300 字总结，描述视频主旨和核心论点",
  "key_points": [
    "要点1（一句话概括）",
    "要点2",
    ...3-5 条
  ],
  "knowledge_points": [
    {
      "topic": "知识点名称",
      "why": "为什么值得深入探究（1-2 句）",
      "direction": "深入方向建议（如读哪本书、学哪个概念）"
    }
  ]
}

只输出 JSON，不要其他文字，不要 markdown 围栏。
```

**校验逻辑**：
- 用 `json.loads` 解析
- 必须包含 `summary` (str), `key_points` (list[str]), `knowledge_points` (list[dict])
- `key_points` 至少 2 条
- 校验失败：温度降到 0.1 重试 1 次，仍失败则 `summarize_failed`

**Markdown 摘要文件格式**（`data/summaries/<aweme_id>.md`）：
```markdown
# <视频标题>

> <发布时间> · <点赞数> · <原视频链接>

## 摘要

<summary>

## 要点

- <key_points[0]>
- <key_points[1]>

## 值得探究的知识点

### 1. <knowledge_points[0].topic>
**为什么**：<why>
**深入方向**：<direction>
```

### 2.6 LLM 抽象 `src/llm_client.py`

```python
class LLMProvider(Protocol):
    name: str
    def chat(self, messages: list[dict]) -> str: ...

class OpenAICompatibleProvider:
    """OpenAI /v1/chat/completions 协议客户端"""
    def __init__(self, name, api_base, api_key, model, temperature, max_tokens): ...
    def chat(self, messages) -> str: ...

def get_provider(config_path: Path) -> LLMProvider:
    """从 yaml 读取 active_provider，返回对应实例"""
```

**Provider 配置**（`data/llm_providers.yaml`，不入 git）：
```yaml
active_provider: deepseek_chat

providers:
  deepseek_chat:
    api_base: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY     # 从环境变量读，避免硬编码
    model: deepseek-chat
    temperature: 0.3
    max_tokens: 2000

  opencode_go:
    api_base: <TBD>                    # 待用户确认
    api_key_env: OPENCODE_GO_API_KEY
    model: <TBD>
    temperature: 0.3
    max_tokens: 2000
```

切换：改 `active_provider` 字段即可，无需改代码。

### 2.7 配置 `src/config.py`

```python
@dataclass
class Config:
    project_root: Path          # douyin-reader/
    data_dir: Path              # data/
    mediacrawler_dir: Path      # MediaCrawler/
    db_path: Path               # data/douyin-reader.db
    llm_providers_path: Path    # data/llm_providers.yaml
    
    audio_dir: Path
    text_dir: Path
    summary_dir: Path

def load_config() -> Config: ...
```

按环境变量覆盖默认值，不传给 main 时用默认。

### 2.8 数据库 `src/db.py`

- 用 `sqlite3` 标准库即可，不上 SQLAlchemy（数据量小，依赖越少越好）
- 包装 `contextmanager` 提供 connection
- 启动时调用 `src/db.py init_schema()` 跑 `docs/schema.sql`

### 2.9 Web `web/app.py`

FastAPI 简单只读：

```python
@app.get("/") -> list_view (creator filter, status filter, 按时间倒序)
@app.get("/video/{aweme_id}") -> detail_view
@app.get("/static/*") -> 静态资源
```

Jinja2 模板：列表 + 详情两页，朴素 HTML + 少量 CSS，不上前端框架。
反代到 `localhost:8000`，本机用即可。

## 三、数据流（一次端到端）

```
T0  run --sec-user-id MS4w...
T0+0s    crawler_runner.run()                # 跑 MediaCrawler
T0+120s  ↓ 产出最新 jsonl + N 个 mp4
T0+121s  importer.import_latest()            # upsert 到 SQLite
T0+122s  ↓ 大部分已存在，少量新条数 status=new
T0+123s  transcribe_worker.tick() 循环        # 选 status=new 的开始处理（先确认 mp4 落地改 downloaded → transcribing）
T0+124s  video1: mp4→wav→whisper→txt
T0+180s  ↓ status=transcribed
T0+181s  summarize_one(video1)                # txt → LLM → JSON → 落库 + md
T0+183s  ↓ status=done
T0+184s  transcribe_worker.tick() 继续 video2
...
T_end    crawl_runs 写入，输出报告 + 新增 X 条/处理完 Y 条/失败 Z 条
```

## 四、并发模型

**单进程串行**，不做并发。理由：
- ASR on CPU 已是瓶颈（占满核心），并发会互相抢
- LLM 调用虽然 IO bound 但单视频 token 量小，串行延迟可接受
- 避免引入线程/进程协调复杂度

唯一并发机会：在 ASR 跑的同时让 LLM 处理上一条已转写完的视频。
MVP 阶段不做，单条单条串行处理。后期需要再写「双 worker 进程间靠 SQLite 协调」。

## 五、调度

**MVP 阶段手动跑**：`python -m src.orchestrator run --sec-user-id <id>`

后期：Mac 上 `launchd` 或 cron 周期触发，每晚 1 点处理。需要锁文件防止重入。

## 六、跨平台（非 MVP 目标，预留思考）

**A 方案（推荐）**：把 `data/text + data/summaries + data/douyin-reader.db` 压缩包发给 Windows 用户，他们本地跑同一份 `web/` 代码 + SQLite，只读浏览。

**B 方案**：FastAPI 部署到内网，SQLite 同步到云。多用户共享。增加鉴权、网络可视化层等，复杂度高。

MVP 完成后再选。

## 七、风险与不做

不做：
- 不做反爬绕过升级（那不是个人用户的事，社区出了再 sync MediaCrawler）
- 不做消息队列、分布式 Worker（单机够用 2 倍以上的规模）
- 不做用户系统、多租户（个人独享）
- 不做实时推送（cron 周期足够）
- 不做 mp4 的转码/水印移除（MediaCrawler 已是无水印原视频）

风险：
- MediaCrawler 失效 → 前端 import 层接口不变，换 downloader 即可
- Faster-whisper 个别视频 OOM → 加 fallback 到 medium 模型 或 跳过标 failed
- LLM JSON 不规范 → 双重保险（temperature 降 +1 次重试，仍失败标 failed）