from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .config import Config
from .db import connect, now_ts
from .llm_client import LLMProvider, _parse_json_lenient


SYSTEM_PROMPT = """你是一位学习方法与知识体系的拆解专家，擅长从短视频文字稿中
重构成一篇结构清晰、逻辑流畅、可独立阅读的文章，并从中提炼出要点与值得深入探究的知识点。

请基于以下视频文字稿输出严格的 JSON，schema 如下：

{
  "summary": "200-300 字总结，描述视频主旨和核心论点",
  "article": "一篇 800-1500 字的完整文章，用连贯的散文体（不要 markdown 标记，纯段落+小标题），把视频里的论述结构化重写。开头引入背景/问题，主体分段展开论证或方法，结尾给出结论或观点",
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

要求：
- 只输出 JSON，不要其他文字
- 不要 markdown 围栏（```）
- 不要思考过程、不要分析步骤、不要任何解释性文字，直接输出 JSON 对象本身
- 全部中文输出
- article 是文章正文，不要包含 summary 字段、要点列表这些重复信息
- key_points 至少 2 条，最多 5 条
- knowledge_points 1-3 条
- 必须以 { 开头，以 } 结尾"""


def _claim(conn: sqlite3.Connection, aweme_id: str, from_status: str, to_status: str) -> bool:
    cur = conn.execute(
        "UPDATE videos SET status=? WHERE aweme_id=? AND status=?",
        (to_status, aweme_id, from_status),
    )
    return cur.rowcount == 1


def _mark_failed(conn: sqlite3.Connection, aweme_id: str, msg: str) -> None:
    conn.execute(
        "UPDATE videos SET status='summarize_failed', status_message=?, retry_count=retry_count+1 WHERE aweme_id=?",
        (msg[:500], aweme_id),
    )


def _build_user_prompt(title: str, transcript: str) -> str:
    if len(transcript) > 10000:
        transcript = transcript[:10000] + "\n... (truncated)"
    return f"视频标题：{title}\n\n视频文字稿（带时间戳）：\n{transcript}"


def _validate_llm_json(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    for key in ("summary", "article"):
        if key not in data or not isinstance(data[key], str):
            raise ValueError(f"missing or invalid '{key}'")
    if "key_points" not in data or not isinstance(data["key_points"], list):
        raise ValueError("missing or invalid 'key_points'")
    if len(data["key_points"]) < 2:
        raise ValueError("key_points must have at least 2 items")
    if "knowledge_points" not in data or not isinstance(data["knowledge_points"], list):
        raise ValueError("missing or invalid 'knowledge_points'")


def _write_summary_md(cfg: Config, aweme_id: str, title: str, video_url: str,
                     create_time: int, liked_count: int,
                     data: dict, provider_name: str, model_name: str) -> Path:
    summary_path = cfg.paths_for(aweme_id)["summary"]
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    import datetime
    pub = datetime.datetime.fromtimestamp(create_time).strftime("%Y-%m-%d")
    lines = [
        f"# {title}",
        "",
        f"> 发布 {pub} · 点赞 {liked_count} · [原视频]({video_url})",
        f"> LLM: {provider_name}/{model_name}",
        "",
        "## 摘要",
        "",
        data["summary"],
        "",
        "## 正文",
        "",
        data["article"],
        "",
        "## 要点",
        "",
    ]
    for kp in data["key_points"]:
        lines.append(f"- {kp}")
    lines += ["", "## 值得探究的知识点", ""]
    for i, kp in enumerate(data.get("knowledge_points", []) or [], 1):
        topic = kp.get("topic", "") if isinstance(kp, dict) else str(kp)
        why = kp.get("why", "") if isinstance(kp, dict) else ""
        direction = kp.get("direction", "") if isinstance(kp, dict) else ""
        lines += [
            f"### {i}. {topic}",
            f"**为什么**：{why}" if why else "",
            f"**深入方向**：{direction}" if direction else "",
            "",
        ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path


def _save_llm_summary(cfg: Config, aweme_id: str, provider_name: str, model_name: str,
                      data: dict, md_path: Path, summarize_duration: float) -> None:
    """写入 llm_summaries 表；若该视频无 primary 版本，则标 primary 并同步到 videos 表"""
    with connect(cfg) as conn:
        existing_primary = conn.execute(
            "SELECT 1 FROM llm_summaries WHERE aweme_id=? AND is_primary=1",
            (aweme_id,),
        ).fetchone()
        is_primary = 0 if existing_primary else 1

        conn.execute(
            """INSERT INTO llm_summaries
               (aweme_id, provider, model, summary, article, key_points,
                knowledge_points, created_at, is_primary, summarize_duration)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(aweme_id, provider, model) DO UPDATE SET
                 summary=excluded.summary,
                 article=excluded.article,
                 key_points=excluded.key_points,
                 knowledge_points=excluded.knowledge_points,
                 created_at=excluded.created_at,
                 summarize_duration=excluded.summarize_duration""",
            (aweme_id, provider_name, model_name,
             data["summary"], data["article"],
             json.dumps(data["key_points"], ensure_ascii=False),
             json.dumps(data.get("knowledge_points", []), ensure_ascii=False),
             now_ts(), is_primary, summarize_duration),
        )

        if is_primary:
            conn.execute(
                """UPDATE videos SET
                   status='done',
                   summary=?,
                   key_points=?,
                   knowledge_points=?,
                   summary_path=?,
                   llm_provider=?,
                   llm_model=?,
                   summarized_at=?,
                   status_message=NULL
                   WHERE aweme_id=?""",
                (data["summary"],
                 json.dumps(data["key_points"], ensure_ascii=False),
                 json.dumps(data.get("knowledge_points", []), ensure_ascii=False),
                 str(md_path.relative_to(cfg.project_root)),
                 provider_name, model_name, now_ts(), aweme_id),
            )


def set_primary_summary(cfg: Config, aweme_id: str, summary_id: int) -> None:
    """切换某条视频的主展示版本"""
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE llm_summaries SET is_primary=0 WHERE aweme_id=?",
            (aweme_id,),
        )
        cur = conn.execute(
            "UPDATE llm_summaries SET is_primary=1 WHERE id=? AND aweme_id=?",
            (summary_id, aweme_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"summary id {summary_id} not found for {aweme_id}")
        row = conn.execute(
            "SELECT * FROM llm_summaries WHERE id=?", (summary_id,)
        ).fetchone()
        conn.execute(
            """UPDATE videos SET
               summary=?, key_points=?, knowledge_points=?,
               llm_provider=?, llm_model=?, summarized_at=?
               WHERE aweme_id=?""",
            (row["summary"], row["key_points"], row["knowledge_points"],
             row["provider"], row["model"], row["created_at"], aweme_id),
        )


def summarize_one(cfg: Config, aweme_id: str, provider: LLMProvider) -> bool:
    """对单个视频用指定 provider 生成摘要；返回是否成功"""
    with connect(cfg) as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE aweme_id=?", (aweme_id,)
        ).fetchone()
        if row is None:
            print(f"[summarize] {aweme_id} not in db, skip")
            return False
        # 只要求视频已转写完成（transcribed 或 已 done 但想换 provider）
        if row["status"] not in ("transcribed", "done"):
            print(f"[summarize] {aweme_id} status={row['status']}, skip")
            return False
        # 如果该 provider 已经跑过且无 --force，则跳过
        existing = conn.execute(
            "SELECT 1 FROM llm_summaries WHERE aweme_id=? AND provider=? AND model=?",
            (aweme_id, provider.name, provider.model),
        ).fetchone()
        if existing:
            print(f"[summarize] {aweme_id} already has {provider.name}/{provider.model}, skip (use reset to redo)")
            return False

    # 转写文件
    transcript_path = cfg.paths_for(aweme_id)["transcript"]
    if not transcript_path.exists() and row["transcript_path"]:
        tp = cfg.project_root / row["transcript_path"]
        if tp.exists():
            transcript_path = tp
    if not transcript_path.exists():
        with connect(cfg) as conn:
            _mark_failed(conn, aweme_id, f"transcript missing: {transcript_path}")
        return False

    transcript = transcript_path.read_text(encoding="utf-8")
    if not transcript.strip():
        with connect(cfg) as conn:
            _mark_failed(conn, aweme_id, "transcript empty")
        return False

    # 抢占状态：先转 summarizing（只在 status=transcribed 时抢；已 done 不动 status）
    if row["status"] == "transcribed":
        with connect(cfg) as conn:
            if not _claim(conn, aweme_id, "transcribed", "summarizing"):
                return False

    title = row["title"] or aweme_id
    user_prompt = _build_user_prompt(title, transcript)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    print(f"[summarize] {aweme_id} calling {provider.name}/{provider.model} (transcript {len(transcript)} chars)")
    t0 = time.time()
    try:
        raw = provider.chat(messages)
        try:
            data = _parse_json_lenient(raw)
            _validate_llm_json(data)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"[summarize] {aweme_id} JSON invalid ({e}); retry with temperature=0.1")
            if hasattr(provider, "temperature"):
                orig_t = provider.temperature
                provider.temperature = 0.1
                raw = provider.chat(messages)
                provider.temperature = orig_t
            data = _parse_json_lenient(raw)
            _validate_llm_json(data)
        duration = time.time() - t0
        print(f"[summarize] {aweme_id} {provider.name}/{provider.model} done in {duration:.1f}s "
              f"article={len(data.get('article',''))} chars")

        md_path = _write_summary_md(
            cfg, aweme_id, title, row["aweme_url"] or "",
            int(row["create_time"]), int(row["liked_count"] or 0),
            data, provider.name, provider.model,
        )
        _save_llm_summary(cfg, aweme_id, provider.name, provider.model, data, md_path, duration)

        # 如果之前是 summarizing，转 done（若已 done 则不动）
        if row["status"] == "transcribed":
            with connect(cfg) as conn:
                conn.execute(
                    "UPDATE videos SET status='done' WHERE aweme_id=? AND status='summarizing'",
                    (aweme_id,),
                )
        print(f"[summarize] {aweme_id} {provider.name}/{provider.model} saved")
        return True
    except Exception as e:
        print(f"[summarize] {aweme_id} {provider.name}/{provider.model} failed: {e!r}")
        if row["status"] == "transcribed":
            with connect(cfg) as conn:
                _mark_failed(conn, aweme_id, str(e))
        return False


def summarize_with_all_providers(cfg: Config, aweme_id: str, force: bool = False) -> dict:
    """对一个视频，用 yaml 中所有 provider 各跑一遍"""
    from .llm_client import load_all_providers
    providers = load_all_providers(cfg.llm_providers_path)

    results = {"success": [], "failed": []}
    for provider in providers:
        if force:
            with connect(cfg) as conn:
                conn.execute(
                    "DELETE FROM llm_summaries WHERE aweme_id=? AND provider=? AND model=?",
                    (aweme_id, provider.name, provider.model),
                )
        ok = summarize_one(cfg, aweme_id, provider)
        (results["success"] if ok else results["failed"]).append(
            (f"{provider.name}/{provider.model}", None)
        )
    return results


def tick(cfg: Config, provider: LLMProvider, max_process: int = 5) -> int:
    processed = 0
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT aweme_id FROM videos WHERE status='transcribed' ORDER BY create_time DESC LIMIT ?",
            (max_process,),
        ).fetchall()
    for row in rows:
        if summarize_one(cfg, row["aweme_id"], provider):
            processed += 1
    return processed


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m src.summarize_worker <aweme_id> [--all-providers] [--force]")
        sys.exit(1)
    aweme_id = sys.argv[1]
    use_all = "--all-providers" in sys.argv
    force = "--force" in sys.argv
    from .config import load_config
    cfg = load_config()
    if use_all:
        r = summarize_with_all_providers(cfg, aweme_id, force=force)
        print("success:", r["success"])
        print("failed:", r["failed"])
    else:
        from .llm_client import load_provider_from_yaml
        p = load_provider_from_yaml(cfg.llm_providers_path)
        summarize_one(cfg, aweme_id, p)