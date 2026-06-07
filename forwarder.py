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

from config import AccountSettings, AppConfig, GroupSettings

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
        cf = self.settings.content_filter
        # 命中屏蔽关键词且开启"丢弃整条" -> 不搬运
        if cf.should_drop(message.text):
            self.logger.info("命中屏蔽词，丢弃整条 [%s:%s]", message.chat_id, message.id)
            return
        try:
            if self.settings.mode == "forward":
                await self.client.forward_messages(self._target_entity, message)
            else:
                caption = message.text if self.settings.keep_caption else None
                if caption and cf.active:
                    caption = cf.apply(caption)
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


# ------------------------- 负载均衡：选 worker -------------------------
def select_worker(worker_state: Dict[str, dict], now: float):
    """从 worker 状态里挑选一个账号来执行下一次发送。

    每个账号状态含：next_free_at（节流：下次允许发送时间）、
    cooldown_until（FloodWait 冷却到期时间）、disabled（不可用，如未登录/无访问权）。
    返回 (account_id, wait_seconds)；都不可用时返回 (None, 0)。

    策略：选「有效可用时间 = max(next_free_at, cooldown_until)」最小的账号，
    从而把发送压力摊到多个账号，并尊重每个账号的节流与冷却。
    """
    best_id = None
    best_avail = None
    for aid, st in worker_state.items():
        if st.get("disabled"):
            continue
        avail = max(st.get("next_free_at", 0.0), st.get("cooldown_until", 0.0))
        if best_avail is None or avail < best_avail:
            best_avail = avail
            best_id = aid
    if best_id is None:
        return None, 0.0
    return best_id, max(0.0, best_avail - now)


class GroupService:
    """负载均衡组：一个监听者 + 多个发送 worker（共享源/目标频道）。"""

    def __init__(self, settings: GroupSettings, manager: "AccountManager"):
        self.settings = settings
        self.manager = manager
        self.state = "stopped"  # stopped | running
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._listener_id: Optional[str] = None
        self._listener_handler = None
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._seen: Deque = deque(maxlen=2000)
        self._seen_set: set = set()
        self._rr_index = 0
        # worker_state: account_id -> {next_free_at, cooldown_until, disabled, sent, errors, name}
        self.worker_state: Dict[str, dict] = {}
        # 每个 member 解析出的实体：account_id -> {"target": ent, "sources": {chat_id: ent}}
        self._entities: Dict[str, dict] = {}
        self.stats = {"forwarded": 0, "errors": 0, "started_at": None, "last_video_at": None}
        self.log_buffer: Deque[str] = deque(maxlen=500)
        self.logger = logging.getLogger(f"group.{settings.id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = True
        handler = _BufferHandler(self.log_buffer)
        handler.setFormatter(_LOG_FMT)
        self.logger.addHandler(handler)

    def _mark_seen(self, key) -> bool:
        if key in self._seen_set:
            return False
        self._seen.append(key)
        self._seen_set.add(key)
        # 控制内存：超出环形缓冲时同步清理 set
        if len(self._seen_set) > self._seen.maxlen:
            self._seen_set = set(self._seen)
        return True

    async def _resolve_for_members(self):
        """为每个已登录成员解析源/目标实体；返回可用成员 id 列表。"""
        target_input = self.settings.resolved_target()
        source_inputs = self.settings.resolved_sources()
        available = []
        for aid in self.settings.member_ids:
            try:
                svc = self.manager.get(aid)
            except KeyError:
                self.logger.error("成员账号不存在：%s", aid)
                continue
            if not await svc.is_authorized():
                self.logger.warning("成员 %s 未登录，已跳过。", svc.settings.name)
                continue
            client = await svc.ensure_client()
            try:
                target_ent = await client.get_entity(target_input)
            except Exception as exc:  # noqa: BLE001
                self.logger.error("成员 %s 无法访问目标频道：%s，已跳过。", svc.settings.name, exc)
                continue
            sources = {}
            ok = True
            for si in source_inputs:
                try:
                    ent = await client.get_entity(si)
                    sources[ent.id] = ent
                except Exception as exc:  # noqa: BLE001
                    self.logger.error("成员 %s 无法访问源频道 %r：%s", svc.settings.name, si, exc)
                    ok = False
            if not ok or not sources:
                self.logger.warning("成员 %s 缺少部分频道访问权限，已跳过。", svc.settings.name)
                continue
            self._entities[aid] = {"target": target_ent, "sources": sources}
            available.append(aid)
            self.logger.info("成员就绪：%s", svc.settings.name)
        return available

    async def start(self):
        if self.state == "running":
            return
        problems = self.settings.validate()
        if problems:
            raise RuntimeError("配置不完整：" + "；".join(problems))

        self._entities.clear()
        available = await self._resolve_for_members()
        if not available:
            raise RuntimeError("没有可用的成员账号（需已登录且能访问源/目标频道）。")

        # 初始化 worker 状态
        self.worker_state = {}
        for aid in available:
            self.worker_state[aid] = {
                "next_free_at": 0.0,
                "cooldown_until": 0.0,
                "disabled": False,
                "sent": 0,
                "errors": 0,
                "name": self.manager.get(aid).settings.name,
            }

        # 选监听者：第一个可用成员
        self._listener_id = available[0]
        listener = self.manager.get(self._listener_id)
        client = await listener.ensure_client()
        chats = list(self._entities[self._listener_id]["sources"].values())

        async def _handler(event):  # noqa: ANN001
            if is_video_message(event.message):
                # 命中屏蔽词且开启"丢弃整条" -> 不入队
                if self.settings.content_filter.should_drop(event.message.text):
                    self.logger.info("命中屏蔽词，丢弃整条 [%s:%s]", event.chat_id, event.message.id)
                    return
                key = (event.chat_id, event.message.id)
                if self._mark_seen(key):
                    self.logger.info("检测到新视频 [%s:%s]，加入分发队列", event.chat_id, event.message.id)
                    await self._queue.put(key)

        self._listener_handler = _handler
        client.add_event_handler(_handler, events.NewMessage(chats=chats))
        self.logger.info("监听者：%s；可用发送账号：%d 个", listener.settings.name, len(available))

        self.state = "running"
        self.stats["started_at"] = time.time()
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

        if self.settings.backfill_limit > 0:
            asyncio.create_task(self._safe_backfill())

    async def _safe_backfill(self):
        try:
            limit = self.settings.backfill_limit
            listener = self.manager.get(self._listener_id)
            client = listener.client
            self.logger.info("开始回填历史视频，每个源频道最多 %s 条…", limit)
            for ent in self._entities[self._listener_id]["sources"].values():
                collected = []
                async for msg in client.iter_messages(ent, limit=limit * 5):
                    if is_video_message(msg):
                        if self.settings.content_filter.should_drop(msg.text):
                            continue
                        collected.append((msg.chat_id, msg.id))
                    if len(collected) >= limit:
                        break
                for key in reversed(collected):
                    if self._mark_seen(key):
                        await self._queue.put(key)
            self.logger.info("历史视频已全部入队。")
        except Exception as exc:  # noqa: BLE001
            self.logger.error("历史回填出错：%s", exc)

    def _pick(self, now: float):
        if self.settings.strategy == "round_robin":
            ids = [a for a, s in self.worker_state.items() if not s.get("disabled")]
            if not ids:
                return None, 0.0
            self._rr_index %= len(ids)
            aid = ids[self._rr_index]
            self._rr_index += 1
            st = self.worker_state[aid]
            avail = max(st["next_free_at"], st["cooldown_until"])
            return aid, max(0.0, avail - now)
        return select_worker(self.worker_state, now)

    async def _dispatch_loop(self):
        try:
            while self.state == "running":
                key = await self._queue.get()
                src_chat_id, msg_id = key
                await self._dispatch_one(src_chat_id, msg_id)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.logger.error("分发循环异常：%s", exc)

    async def _dispatch_one(self, src_chat_id, msg_id):
        attempts = 0
        max_attempts = max(1, len([s for s in self.worker_state.values() if not s.get("disabled")]))
        while attempts <= max_attempts:
            aid, wait = self._pick(time.time())
            if aid is None:
                self.logger.error("无可用发送账号，丢弃 [%s:%s]", src_chat_id, msg_id)
                return
            if wait > 0:
                await asyncio.sleep(wait)
            svc = self.manager.get(aid)
            st = self.worker_state[aid]
            try:
                await self._send_via(aid, src_chat_id, msg_id)
                st["sent"] += 1
                st["next_free_at"] = time.time() + self.settings.per_account_delay
                self.stats["forwarded"] += 1
                self.stats["last_video_at"] = time.time()
                self.logger.info("[%s] 已搬运 [%s:%s] -> 目标", st["name"], src_chat_id, msg_id)
                return
            except FloodWaitError as exc:
                st["cooldown_until"] = time.time() + exc.seconds + 1
                self.logger.warning("[%s] 触发限流 %s 秒，切换其它账号", st["name"], exc.seconds)
                attempts += 1
                continue
            except Exception as exc:  # noqa: BLE001
                st["errors"] += 1
                st["next_free_at"] = time.time() + self.settings.per_account_delay
                self.stats["errors"] += 1
                self.logger.error("[%s] 搬运失败 [%s:%s]：%s", st["name"], src_chat_id, msg_id, exc)
                return
        self.logger.error("[%s:%s] 多次尝试后仍未成功（账号均在冷却）", src_chat_id, msg_id)

    async def _send_via(self, account_id, src_chat_id, msg_id):
        svc = self.manager.get(account_id)
        client = svc.client
        if client is None or not client.is_connected():
            client = await svc.ensure_client()
        ent = self._entities.get(account_id, {})
        target = ent.get("target")
        source_ent = ent.get("sources", {}).get(src_chat_id)
        if target is None or source_ent is None:
            raise RuntimeError("该账号缺少频道实体（可能无访问权限）")
        cf = self.settings.content_filter
        if self.settings.mode == "forward":
            await client.forward_messages(target, msg_id, from_peer=source_ent)
        else:
            msg = await client.get_messages(source_ent, ids=msg_id)
            if msg is None:
                raise RuntimeError("无法获取源消息")
            caption = msg.text if self.settings.keep_caption else None
            if caption and cf.active:
                caption = cf.apply(caption)
            await client.send_file(target, file=msg.media, caption=caption)

    async def stop(self):
        if self._dispatcher_task is not None:
            self._dispatcher_task.cancel()
            self._dispatcher_task = None
        if self._listener_id and self._listener_handler:
            try:
                listener = self.manager.get(self._listener_id)
                if listener.client is not None:
                    listener.client.remove_event_handler(self._listener_handler)
            except Exception:  # noqa: BLE001
                pass
        self._listener_handler = None
        if self.state == "running":
            self.logger.info("负载均衡组已停止。")
        self.state = "stopped"

    async def status(self) -> dict:
        members = []
        now = time.time()
        for aid in self.settings.member_ids:
            try:
                svc = self.manager.get(aid)
            except KeyError:
                members.append({"id": aid, "name": "(已删除)", "authorized": False, "sent": 0, "errors": 0, "cooling": False})
                continue
            st = self.worker_state.get(aid, {})
            members.append({
                "id": aid,
                "name": svc.settings.name,
                "authorized": await svc.is_authorized(),
                "sent": st.get("sent", 0),
                "errors": st.get("errors", 0),
                "cooling": st.get("cooldown_until", 0) > now,
            })
        return {
            "id": self.settings.id,
            "name": self.settings.name,
            "state": self.state,
            "listener": (self.manager.get(self._listener_id).settings.name
                         if self._listener_id and self.manager.config.get(self._listener_id) else None),
            "queue_size": self._queue.qsize(),
            "members": members,
            "stats": {
                **self.stats,
                "started_at_str": _fmt(self.stats["started_at"]),
                "last_video_at_str": _fmt(self.stats["last_video_at"]),
            },
            "settings": self.settings.to_dict(),
            "problems": self.settings.validate(),
        }

    def get_logs(self, limit: int = 200) -> List[str]:
        return list(self.log_buffer)[-limit:]

    async def apply_settings(self, data: dict):
        data["id"] = self.settings.id
        self.settings = GroupSettings.from_dict(data)
        self.logger.info("组配置已保存。")


# ------------------------- 多账号管理器 -------------------------
class AccountManager:
    """管理多个 ForwarderService 实例以及全局配置（含面板密码）。"""

    def __init__(self, config: Optional[AppConfig] = None):
        self.config: AppConfig = config or AppConfig.load()
        self.services: Dict[str, ForwarderService] = {}
        for acct in self.config.accounts:
            self.services[acct.id] = ForwarderService(acct)
        self.groups: Dict[str, GroupService] = {}
        for grp in self.config.groups:
            self.groups[grp.id] = GroupService(grp, self)

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
        # 停止包含该账号的运行中负载均衡组
        for grp in self.groups.values():
            if account_id in grp.settings.member_ids and grp.state == "running":
                await grp.stop()
        self.config.remove(account_id)  # 同时从各组成员中剔除
        self.config.save()
        self.services.pop(account_id, None)

    async def apply_settings(self, account_id: str, data: dict) -> None:
        svc = self.get(account_id)
        await svc.apply_settings(data)
        # 同步回 config 并持久化
        self.config.accounts = [s.settings for s in self.services.values()]
        self.config.save()

    # ----- 负载均衡组管理 -----
    def get_group(self, group_id: str) -> GroupService:
        grp = self.groups.get(group_id)
        if grp is None:
            raise KeyError(f"组不存在：{group_id}")
        return grp

    async def list_group_status(self) -> List[dict]:
        return [await g.status() for g in self.groups.values()]

    def add_group(self, name: str = "新负载均衡组") -> GroupService:
        gs = GroupSettings(name=name or "新负载均衡组")
        self.config.add_group(gs)
        self.config.save()
        svc = GroupService(gs, self)
        self.groups[gs.id] = svc
        return svc

    async def remove_group(self, group_id: str) -> None:
        grp = self.groups.get(group_id)
        if grp:
            await grp.stop()
        self.config.remove_group(group_id)
        self.config.save()
        self.groups.pop(group_id, None)

    async def apply_group_settings(self, group_id: str, data: dict) -> None:
        grp = self.get_group(group_id)
        if grp.state == "running":
            raise RuntimeError("请先停止该组再修改配置。")
        await grp.apply_settings(data)
        self.config.groups = [g.settings for g in self.groups.values()]
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
