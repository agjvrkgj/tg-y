"""Telegram 频道视频搬运工具。

功能：
- 用「用户账号」登录（MTProto），监听一个或多个源频道；
- 当源频道出现新的视频消息时，自动复制(copy)或转发(forward)到目标频道；
- 可选：启动时把每个源频道最近的若干条历史视频也搬运一遍（BACKFILL_LIMIT）。

运行：python forwarder.py
首次运行会要求输入手机号和验证码完成登录，之后会复用 .session 文件。
"""
from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    DocumentAttributeVideo,
    MessageMediaDocument,
)

from config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("forwarder")
# 降低 telethon 自身的日志噪音
logging.getLogger("telethon").setLevel(logging.WARNING)


def is_video_message(message) -> bool:
    """判断一条消息是否包含视频。

    覆盖：普通视频(message.video)、作为文档上传的视频(video mime),
    同时排除圆形视频留言(video note)和 GIF 动图（可按需放开）。
    """
    if message is None:
        return False

    # Telethon 的便捷属性：是视频时返回 Document，否则 None
    if getattr(message, "video", None) is not None:
        # 排除圆形视频留言（video note）
        if getattr(message, "video_note", None) is not None:
            return False
        return True

    # 兜底：检查 document 的属性 / mime 类型
    media = getattr(message, "media", None)
    if isinstance(media, MessageMediaDocument) and media.document is not None:
        doc = media.document
        mime = getattr(doc, "mime_type", "") or ""
        if mime.startswith("video/"):
            return True
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeVideo):
                # round_message 即视频留言，跳过
                if getattr(attr, "round_message", False):
                    return False
                return True
    return False


class VideoForwarder:
    def __init__(self, config: Config):
        self.config = config
        self.client = TelegramClient(
            config.session_name, config.api_id, config.api_hash
        )
        self._target_entity = None
        # 简单去重：记住已经处理过的 (chat_id, message_id)
        self._seen: set[tuple[int, int]] = set()

    async def _resolve_entities(self):
        """把配置里的频道标识解析成实体，提前发现配置错误。"""
        self._target_entity = await self.client.get_entity(
            self.config.target_channel
        )
        logger.info("目标频道已就绪: %s", _entity_name(self._target_entity))

        resolved_sources = []
        for src in self.config.source_channels:
            try:
                entity = await self.client.get_entity(src)
                resolved_sources.append(entity)
                logger.info("监控源频道: %s", _entity_name(entity))
            except Exception as exc:  # noqa: BLE001
                logger.error("无法解析源频道 %r: %s（请确认账号已加入/能访问该频道）", src, exc)
        if not resolved_sources:
            raise SystemExit("没有任何可用的源频道，退出。")
        return resolved_sources

    async def _send(self, message):
        """按配置把单条视频消息搬运到目标频道。"""
        key = (message.chat_id, message.id)
        if key in self._seen:
            return
        self._seen.add(key)

        try:
            if self.config.mode == "forward":
                await self.client.forward_messages(
                    self._target_entity, message
                )
            else:  # copy 模式：重新发送，去掉"转发自"标签
                caption = (
                    message.text if self.config.keep_caption else None
                )
                await self.client.send_file(
                    self._target_entity,
                    file=message.media,
                    caption=caption,
                )
            logger.info(
                "已搬运视频 [%s:%s] -> 目标频道", message.chat_id, message.id
            )
        except FloodWaitError as exc:
            logger.warning("触发限流，需等待 %s 秒", exc.seconds)
            await asyncio.sleep(exc.seconds + 1)
            # 重试一次
            self._seen.discard(key)
            await self._send(message)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("搬运失败 [%s:%s]: %s", message.chat_id, message.id, exc)
            return

        if self.config.send_delay > 0:
            await asyncio.sleep(self.config.send_delay)

    async def _backfill(self, sources):
        """启动时搬运每个源频道最近的历史视频。"""
        limit = self.config.backfill_limit
        if limit <= 0:
            return
        logger.info("开始搬运历史视频，每个源频道最多 %s 条…", limit)
        for entity in sources:
            collected = []
            # iter_messages 从新到旧；收集到 limit 条视频后倒序发送，保持时间顺序
            async for msg in self.client.iter_messages(entity, limit=limit * 5):
                if is_video_message(msg):
                    collected.append(msg)
                if len(collected) >= limit:
                    break
            for msg in reversed(collected):
                await self._send(msg)
        logger.info("历史视频搬运完成。")

    async def run(self):
        await self.client.start()
        logger.info("登录成功。")
        sources = await self._resolve_entities()

        # 注册新消息监听
        @self.client.on(events.NewMessage(chats=sources))
        async def _handler(event):  # noqa: ANN001
            if is_video_message(event.message):
                logger.info(
                    "检测到新视频 [%s:%s]", event.chat_id, event.message.id
                )
                await self._send(event.message)

        await self._backfill(sources)

        logger.info("开始监控，等待新视频…（Ctrl+C 退出）")
        await self.client.run_until_disconnected()


def _entity_name(entity) -> str:
    title = getattr(entity, "title", None)
    username = getattr(entity, "username", None)
    ident = getattr(entity, "id", "?")
    if title:
        return f"{title} (id={ident})"
    if username:
        return f"@{username} (id={ident})"
    return f"id={ident}"


def main():
    config = Config.load()
    forwarder = VideoForwarder(config)
    try:
        with forwarder.client:
            forwarder.client.loop.run_until_complete(forwarder.run())
    except KeyboardInterrupt:
        logger.info("已手动停止。")


if __name__ == "__main__":
    main()
