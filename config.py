"""加载并校验运行配置（从 .env 读取）。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


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
        raise SystemExit(f"配置 {name} 必须是整数，当前值: {raw!r}")


def _parse_channel(value: str):
    """把单个频道标识解析成 Telethon 可用的形式。

    - 纯数字（含负号）-> int（频道/群的内部ID）
    - 其它（@username / t.me 链接）-> 原样字符串
    """
    value = value.strip()
    if not value:
        return None
    # 去掉常见的链接前缀，保留 username
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    # 纯数字ID
    candidate = value.lstrip("-")
    if candidate.isdigit():
        return int(value)
    return value


@dataclass
class Config:
    api_id: int
    api_hash: str
    session_name: str
    source_channels: List = field(default_factory=list)
    target_channel: object = None
    mode: str = "copy"
    keep_caption: bool = True
    backfill_limit: int = 0
    send_delay: int = 3

    @classmethod
    def load(cls) -> "Config":
        api_id_raw = os.getenv("API_ID", "").strip()
        api_hash = os.getenv("API_HASH", "").strip()
        if not api_id_raw or not api_hash:
            raise SystemExit(
                "缺少 API_ID / API_HASH。请复制 .env.example 为 .env 并填写。"
            )
        try:
            api_id = int(api_id_raw)
        except ValueError:
            raise SystemExit(f"API_ID 必须是数字，当前值: {api_id_raw!r}")

        sources_raw = os.getenv("SOURCE_CHANNELS", "")
        sources = [c for c in (_parse_channel(s) for s in sources_raw.split(",")) if c]
        if not sources:
            raise SystemExit("SOURCE_CHANNELS 不能为空，请至少配置一个源频道。")

        target = _parse_channel(os.getenv("TARGET_CHANNEL", ""))
        if not target:
            raise SystemExit("TARGET_CHANNEL 不能为空，请配置目标频道。")

        mode = os.getenv("MODE", "copy").strip().lower()
        if mode not in {"copy", "forward"}:
            raise SystemExit("MODE 只能是 copy 或 forward。")

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_name=os.getenv("SESSION_NAME", "forwarder").strip() or "forwarder",
            source_channels=sources,
            target_channel=target,
            mode=mode,
            keep_caption=_get_bool("KEEP_CAPTION", True),
            backfill_limit=_get_int("BACKFILL_LIMIT", 0),
            send_delay=_get_int("SEND_DELAY", 3),
        )
