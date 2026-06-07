"""Telegram 频道视频搬运核心服务（支持多账号）。

- ForwarderService：单个账号的搬运服务（登录/启停/状态/日志）。
- AccountManager：管理多个账号及全局配置（含面板密码）。

命令行用法仍可用：python forwarder.py  （驱动第一个账号）
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

from config import AccountSettings, AppConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("telethon").setLevel(logging.WARNING)


# ------------------------- 内存日志缓冲 -------------------------
class _BufferHandler(logging.Handler):
    """把日志记录写入一个内存环形缓冲，供 Web 界面读取。"""

    def __init__(self, buffer: Deque[str]):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


_LOG_FMT = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
)


def is_video_message(message) -> bool:
    """判断一条消息是否包含视频（排除圆形视频留言）。"""
    if message is None:
        return False
    if getattr(message, "video", None) is not None:
        if getattr(message, "video_note", None) is not None:
            return False
        return True
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaDocument) and media.document is not None:
        doc = media.document
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("video/"):
            return True
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeVideo):
                if getattr(attr, "round_message", False):
                    return False
                return True
    return False


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    username = getattr(entity, "username", None)
    ident = getattr(entity, "id", "?")
    if title:
        return f"{title} (id={ident})"
    if username:
        return f"@{username} (id={ident})"
    return f"id={ident}"


def _fmt(epoch) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


class ForwarderService:
    """单个账号的搬运服务。所有方法在调用方的事件循环中运行。"""

    def __init__(self, settings: AccountSettings):
        self.settings: AccountSettings = settings
        self.client: Optional[TelegramClient] = None
        self.state: str = "stopped"  # stopped | running
        self._handler = None
        self._target_entity = None
        self._sources: List = []
        self._seen: set = set()
        self._lock = asyncio.Lock()
        self._login_phone: Optional[str] = None
        self._login_hash: Optional[str] = None
        self.stats = {
            "forwarded": 0,
            "started_at": None,
            "last_video_at": None,
            "errors": 0,
        }
        # 每个账号一份独立日志缓冲与 logger
        self.log_buffer: Deque[str] = deque(maxlen=500)
        self.logger = logging.getLogger(f"forwarder.{settings.id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = True  # 仍输出到控制台
        handler = _BufferHandler(self.log_buffer)
        handler.setFormatter(_LOG_FMT)
        self.logger.addHandler(handler)

    # ------------------------- 客户端生命周期 -------------------------
    async def ensure_client(self) -> TelegramClient:
        if self.client is None:
            if not self.settings.api_id or not self.settings.api_hash:
                raise RuntimeError("缺少 API_ID / API_HASH，请先在设置中填写。")
            self.client = TelegramClient(
                self.settings.session_name,
                self.settings.api_id,
                self.settings.api_hash,
            )
        if not self.client.is_connected():
            await self.client.connect()
        return self.client

    async def is_authorized(self) -> bool:
        try:
            client = await self.ensure_client()
        except RuntimeError:
            return False
        try:
            return await client.is_user_authorized()
        except Exception:  # noqa: BLE001
            return False

    async def reset_client(self) -> None:
        await self.stop()
        if self.client is not None and self.client.is_connected():
            await self.client.disconnect()
        self.client = None

    # ------------------------- 登录流程 -------------------------
    async def send_code(self, phone: str) -> None:
        client = await self.ensure_client()
        phone = phone.strip()
        sent = await client.send_code_request(phone)
        self._login_phone = phone
        self._login_hash = sent.phone_code_hash
        self.logger.info("验证码已发送至 %s", phone)

    async def sign_in(self, code: str, password: Optional[str] = None) -> None:
        client = await self.ensure_client()
        if not self._login_phone:
            raise RuntimeError("请先请求验证码。")
        try:
            await client.sign_in(
                phone=self._login_phone,
                code=code.strip(),
                phone_code_hash=self._login_hash,
            )
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError("该账号开启了两步验证，请提供密码。")
            await client.sign_in(password=password)
        self._login_phone = None
        self._login_hash = None
        me = await client.get_me()
        self.logger.info("登录成功：%s", _entity_name(me))

    async def logout(self) -> None:
        await self.stop()
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        self.logger.info("已退出登录。")

    # ------------------------- 搬运 -------------------------
    async def _resolve_entities(self):
        self._target_entity = await self.client.get_entity(
            self.settings.resolved_target()
        )
        self.logger.info("目标频道已就绪: %s", _entity_name(self._target_entity))
        resolved = []
        for src in self.settings.resolved_sources():
            try:
                entity = await self.client.get_entity(src)
                resolved.append(entity)
                self.logger.info("监控源频道: %s", _entity_name(entity))
            except Exception as exc:  # noqa: BLE001
                self.logger.error("无法解析源频道 %r: %s（确认账号能访问该频道）", src, exc)
        if not resolved:
            raise RuntimeError("没有任何可用的源频道。")
        return resolved

    async def _send(self, message):
        key = (message.chat_id, message.id)
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            if self.settings.mode == "forward":
                await self.client.forward_messages(self._target_entity, message)
            else:
                caption = message.text if self.settings.keep_caption else None
                await self.client.send_file(
                    self._target_entity, file=message.media, caption=caption
                )
            self.stats["forwarded"] += 1
            self.stats["last_video_at"] = time.time()
            self.logger.info("已搬运视频 [%s:%s] -> 目标频道", message.chat_id, message.id)
        except FloodWaitError as exc:
            self.logger.warning("触发限流，需等待 %s 秒", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            self._seen.discard(key)
            await self._send(message)
            return
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.logger.error("搬运失败 [%s:%s]: %s", message.chat_id, message.id, exc)
            return
        if self.settings.send_delay > 0:
            await asyncio.sleep(self.settings.send_delay)

    async def _backfill(self):
        limit = self.settings.backfill_limit
        if limit <= 0:
            return
        self.logger.info("开始搬运历史视频，每个源频道最多 %s 条…", limit)
        for entity in self._sources:
            collected = []
            async for msg in self.client.iter_messages(entity, limit=limit * 5):
                if is_video_message(msg):
                    collected.append(msg)
                if len(collected) >= limit:
                    break
            for msg in reversed(collected):
                await self._send(msg)
        self.logger.info("历史视频搬运完成。")

    # ------------------------- 启动 / 停止 -------------------------
    async def start(self) -> None:
        async with self._lock:
            if self.state == "running":
                return
            problems = self.settings.validate()
            if problems:
                raise RuntimeError("配置不完整：" + "；".join(problems))
            client = await self.ensure_client()
            if not await client.is_user_authorized():
                raise RuntimeError("尚未登录，请先在界面完成登录。")

            self._sources = await self._resolve_entities()

            async def _handler(event):  # noqa: ANN001
                if is_video_message(event.message):
                    self.logger.info(
                        "检测到新视频 [%s:%s]", event.chat_id, event.message.id
                    )
                    await self._send(event.message)

            self._handler = _handler
            client.add_event_handler(_handler, events.NewMessage(chats=self._sources))
            self.state = "running"
            self.stats["started_at"] = time.time()
            self.logger.info("开始监控，等待新视频…")

        if self.settings.backfill_limit > 0:
            asyncio.create_task(self._safe_backfill())

    async def _safe_backfill(self):
        try:
            await self._backfill()
        except Exception as exc:  # noqa: BLE001
            self.logger.error("历史搬运出错: %s", exc)

    async def stop(self) -> None:
        if self.client is not None and self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:  # noqa: BLE001
                pass
        self._handler = None
        if self.state == "running":
            self.logger.info("已停止监控。")
        self.state = "stopped"

    # ------------------------- 状态 -------------------------
    async def status(self) -> dict:
        authorized = await self.is_authorized()
        return {
            "id": self.settings.id,
            "name": self.settings.name,
            "state": self.state,
            "authorized": authorized,
            "awaiting_code": self._login_phone is not None,
            "stats": {
                **self.stats,
                "started_at_str": _fmt(self.stats["started_at"]),
                "last_video_at_str": _fmt(self.stats["last_video_at"]),
            },
            "settings": self.settings.public_dict(),
            "problems": self.settings.validate(),
        }

    def get_logs(self, limit: int = 200) -> List[str]:
        items = list(self.log_buffer)
        return items[-limit:]

    async def apply_settings(self, data: dict) -> bool:
        """用新设置覆盖当前账号，返回是否变更了凭证（需重建客户端）。"""
        old = self.settings
        h = str(data.get("api_hash") or "")
        if not h.strip() or "•" in h:
            data["api_hash"] = old.api_hash
        data["id"] = old.id  # id 不可改
        if not data.get("session_name"):
            data["session_name"] = old.session_name
        new = AccountSettings.from_dict(data)
        creds_changed = (
            new.api_id != old.api_id
            or new.api_hash != old.api_hash
            or new.session_name != old.session_name
        )
        self.settings = new
        self.logger.info("配置已保存。")
        if creds_changed:
            await self.reset_client()
        return creds_changed


# ------------------------- 多账号管理器 -------------------------
class AccountManager:
    """管理多个 ForwarderService 实例以及全局配置（含面板密码）。"""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config: AppConfig = config or AppConfig.load()
        self.services: Dict[str, ForwarderService] = {}
        for acct in self.config.accounts:
            self.services[acct.id] = ForwarderService(acct)

    # ----- 查询 -----
    def get(self, account_id: str) -> ForwarderService:
        svc = self.services.get(account_id)
        if svc is None:
            raise KeyError(f"账号不存在：{account_id}")
        return svc

    async def list_status(self) -> List[dict]:
        return [await svc.status() for svc in self.services.values()]

    # ----- 账号增删 -----
    def add_account(self, name: str = "新账号") -> ForwarderService:
        acct = AccountSettings(name=name or "新账号")
        self.config.add(acct)
        self.config.save()
        svc = ForwarderService(acct)
        self.services[acct.id] = svc
        return svc

    async def remove_account(self, account_id: str) -> None:
        svc = self.services.get(account_id)
        if svc:
            await svc.logout()
        self.config.remove(account_id)
        self.config.save()
        self.services.pop(account_id, None)

    async def apply_settings(self, account_id: str, data: dict) -> None:
        svc = self.get(account_id)
        await svc.apply_settings(data)
        # 同步回 config 并持久化
        self.config.accounts = [s.settings for s in self.services.values()]
        self.config.save()

    # ----- 密码 -----
    def set_password(self, password: str) -> None:
        self.config.set_password(password)

    def password_enabled(self) -> bool:
        return self.config.password_enabled

    def check_password(self, password: str) -> bool:
        return self.config.check_password(password)


# ------------------------- 命令行入口（驱动第一个账号） -------------------------
def main():
    manager = AccountManager()
    if not manager.services:
        print("未配置任何账号。请先用 Web 界面(python web.py)添加，或在 .env 填写凭证。")
        return
    service = next(iter(manager.services.values()))

    async def _run():
        await service.ensure_client()
        if not await service.client.is_user_authorized():
            await service.client.start()
        await service.start()
        service.logger.info("（Ctrl+C 退出）")
        await service.client.run_until_disconnected()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        service.logger.info("已手动停止。")


if __name__ == "__main__":
    main()
