-- douyin-reader SQLite schema
-- 配套 docs/architecture.md

CREATE TABLE IF NOT EXISTS creators (
    sec_user_id      TEXT PRIMARY KEY,
    nickname         TEXT,
    homepage_url     TEXT NOT NULL,
    avatar_url       TEXT,
    intro            TEXT,                           -- 博主简介/签名
    category         TEXT,                           -- 类型：学习方法/科技/财经/...
    note             TEXT,                           -- 用户备注
    first_seen_at    INTEGER NOT NULL,
    last_crawled_at  INTEGER,
    crawl_interval_hours INTEGER DEFAULT 168,
    enabled          INTEGER DEFAULT 1,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    aweme_id        TEXT PRIMARY KEY,
    sec_user_id     TEXT NOT NULL,
    title           TEXT NOT NULL,
    desc_text       TEXT,
    create_time     INTEGER NOT NULL,            -- unix seconds
    aweme_url       TEXT,
    cover_url       TEXT,
    video_download_url TEXT,

    liked_count     INTEGER DEFAULT 0,
    collected_count INTEGER DEFAULT 0,
    comment_count   INTEGER DEFAULT 0,
    share_count     INTEGER DEFAULT 0,

    -- 状态机
    -- new | downloaded | transcribing | transcribed | summarizing | done
    -- transcribe_failed | summarize_failed | manual_failed
    status          TEXT NOT NULL DEFAULT 'new',
    status_message  TEXT,
    retry_count     INTEGER DEFAULT 0,

    -- 文件路径（相对 data_dir）
    video_path      TEXT,
    audio_path      TEXT,
    transcript_path TEXT,
    summary_path    TEXT,

    -- LLM 处理结果
    summary         TEXT,
    key_points      TEXT,                        -- JSON array string
    knowledge_points TEXT,                       -- JSON array string
    llm_provider    TEXT,
    llm_model       TEXT,
    
    -- 处理时间戳
    downloaded_at       INTEGER,
    transcribed_at      INTEGER,
    summarized_at       INTEGER,

    -- 耗时（秒）
    video_duration      REAL,                         -- 视频本身时长
    transcribe_duration REAL,                         -- ASR 转写耗时

    -- 元数据
    fetched_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,

    FOREIGN KEY (sec_user_id) REFERENCES creators(sec_user_id)
);

CREATE INDEX IF NOT EXISTS idx_videos_sec_user_id ON videos(sec_user_id);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_create_time ON videos(create_time DESC);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sec_user_id     TEXT NOT NULL,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER,
    new_count       INTEGER DEFAULT 0,
    existing_count  INTEGER DEFAULT 0,
    processed_count INTEGER DEFAULT 0,
    failed_count    INTEGER DEFAULT 0,
    duration_sec    REAL,

    FOREIGN KEY (sec_user_id) REFERENCES creators(sec_user_id)
);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_sec_user_id ON crawl_runs(sec_user_id);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at ON crawl_runs(started_at DESC);

-- 多 LLM 版本摘要：每个视频可以有多个 provider/model 跑出的版本
CREATE TABLE IF NOT EXISTS llm_summaries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    summary           TEXT,
    article           TEXT,                         -- 完整文章（不只是要点列表）
    key_points        TEXT,                         -- JSON array string
    knowledge_points  TEXT,                         -- JSON array string
    created_at        INTEGER NOT NULL,
    is_primary        INTEGER DEFAULT 0,            -- 1 表示当前展示版本
    summarize_duration REAL,                        -- LLM 调用耗时（秒）
    UNIQUE(aweme_id, provider, model),
    FOREIGN KEY (aweme_id) REFERENCES videos(aweme_id)
);

CREATE INDEX IF NOT EXISTS idx_llm_summaries_aweme_id ON llm_summaries(aweme_id);
CREATE INDEX IF NOT EXISTS idx_llm_summaries_primary ON llm_summaries(is_primary);