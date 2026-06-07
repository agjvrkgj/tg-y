"""Telegram 频道视频搬运核心服务。

提供 ForwarderService：可被 Web 界面或命令行控制
（登录 / 启动监控 / 停止 / 查询状态 / 读取日志）。

命令行用法仍然可用：python forwarder.py
首次运行会要求输入手机号和验证码完成登录。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime
from typing import Deque, List, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeVideo, MessageMediaDocument

from config import Settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("forwarder")
logging.getLogger("telethon").setLevel(logging.WARNING)


# ------------------------- 内存日志缓冲 -------------------------
class _BufferHandler(logging.Handler):
    """把日志记录同时写入一个内存环形缓冲，供 Web 界面读取。"""

    def __init__(self, buffer: Deque[str]):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


LOG_BUFFER: Deque[str] = deque(maxlen=500)
_buffer_handler = _BufferHandler(LOG_BUFFER)
_buffer_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
)
logger.addHandler(_buffer_handler)


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


class ForwarderService:
    """可被控制的搬运服务（单实例）。所有方法在调用方的事件循环中运行。"""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings: Settings = settings or Settings.load()
        self.client: Optional[TelegramClient] = None
        self.state: str = "stopped"  # stopped | running
        self._handler = None
        self._target_entity = None
        self._sources: List = []
        self._seen: set = set()
        self._lock = asyncio.Lock()
        # 登录中间态
        self._login_phone: Optional[str] = None
        self._login_hash: Optional[str] = None
        # 统计
        self.stats = {
            "forwarded": 0,
            "started_at": None,  # epoch
            "last_video_at": None,  # epoch
            "errors": 0,
        }

    # ------------------------- 客户端生命周期 -------------------------
    async def ensure_client(self) -> TelegramClient:
        """确保 TelegramClient 已创建并连接（不负责登录）。"""
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
        """断开并丢弃当前客户端（用于 API 凭证变更后重建）。"""
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
        logger.info("验证码已发送至 %s", phone)

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
        logger.info("登录成功：%s", _entity_name(me))

    async def logout(self) -> None:
        await self.stop()
        if self.client is not None:
            try:
                await self.client.log_out()
            except Exception:  # noqa: BLE001
                pass
        self.client = None
        logger.info("已退出登录。")

    # ------------------------- 搬运 -------------------------
    async def _resolve_entities(self):
        self._target_entity = await self.client.get_entity(
            self.settings.resolved_target()
        )
        logger.info("目标频道已就绪: %s", _entity_name(self._target_entity))
        resolved = []
        for src in self.settings.resolved_sources():
            try:
                entity = await self.client.get_entity(src)
                resolved.append(entity)
                logger.info("监控源频道: %s", _entity_name(entity))
            except Exception as exc:  # noqa: BLE001
                logger.error("无法解析源频道 %r: %s（确认账号能访问该频道）", src, exc)
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
            logger.info("已搬运视频 [%s:%s] -> 目标频道", message.chat_id, message.id)
        except FloodWaitError as exc:
            logger.warning("触发限流，需等待 %s 秒", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            self._seen.discard(key)
            await self._send(message)
            return
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            logger.error("搬运失败 [%s:%s]: %s", message.chat_id, message.id, exc)
            return
        if self.settings.send_delay > 0:
            await asyncio.sleep(self.settings.send_delay)

    async def _backfill(self):
        limit = self.settings.backfill_limit
        if limit <= 0:
            return
        logger.info("开始搬运历史视频，每个源频道最多 %s 条…", limit)
        for entity in self._sources:
            collected = []
            async for msg in self.client.iter_messages(entity, limit=limit * 5):
                if is_video_message(msg):
                    collected.append(msg)
                if len(collected) >= limit:
                    break
            for msg in reversed(collected):
                await self._send(msg)
        logger.info("历史视频搬运完成。")

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
                    logger.info("检测到新视频 [%s:%s]", event.chat_id, event.message.id)
                    await self._send(event.message)

            self._handler = _handler
            client.add_event_handler(
                _handler, events.NewMessage(chats=self._sources)
            )
            self.state = "running"
            self.stats["started_at"] = time.time()
            logger.info("开始监控，等待新视频…")

        # 历史回填放后台执行，避免阻塞调用方
        if self.settings.backfill_limit > 0:
            asyncio.create_task(self._safe_backfill())

    async def _safe_backfill(self):
        try:
            await self._backfill()
        except Exception as exc:  # noqa: BLE001
            logger.error("历史搬运出错: %s", exc)

    async def stop(self) -> None:
        if self.client is not None and self._handler is not None:
            try:
                self.client.remove_event_handler(self._handler)
            except Exception:  # noqa: BLE001
                pass
        self._handler = None
        if self.state == "running":
            logger.info("已停止监控。")
        self.state = "stopped"

    # ------------------------- 状态 -------------------------
    async def status(self) -> dict:
        authorized = await self.is_authorized()
        return {
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
        items = list(LOG_BUFFER)
        return items[-limit:]

    async def apply_settings(self, data: dict) -> None:
        """用新设置覆盖，并按需重建客户端。运行中需先停止。"""
        old = self.settings
        # 若界面未重新填写 api_hash（被脱敏），保留旧值
        if not str(data.get("api_hash") or "").strip() or "•" in str(data.get("api_hash") or ""):
            data["api_hash"] = old.api_hash
        new = Settings.from_dict(data)
        creds_changed = (
            new.api_id != old.api_id
            or new.api_hash != old.api_hash
            or new.session_name != old.session_name
        )
        self.settings = new
        new.save()
        logger.info("配置已保存。")
        if creds_changed:
            await self.reset_client()


def _fmt(epoch) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


# ------------------------- 命令行入口 -------------------------
def main():
    service = ForwarderService()

    async def _run():
        await service.ensure_client()
        # 命令行交互式登录（如已登录则跳过）
        if not await service.client.is_user_authorized():
            await service.client.start()
        await service.start()
        logger.info("（Ctrl+C 退出）")
        await service.client.run_until_disconnected()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("已手动停止。")


if __name__ == "__main__":
    main()
