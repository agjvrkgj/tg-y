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


# ------------------------- 应用级配置 -------------------------
@dataclass
class AppConfig:
    password_salt: str = ""
    password_hash: str = ""
    accounts: List[AccountSettings] = field(default_factory=list)

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
        return acct

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
