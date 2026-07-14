# 问题记录与修复日志

> 按时间倒序记录开发过程中遇到的问题、根因和修复方案，方便回顾和分享。

---

## 2026-07-14 MiMo ASR 5 条视频转写失败

**现象**：5 条视频转写失败，4 条返回 `exceeds maximum size of 10MB`，1 条 `write operation timed out`。

**根因**：
- MiMo API base64 请求体上限 10MB，16kHz mono WAV 每分钟约 1.9MB，超过 5 分钟的视频 WAV 就超限
- 压缩阈值设的 25MB（远超 10MB 限制），导致 11-12MB 的 WAV 没被压缩
- 网络上传 6.9MB 时 300s timeout 不够

**修复**：
- 压缩阈值从 25MB 降到 8MB
- PyAV 转 mp3 32kbps（11MB WAV → 1.3MB mp3）
- timeout 从 300s 提到 600s

---

## 2026-07-14 转写任务 `name 'os' is not defined`

**现象**：Web 点「跑流程」后立即报错 `error: name 'os' is not defined`，用时 0 秒。

**根因**：`web/app.py` 的 `_run_background_task` 函数里用了 `os.environ.copy()`，但文件顶部没有 `import os`。

**修复**：加 `import os`。

---

## 2026-07-14 MiMo ASR 20 条视频转写失败（第二轮）

**现象**：修好 env var 名后重跑，又有 20 条失败。错误分两类：
- 16 条：`No such file or directory: /var/folders/.../tmpXXX.mp3`
- 4 条：`mime type must be one of: audio/wav, audio/mpeg, audio/mp3. Got: audio/mp4`

**根因**：系统没有可用的 ffmpeg（conda 的 ffmpeg 缺 `libopenjp2.7.dylib`，homebrew 没装）。
- 16 条：MiMo 压缩逻辑调 ffmpeg 生成临时 mp3，ffmpeg 不存在 → 文件没创建 → 读取报错
- 4 条：音频抽取失败后直接把 mp4 喂给 MiMo → MiMo 不支持 mp4 音频输入

**修复**：
- 音频抽取：用 PyAV 替代 ffmpeg（`av.AudioResampler` 重采样为 16kHz mono WAV）
- MiMo 压缩：也用 PyAV 替代 ffmpeg
- ffmpeg 可选（优先用，不可用自动走 PyAV）

---

## 2026-07-14 MiMo ASR 30 条视频转写失败（第一轮）

**现象**：首次用 MiMo ASR 跑转写，30 条全部失败，错误统一为 `MIMO_API_KEY not set`。

**根因**：用户 `.env` 文件里变量名写的是 `MIMO_ASR_API_KEY`，代码配置里找的是 `MIMO_API_KEY`，名字不匹配。

**修复**：统一变量名为 `MIMO_API_KEY`。

**教训**：配置项的变量名要在文档和代码里保持一致，用户手填容易出错。

---

## 2026-07-14 任务卡在 summarizing 状态

**现象**：LLM 摘要阶段某条视频状态卡在 `summarizing`，进程一直等 HTTP 响应不返回。

**根因**：opencode-go 的 deepseek-v4-pro 是 reasoning 模型，思考时间长，120s timeout 不够。

**修复**：
- timeout 从 120s 提到 300s（后又提到 600s）
- Web 加「停止任务」按钮，可以手动终止卡住的任务
- 停止后需手动重置 `summarizing` → `transcribed`：`python -m src.orchestrator reset --aweme-id <id> --status transcribed`

---

## 2026-07-14 页面不停抖动 + 跑流程弹窗一闪即消失

**现象**：
1. 停止任务后页面持续刷新抖动
2. 点击「跑流程」弹窗出现后立即消失

**根因**：
1. `pollTask()` 在任务完成/停止后调 `setTimeout(() => location.reload(), 4000)`，刷新后 `pollTask` 再次检测到已完成状态又刷新 → 死循环
2. `submitRun()` 用了全局 `event.target`，inline onclick 不传 event 参数时 `event` 可能未定义 → JS 报错 → 弹窗被关闭

**修复**：
- 去掉所有 `setTimeout(() => location.reload())`，改为显示「刷新页面」按钮让用户手动刷新
- `onclick="submitRun(event)"` 显式传 event 参数，函数签名改为 `submitRun(e)`

---

## 2026-07-13 LLM 摘要 JSON 解析失败（glm-5.2 / deepseek-v4-pro）

**现象**：opencode-go 的 glm-5.2 和 deepseek-v4-pro 返回的 JSON 无法解析。

**根因**：
- `max_tokens=2000` 太小，reasoning 模型思考过程占满 token 额度，JSON 还没输出就被截断
- 模型输出前带了思考过程文字（如 "1. **理解目标**..."），不是纯 JSON

**修复**：
- `max_tokens` 提到 8000-12000
- 开启 `json_mode: true`（`response_format: {"type": "json_object"}`）
- prompt 末尾加「不要思考过程、不要分析步骤，直接输出 JSON」
- 容错解析：温度降到 0.1 重试 1 次

---

## 2026-07-13 opencode-go API 返回 403

**现象**：调用 opencode-go 的 API 返回 `HTTP 403: error code: 1010`。

**根因**：Python urllib 默认 User-Agent 是 `Python-urllib/3.x`，被 CloudFlare 拦截。

**修复**：请求头加 `User-Agent: douyin-reader/0.1 (macOS) Python-urllib`。

---

## 2026-07-13 importer 幂等检测失败

**现象**：重复跑 importer，每次都报 `new=20 existing=0`，应该是第二次 `new=0 existing=20`。

**根因**：SQLite 的 `INSERT ... ON CONFLICT DO UPDATE` 语句的 `cursor.rowcount` 始终返回 1（不管是新增还是更新），不能用来判断是否是新增。

**修复**：先 `SELECT 1 FROM videos WHERE aweme_id=?` 判断是否已存在，再决定 upsert 后返回 True/False。

---

## 2026-07-13 faster-whisper 模型下载失败

**现象**：首次跑 ASR 报 `LocalEntryNotFoundError`，HuggingFace 无法访问。

**根因**：`huggingface.co` 在国内网络不通；`hf-mirror.com` 镜像没有 `large-v3-turbo` 模型。

**修复**：
- 换用 `large-v3`（镜像有）
- 环境变量 `HF_ENDPOINT=https://hf-mirror.com`

---

## 2026-07-13 Whisper 模型下载磁盘空间不足

**现象**：下载 large-v3 模型报 `No space left on device`，磁盘只剩 126MB。

**根因**：根分区 460G 已用 100%。

**修复**：用户手动清理磁盘到 ≥5G 后继续。

---

## 2026-07-13 MediaCrawler git clone 超时

**现象**：`git clone` MediaCrawler 仓库一直超时。

**根因**：网络问题，git clone 大仓库慢。

**修复**：改用 `curl` 下载 zip 包 + `ditto` 解压（macOS 自带，比 unzip 更可靠处理中文路径）。

---

## 2026-07-13 解压中文目录名乱码

**现象**：`unzip` 解压 MediaCrawler 时报 `write error (disk full?)`，目录名含中文乱码。

**根因**：macOS 的 `unzip` 对中文路径处理有问题。

**修复**：用 macOS 自带的 `ditto -x -k` 替代 `unzip`。