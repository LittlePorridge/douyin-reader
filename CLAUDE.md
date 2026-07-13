# CLAUDE.md - douyin-reader 项目规范

> 约束先行：先定规则，再动代码。本文件是项目的最高约束。

## 项目概述

个人知识管理工具：自动抓取抖音博主视频，将视频转写为文字，调用 LLM 生成摘要、要点、值得深入的知识点，最终通过本地 Web 浏览。
- 非生产级系统，单机使用
- 但要求高可观测性和可恢复性：任何一步失败后下次能从断点继续，不重复烧模型/不重复花钱

## 红线（必须先问）

- 删除文件、目录或 git 历史
- 修改 `.env`、密钥、token、LLM API key
- git push / rebase / reset --hard
- 调用 LLM 在测试环境之外做大批量调用（>10 条/次）前先确认算钱量级
- 修改 `MediaCrawler/` 内部代码（外部依赖，不耦合，应保持只读）

## 设计原则

1. **MediaCrawler 是外部不稳组件**：我们的代码只认它产出的 `jsonl` + `mp4` 文件，不直接 import 它，写成子进程调用
2. **状态机驱动**：每个视频是有状态工件，状态保存在 SQLite，任何时候可从断点继续
3. **所有处理步骤幂等**：靠 `aweme_id` 主键去重；重复运行不会重复下载/转写/调用 LLM
4. **单编排器串行化**：一次运行由 `orchestrator.py` 按序驱动各阶段，不存在多进程争抢任务（单机够用，无需 Redis/队列）
5. **LLM 可插拔**：通过配置文件切换提供商，所有提供商必须 OpenAI-compatible HTTP 接口
6. **可观测优先**：每一步打结构化日志，关键决策（启动哪个 worker、选了哪条视频）都可见

## 目录约定

```
douyin-reader/
├── CLAUDE.md                       # 项目规范（本文件）
├── docs/
│   ├── architecture.md             # 详细架构
│   ├── schema.sql                  # SQLite 建表语句
│   └── llm_providers.yaml.example  # LLM provider 配置模板
├── MediaCrawler/                   # 外部爬虫依赖（只读，不动）
├── data/                           # 所有产出数据（gitignore）
│   ├── douyin/                     # MediaCrawler 原始产出
│   │   ├── jsonl/
│   │   └── videos/<aweme_id>/video.mp4
│   ├── audio/<aweme_id>.wav        # 抽出的音频
│   ├── text/<aweme_id>.txt         # ASR 文字稿（带时间戳）
│   ├── summaries/<aweme_id>.md    # LLM 摘要 markdown
│   └── douyin-reader.db            # SQLite
├── src/
│   ├── orchestrator.py             # 编排器入口
│   ├── crawler_runner.py           # 调用 MediaCrawler
│   ├── importer.py                 # jsonl → SQLite
│   ├── transcribe_worker.py        # ASR worker
│   ├── summarize_worker.py         # LLM 摘要 worker
│   ├── llm_client.py               # LLM 抽象层
│   ├── db.py                       # SQLite 连接/初始化
│   ├── schema.sql -> ../docs/schema.sql (软链或复制)
│   └── config.py                   # 全局配置
├── web/
│   ├── app.py                      # FastAPI
│   └── templates/
├── scripts/
│   └──CLAUDE.md helper if needed
└── .gitignore                      # 排除 data/, MediaCrawler/browser_data/, *.db
```

## 命名约定

- Python 模块/函数：`snake_case`
- SQLite 表名：`snake_case` 单数
- 数据文件：用 `aweme_id` 当文件名（无扩展名的纯数字字符串）
- 配置 yaml：`snake_case`
- LLM provider 名：`snake_case`，在 `llm_providers.yaml` 注册

## 状态机（核心字段 `videos.status`）

```
                    ┌─────────── 失败重试限制 (3次) 后转 manual_failed ───────┐
                    │                                                  │
new ──→ downloaded ──→ transcribing ──→ transcribed ──→ summarizing ──→ done
            ↑          │ transcribe_failed  │ summarize_failed    │
            │          └────────────────────┘                     │
            └──────────────────────────────────────────────────────┘
                       (下次编排周期从 failed 状态重试)
```

- `new`：元数据导入但未确认 mp4 是否落地（importer 已写入但 mp4 检查未做）
- `downloaded`：mp4 已确认存在
- `transcribing`：ASR 进行中（理论瞬时态，正常退出转 transcribed，失败转 transcribe_failed）
- `transcribed`：文字稿已落盘
- `summarizing`：LLM 进行中
- `done`：摘要完成
- `transcribe_failed` / `summarize_failed`：失败，记录 `status_message`，下次重试
- `manual_failed`：重试满 3 次，等人工介入

## 增量抓取策略

不增量抓取，每次跑全量列表。SQL upsert 自动去重，已存在的视频状态不重置。
只有 `status='new'` 的视频会触发后续下载确认 → ASR → 摘要链路。

## 失败处理策略

- ASR 失败：状态 `transcribe_failed`，下次编排周期重试
- LLM 失败：状态 `summarize_failed`，下次重试
- 每个 `aweme_id` 维护 `retry_count` 字段，达到 3 次硬封 `manual_failed`
- 失败原因必须写到 `status_message` 字段，详情进日志

## LLM 提供商配置

通过 `data/llm_providers.yaml` 切换，`active_provider` 指定当前使用哪个。
首次开发支持两个：`deepseek_chat`（直接可用）、`opencode_go`（接入方式待用户确认）。

任何 provider 必须 OpenAI-compatible（`/v1/chat/completions` 协议），
否则需在 `llm_client.py` 加适配层。

## 验证命令

```bash
# Lint
ruff check src/ web/
ruff format --check src/ web/

# Type check（可选，MVP 阶段可不开）
# pyright src/

# 单元测试
pytest tests/

# 端到端手动验证（用 1 个视频跑全链路）
python -m src.orchestrator run-once --sec-user-id <id> --max-new-videos 1
```

## 当前阶段

- 状态：MVP 开发前，已验证 MediaCrawler 抓取 + 下载链路可跑通
- 下一步：根据 architecture.md 实现 importer + transcribe_worker + summarize_worker + web

## 待定问题（开发前需要回答）

1. opencode 的 Go 套餐接入方式：直接 HTTP（OpenAI-compatible endpoint）还是只能 CLI 调用？
   - 若是 HTTP：填 yaml 即可
   - 若是 CLI：需要在 `llm_client.py` 增加 CLI provider 适配
2. ASR 模型文件大小约 1.5G，是否需要预下载到特定路径管理？
3. 跨平台分享给 Windows 用户的方案（先不做，但要预留）：
   - A 方案：打包 `data/text + data/summaries + data/douyin-reader.db` 给用户本地跑 FastAPI（最简单）
   - B 方案：FastAPI 部署到内网云，SQLite 定期推到云（多用户共享，需独立开发）

## 开发流程

改实践一定先改/补 CLAUDE.md 或 architecture.md，再写代码。新增模块或不一致行为时尤其重要。

新增博主流程：
1. 在 SQLite `creators` 表插入一条记录（手动或后续做 CLI 工具）
2. 跑 `python -m src.orchestrator run --sec-user-id <id> --full`
3. 系统自动抓全量 → 新视频走状态机 → 完成后 web 可见