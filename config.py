"""配置/设置存储（多账号 + 面板密码）。

优先从 settings.json 读取（可由 Web 界面读写）；
若不存在，则从 .env 环境变量回退（兼容旧的单账号用法）。

settings.json 结构：
{
  "password_salt": "...",      # 面板密码盐（hex）
  "password_hash": "...",      # 面板密码哈希（hex），未设置则为空
  "accounts": [ {AccountSettings...}, ... ]
}
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Union

from dotenv import load_dotenv

load_dotenv()

SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", "settings.json"))

ChannelId = Union[int, str]


# ------------------------- 环境变量小工具 -------------------------
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
    """把单个频道标识解析成 Telethon 可用的形式。"""
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
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    return [str(s).strip() for s in items if str(s).strip()]


# ------------------------- 密码哈希 -------------------------
def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """返回 (salt_hex, hash_hex)。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000
    )
    return salt, digest.hex()


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    if not salt or not expected_hash:
        return False
    _, actual = hash_password(password, salt)
    return secrets.compare_digest(actual, expected_hash)


# ------------------------- 内容过滤 -------------------------
# 链接匹配：http(s):// 链接、t.me 链接、裸域名(含常见 TLD)
_URL_RE = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b(?:t\.me|telegram\.me|telegram\.dog)/\S+"
    r"|\b[a-zA-Z0-9-]+\.(?:com|net|org|io|me|cn|cc|xyz|info|top|vip|tv|app|link|shop)\b\S*",
    re.IGNORECASE,
)
# @用户名（@ 后跟 5 位以上字母数字下划线，Telegram 用户名规则的宽松版）
_MENTION_RE = re.compile(r"@[A-Za-z0-9_]{3,}")


@dataclass
class ContentFilter:
    """搬运时对文案(caption)做内容过滤的配置。

    - block_keywords：命中其中任意关键词时，按 drop_on_match 决定丢弃整条或仅清洗文案。
    - remove_links / remove_mentions：从文案中去除链接 / @提及。
    - replacement：被去除片段的替换文本（默认空串）。
    - drop_on_match：命中屏蔽关键词时是否直接丢弃整条消息（不搬运）。
    """

    block_keywords: List[str] = field(default_factory=list)
    remove_links: bool = False
    remove_mentions: bool = False
    replacement: str = ""
    drop_on_match: bool = False

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ContentFilter":
        data = data or {}
        kws = data.get("block_keywords")
        if isinstance(kws, str):
            # 允许用换行/逗号分隔的字符串
            kws = re.split(r"[\n,]", kws)
        keywords = [str(k).strip() for k in (kws or []) if str(k).strip()]
        return cls(
            block_keywords=keywords,
            remove_links=bool(data.get("remove_links", False)),
            remove_mentions=bool(data.get("remove_mentions", False)),
            replacement=str(data.get("replacement") or ""),
            drop_on_match=bool(data.get("drop_on_match", False)),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def active(self) -> bool:
        return bool(self.block_keywords or self.remove_links or self.remove_mentions)

    def matches_keyword(self, text: Optional[str]) -> bool:
        """文案是否命中任一屏蔽关键词（大小写不敏感）。"""
        if not text or not self.block_keywords:
            return False
        low = text.lower()
        return any(kw.lower() in low for kw in self.block_keywords)

    def should_drop(self, text: Optional[str]) -> bool:
        """是否应直接丢弃整条消息：开启 drop_on_match 且命中关键词。"""
        return self.drop_on_match and self.matches_keyword(text)

    def apply(self, text: Optional[str]) -> Optional[str]:
        """对文案做清洗，返回处理后的文案（不负责丢弃判断）。

        顺序：去链接 -> 去@提及 -> 删除含屏蔽关键词的整行 -> 规整空白。
        """
        if text is None:
            return None
        result = text
        if self.remove_links:
            result = _URL_RE.sub(self.replacement, result)
        if self.remove_mentions:
            result = _MENTION_RE.sub(self.replacement, result)
        if self.block_keywords and not self.drop_on_match:
            # 非丢弃模式下：逐行删除包含屏蔽词的行
            kept = []
            lows = [kw.lower() for kw in self.block_keywords]
            for line in result.splitlines():
                if any(kw in line.lower() for kw in lows):
                    continue
                kept.append(line)
            result = "\n".join(kept)
        # 规整：合并多余空行与首尾空白
        result = re.sub(r"\n{3,}", "\n\n", result).strip()
        return result


# ------------------------- 单账号设置 -------------------------
@dataclass
class AccountSettings:
    """单个 Telegram 账号的全部可配置项。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "新账号"
    api_id: int = 0
    api_hash: str = ""
    session_name: str = ""
    source_channels: List[str] = field(default_factory=list)
    target_channel: str = ""
    mode: str = "copy"  # copy | forward
    keep_caption: bool = True
    backfill_limit: int = 0
    send_delay: int = 3
    content_filter: ContentFilter = field(default_factory=ContentFilter)
    # 上传前删除视频开头的秒数（0=不裁剪）；仅 copy 模式生效，需要安装 ffmpeg
    trim_start_seconds: int = 0
    # 搬运相册(视频+图片+文字)时，用相册里的图片作为视频封面；仅 copy 模式生效
    album_cover: bool = True

    def __post_init__(self):
        if not self.session_name:
            self.session_name = f"session_{self.id}"

    @classmethod
    def from_dict(cls, data: dict) -> "AccountSettings":
        acct = cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "新账号").strip() or "新账号",
        )
        acct.api_id = int(data.get("api_id") or 0)
        acct.api_hash = str(data.get("api_hash") or "").strip()
        acct.session_name = str(data.get("session_name") or "").strip() or f"session_{acct.id}"
        acct.source_channels = _split_sources(data.get("source_channels"))
        acct.target_channel = str(data.get("target_channel") or "").strip()
        acct.mode = str(data.get("mode") or "copy").strip().lower()
        if acct.mode not in {"copy", "forward"}:
            acct.mode = "copy"
        acct.keep_caption = bool(data.get("keep_caption", True))
        try:
            acct.backfill_limit = int(data.get("backfill_limit") or 0)
        except (TypeError, ValueError):
            acct.backfill_limit = 0
        try:
            acct.send_delay = int(data.get("send_delay") or 0)
        except (TypeError, ValueError):
            acct.send_delay = 3
        acct.content_filter = ContentFilter.from_dict(data.get("content_filter"))
        try:
            acct.trim_start_seconds = max(0, int(data.get("trim_start_seconds") or 0))
        except (TypeError, ValueError):
            acct.trim_start_seconds = 0
        acct.album_cover = bool(data.get("album_cover", True))
        return acct

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        """返回给前端：脱敏 api_hash。"""
        data = self.to_dict()
        h = self.api_hash or ""
        data["api_hash"] = (("•" * max(len(h) - 4, 0)) + h[-4:]) if h else ""
        data["api_hash_set"] = bool(h)
        return data

    def resolved_sources(self) -> List[ChannelId]:
        return [c for c in (parse_channel(s) for s in self.source_channels) if c is not None]

    def resolved_target(self) -> Optional[ChannelId]:
        return parse_channel(self.target_channel)

    def validate(self) -> List[str]:
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


# ------------------------- 负载均衡组设置 -------------------------
@dataclass
class GroupSettings:
    """一个负载均衡组：多个账号共同把源频道视频搬运到目标频道。

    发送任务会在 member_ids 指定的账号间轮转分摊，以规避单账号限速。
    要求：组内每个账号都需能访问源频道与目标频道。
    """

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "新负载均衡组"
    member_ids: List[str] = field(default_factory=list)
    source_channels: List[str] = field(default_factory=list)
    target_channel: str = ""
    mode: str = "copy"  # copy | forward
    keep_caption: bool = True
    # 每个账号两次发送之间的最小间隔秒数（节流，越大越不易限速）
    per_account_delay: int = 5
    # 调度策略：balanced=挑最早可用的账号；round_robin=严格轮转
    strategy: str = "balanced"
    backfill_limit: int = 0
    content_filter: ContentFilter = field(default_factory=ContentFilter)
    # 上传前删除视频开头的秒数（0=不裁剪）；仅 copy 模式生效，需要安装 ffmpeg
    trim_start_seconds: int = 0
    # 搬运相册时用相册图片作为视频封面；仅 copy 模式生效
    album_cover: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "GroupSettings":
        g = cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=str(data.get("name") or "新负载均衡组").strip() or "新负载均衡组",
        )
        members = data.get("member_ids") or []
        g.member_ids = [str(m).strip() for m in members if str(m).strip()]
        g.source_channels = _split_sources(data.get("source_channels"))
        g.target_channel = str(data.get("target_channel") or "").strip()
        g.mode = str(data.get("mode") or "copy").strip().lower()
        if g.mode not in {"copy", "forward"}:
            g.mode = "copy"
        g.keep_caption = bool(data.get("keep_caption", True))
        g.strategy = str(data.get("strategy") or "balanced").strip().lower()
        if g.strategy not in {"balanced", "round_robin"}:
            g.strategy = "balanced"
        try:
            g.per_account_delay = max(0, int(data.get("per_account_delay") or 0))
        except (TypeError, ValueError):
            g.per_account_delay = 5
        try:
            g.backfill_limit = max(0, int(data.get("backfill_limit") or 0))
        except (TypeError, ValueError):
            g.backfill_limit = 0
        g.content_filter = ContentFilter.from_dict(data.get("content_filter"))
        try:
            g.trim_start_seconds = max(0, int(data.get("trim_start_seconds") or 0))
        except (TypeError, ValueError):
            g.trim_start_seconds = 0
        g.album_cover = bool(data.get("album_cover", True))
        return g

    def to_dict(self) -> dict:
        return asdict(self)

    def resolved_sources(self) -> List[ChannelId]:
        return [c for c in (parse_channel(s) for s in self.source_channels) if c is not None]

    def resolved_target(self) -> Optional[ChannelId]:
        return parse_channel(self.target_channel)

    def validate(self) -> List[str]:
        problems: List[str] = []
        if not self.member_ids:
            problems.append("至少需要选择一个成员账号")
        if not self.resolved_sources():
            problems.append("至少需要一个源频道")
        if not self.resolved_target():
            problems.append("需要配置目标频道")
        return problems


# ------------------------- 应用级配置 -------------------------
@dataclass
class AppConfig:
    password_salt: str = ""
    password_hash: str = ""
    accounts: List[AccountSettings] = field(default_factory=list)
    groups: List[GroupSettings] = field(default_factory=list)

    # ----- 持久化 -----
    @classmethod
    def load(cls) -> "AppConfig":
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                return cls.from_dict(data)
            except (json.JSONDecodeError, OSError):
                pass
        return cls.from_env()

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        cfg = cls(
            password_salt=str(data.get("password_salt") or ""),
            password_hash=str(data.get("password_hash") or ""),
        )
        accounts_raw = data.get("accounts")
        if isinstance(accounts_raw, list):
            cfg.accounts = [AccountSettings.from_dict(a) for a in accounts_raw]
        elif data.get("api_id") or data.get("api_hash"):
            # 兼容旧的单账号 settings.json（字段直接在顶层）
            cfg.accounts = [AccountSettings.from_dict(data)]
        groups_raw = data.get("groups")
        if isinstance(groups_raw, list):
            cfg.groups = [GroupSettings.from_dict(g) for g in groups_raw]
        return cfg

    @classmethod
    def from_env(cls) -> "AppConfig":
        cfg = cls()
        api_id_raw = os.getenv("API_ID", "").strip()
        api_hash = os.getenv("API_HASH", "").strip()
        if api_id_raw or api_hash:
            try:
                api_id = int(api_id_raw) if api_id_raw else 0
            except ValueError:
                api_id = 0
            mode = os.getenv("MODE", "copy").strip().lower()
            if mode not in {"copy", "forward"}:
                mode = "copy"
            cfg.accounts = [
                AccountSettings(
                    name="默认账号",
                    api_id=api_id,
                    api_hash=api_hash,
                    session_name=os.getenv("SESSION_NAME", "").strip() or "forwarder",
                    source_channels=_split_sources(os.getenv("SOURCE_CHANNELS", "")),
                    target_channel=os.getenv("TARGET_CHANNEL", "").strip(),
                    mode=mode,
                    keep_caption=_get_bool("KEEP_CAPTION", True),
                    backfill_limit=_get_int("BACKFILL_LIMIT", 0),
                    send_delay=_get_int("SEND_DELAY", 3),
                )
            ]
        return cfg

    def save(self) -> None:
        SETTINGS_FILE.write_text(
            json.dumps(
                {
                    "password_salt": self.password_salt,
                    "password_hash": self.password_hash,
                    "accounts": [a.to_dict() for a in self.accounts],
                    "groups": [g.to_dict() for g in self.groups],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    # ----- 账号管理 -----
    def get(self, account_id: str) -> Optional[AccountSettings]:
        return next((a for a in self.accounts if a.id == account_id), None)

    def add(self, acct: AccountSettings) -> None:
        self.accounts.append(acct)

    def remove(self, account_id: str) -> Optional[AccountSettings]:
        acct = self.get(account_id)
        if acct:
            self.accounts = [a for a in self.accounts if a.id != account_id]
        # 同时从所有组的成员里剔除该账号
        for g in self.groups:
            if account_id in g.member_ids:
                g.member_ids = [m for m in g.member_ids if m != account_id]
        return acct

    # ----- 组管理 -----
    def get_group(self, group_id: str) -> Optional[GroupSettings]:
        return next((g for g in self.groups if g.id == group_id), None)

    def add_group(self, group: GroupSettings) -> None:
        self.groups.append(group)

    def remove_group(self, group_id: str) -> Optional[GroupSettings]:
        g = self.get_group(group_id)
        if g:
            self.groups = [x for x in self.groups if x.id != group_id]
        return g

    # ----- 密码 -----
    @property
    def password_enabled(self) -> bool:
        # 环境变量优先；否则看是否存了哈希
        return bool(os.getenv("WEB_PASSWORD") or self.password_hash)

    def check_password(self, password: str) -> bool:
        env_pw = os.getenv("WEB_PASSWORD")
        if env_pw:
            return secrets.compare_digest(password or "", env_pw)
        return verify_password(password or "", self.password_salt, self.password_hash)

    def set_password(self, password: str) -> None:
        if not password:
            self.password_salt = ""
            self.password_hash = ""
        else:
            self.password_salt, self.password_hash = hash_password(password)
        self.save()
