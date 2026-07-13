from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    """简易 .env 加载器：KEY=VALUE 一行一对，跳过注释和空行。不覆盖已存在的环境变量。"""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Config:
    project_root: Path
    data_dir: Path
    mediacrawler_dir: Path
    db_path: Path
    llm_providers_path: Path
    audio_dir: Path
    text_dir: Path
    summary_dir: Path
    jsonl_dir: Path
    videos_dir: Path

    @property
    def llm_providers(self) -> str:
        return os.environ.get("LLM_PROVIDER", "deepseek_chat")

    def paths_for(self, aweme_id: str) -> dict[str, Path]:
        return {
            "video": self.videos_dir / aweme_id / "video.mp4",
            "audio": self.audio_dir / f"{aweme_id}.wav",
            "transcript": self.text_dir / f"{aweme_id}.txt",
            "summary": self.summary_dir / f"{aweme_id}.md",
        }

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.audio_dir, self.text_dir, self.summary_dir,
                  self.jsonl_dir, self.videos_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    data_dir = root / "data"
    mediacrawler_dir = root / "MediaCrawler"
    # MP4 由 MediaCrawler 下载到它的 data/douyin/videos 目录；我们的 data/douyin 是符号/软链或直接指
    # 优先用 MediaCrawler 的产出目录，避免数据搬迁；用户可显式覆盖
    return Config(
        project_root=root,
        data_dir=data_dir,
        mediacrawler_dir=mediacrawler_dir,
        db_path=data_dir / "douyin-reader.db",
        llm_providers_path=data_dir / "llm_providers.yaml",
        audio_dir=data_dir / "audio",
        text_dir=data_dir / "text",
        summary_dir=data_dir / "summaries",
        jsonl_dir=mediacrawler_dir / "data" / "douyin" / "jsonl",
        videos_dir=mediacrawler_dir / "data" / "douyin" / "videos",
    )