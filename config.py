"""配置/设置存储。

优先从 settings.json 读取（可由 Web 界面读写）；
若不存在，则从 .env 环境变量回退（兼容纯命令行用法）。
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from dotenv import load_dotenv

load_dotenv()

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "settings.json"))

ChannelId = Union[int, str]


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def parse_channel(value: str) -> Optional[ChannelId]:
    """把单个频道标识解析成 Telethon 可用的形式。

    - 纯数字（含负号）-> int（频道/群的内部ID）
    - 其它（@username / t.me 链接）-> 原样字符串
    - 空 -> None
    """
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    candidate = value.lstrip("-")
    if candidate.isdigit():
        return int(value)
    return value


def _split_sources(raw) -> List[str]:
    """把来源（可能是逗号字符串或列表）规整成去空白的字符串列表。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    return [str(s).strip() for s in items if str(s).strip()]


@dataclass
class Settings:
    """工具的全部可配置项。频道以「用户输入的原始字符串」形式保存，便于 Web 端编辑。"""

    api_id: int = 0
    api_hash: str = ""
    session_name: str = "forwarder"
    source_channels: List[str] = field(default_factory=list)
    target_channel: str = ""
    mode: str = "copy"  # copy | forward
    keep_caption: bool = True
    backfill_limit: int = 0
    send_delay: int = 3

    # ----- 持久化 -----
    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                return cls.from_dict(data)
            except (json.JSONDecodeError, OSError):
                pass
        return cls.from_env()

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        merged = cls()
        merged.api_id = int(data.get("api_id") or 0)
        merged.api_hash = str(data.get("api_hash") or "").strip()
        merged.session_name = str(data.get("session_name") or "forwarder").strip() or "forwarder"
        merged.source_channels = _split_sources(data.get("source_channels"))
        merged.target_channel = str(data.get("target_channel") or "").strip()
        merged.mode = str(data.get("mode") or "copy").strip().lower()
        if merged.mode not in {"copy", "forward"}:
            merged.mode = "copy"
        merged.keep_caption = bool(data.get("keep_caption", True))
        try:
            merged.backfill_limit = int(data.get("backfill_limit") or 0)
        except (TypeError, ValueError):
            merged.backfill_limit = 0
        try:
            merged.send_delay = int(data.get("send_delay") or 0)
        except (TypeError, ValueError):
            merged.send_delay = 3
        return merged

    @classmethod
    def from_env(cls) -> "Settings":
        api_id_raw = os.getenv("API_ID", "").strip()
        try:
            api_id = int(api_id_raw) if api_id_raw else 0
        except ValueError:
            api_id = 0
        mode = os.getenv("MODE", "copy").strip().lower()
        if mode not in {"copy", "forward"}:
            mode = "copy"
        return cls(
            api_id=api_id,
            api_hash=os.getenv("API_HASH", "").strip(),
            session_name=os.getenv("SESSION_NAME", "forwarder").strip() or "forwarder",
            source_channels=_split_sources(os.getenv("SOURCE_CHANNELS", "")),
            target_channel=os.getenv("TARGET_CHANNEL", "").strip(),
            mode=mode,
            keep_caption=_get_bool("KEEP_CAPTION", True),
            backfill_limit=_get_int("BACKFILL_LIMIT", 0),
            send_delay=_get_int("SEND_DELAY", 3),
        )

    def save(self) -> None:
        SETTINGS_FILE.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        """用于返回给前端：隐藏 api_hash 的大部分内容。"""
        data = self.to_dict()
        h = self.api_hash or ""
        data["api_hash"] = (("•" * max(len(h) - 4, 0)) + h[-4:]) if h else ""
        data["api_hash_set"] = bool(h)
        return data

    # ----- 解析后的频道 -----
    def resolved_sources(self) -> List[ChannelId]:
        return [c for c in (parse_channel(s) for s in self.source_channels) if c is not None]

    def resolved_target(self) -> Optional[ChannelId]:
        return parse_channel(self.target_channel)

    def validate(self) -> List[str]:
        """返回问题列表，空列表表示配置可用。"""
        problems: List[str] = []
        if not self.api_id:
            problems.append("缺少 API_ID")
        if not self.api_hash:
            problems.append("缺少 API_HASH")
        if not self.resolved_sources():
            problems.append("至少需要一个源频道")
        if not self.resolved_target():
            problems.append("需要配置目标频道")
        return problems
