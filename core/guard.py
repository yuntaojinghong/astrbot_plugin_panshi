"""反垃圾防护：违禁词 / 刷屏 / 广告链接 / 复读打断。"""

from __future__ import annotations

import re
import time
from collections import deque

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

from .base_handle import BaseHandle
from ..utils import safe_int

# 内置违禁词（广告类为主，避免误伤）
_BUILTIN_WORDS = [
    "加微信",
    "加V",
    "微信号",
    "扫码进群",
    "点击链接",
    "免费领取",
    "代刷",
    "私聊我",
    "兼职日结",
    "刷单",
]

# 广告链接特征
_AD_PATTERNS = [
    re.compile(r"https?://[^\s]+", re.IGNORECASE),
    re.compile(r"www\.[a-z0-9\-]+\.[a-z]{2,}", re.IGNORECASE),
    re.compile(r"[a-zA-Z0-9\-]+\.(com|cn|net|org|xyz|top|vip|cc)(/|\b)", re.IGNORECASE),
    re.compile(r"\b\d{5,12}\b.*(加|联系|私)", re.IGNORECASE),
]


class GuardHandle(BaseHandle):
    """消息防护处理器。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        # 每用户最近发言时间戳: {(gid, uid): deque[ts]}
        self._msg_times: dict[tuple, deque] = {}
        # 最近消息缓存（用于撤回）: {gid: deque[{"user_id","message_id","text","ts"}]}
        self._recent: dict[str, deque] = {}
        # 头部追踪: {gid: deque[text]}
        self._repeat: dict[str, deque] = {}
        # 上次内存清理时间
        self._last_cleanup: float = 0.0

    # ========== 主入口：每条群消息都过一遍 ==========
    async def inspect(self, event, message_id: str | None = None) -> str | None:
        """检查一条群消息，返回处理结果文本（None 表示无违规）。"""
        self._cleanup()
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        text = self._extract_text(event)

        # 记录消息缓存
        self._cache_message(group_id, user_id, message_id, text)

        # 机器人自身消息豁免：匿名树洞（astrbot_plugin_anon_shudong）等插件
        # 是用 context.send_message 以「bot 自身身份」把内容转述到群里的，
        # 这类消息的 sender 就是机器人自己。若照常处罚，会去禁言机器人自己，
        # 进而把一个无辜的真实用户（甚至是机器人）禁言，造成「匿名身份被一并禁言」。
        if self._is_self_message(event):
            return None

        # 按群配置视图（修复 Issue #1：面板「独立配置」运行时不生效）
        cfg = self.cfg_for(event)

        # 匿名转述内容豁免（按昵称/格式识别）
        if cfg.anon_protect and self._is_anon_relay(text):
            return None

        # 风控白名单豁免（不参与任何防护处罚）
        if self._is_whitelisted(event):
            return None

        # 管理员/超管豁免（可按需调整）
        if self._is_exempt(event):
            return None

        guard = cfg.guard

        # 1. 违禁词
        if guard.get("forbidden_enable", True):
            hit = self._match_forbidden(text, guard)
            if hit:
                return await self._punish(
                    event,
                    reason=f"命中违禁词「{hit}」",
                    ban_time=int(guard.get("forbidden_ban_time", 600)),
                )

        # 2. 广告链接
        if guard.get("ad_enable", True) and self._match_ad(text):
            return await self._punish(
                event,
                reason="发送广告链接",
                ban_time=int(guard.get("forbidden_ban_time", 600)),
            )

        # 3. 刷屏
        if guard.get("spam_enable", True):
            if self._check_spam(group_id, user_id, guard):
                return await self._punish(
                    event,
                    reason="刷屏",
                    ban_time=int(guard.get("spam_ban_time", 300)),
                )

        # 4. 复读打断
        if guard.get("repeat_enable", False):
            if self._check_repeat(group_id, text, guard):
                return f"🔁 检测到复读，已打断。"

        return None

    # ========== 各检测逻辑 ==========
    def _match_forbidden(self, text: str, guard: dict) -> str | None:
        if not text:
            return None
        words = list(guard.get("forbidden_words", []) or [])
        if guard.get("forbidden_builtin", True):
            words += _BUILTIN_WORDS
        for w in words:
            w = str(w)
            if not w:
                continue
            if w.startswith("re:"):
                try:
                    if re.search(w[3:], text, re.IGNORECASE):
                        return w[3:]
                except re.error:
                    continue
            elif w.lower() in text.lower():
                return w
        return None

    @staticmethod
    def _match_ad(text: str) -> bool:
        if not text:
            return False
        # 纯链接直接判定；含链接且带引流词更可疑
        links = sum(1 for p in _AD_PATTERNS[:3] if p.search(text))
        if links >= 1 and any(k in text for k in ("加", "领", "进群", "私", "买", "优惠", "扫码")):
            return True
        return False

    def _check_spam(self, group_id: str, user_id: str, guard: dict) -> bool:
        now = time.time()
        window = int(guard.get("spam_window", 5))
        limit = int(guard.get("spam_count", 5))
        key = (group_id, user_id)
        dq = self._msg_times.setdefault(key, deque())
        dq.append(now)
        # 清理窗口外
        while dq and now - dq[0] > window:
            dq.popleft()
        return len(dq) >= limit

    def _check_repeat(self, group_id: str, text: str, guard: dict) -> bool:
        if not text or len(text) < 2:
            return False
        dq = self._repeat.setdefault(group_id, deque(maxlen=20))
        dq.append(text)
        threshold = int(guard.get("repeat_count", 4))
        # 最近 N 条是否完全相同
        tail = list(dq)[-threshold:]
        if len(tail) >= threshold and len(set(tail)) == 1:
            dq.clear()
            return True
        return False

    # ========== 处罚 ==========
    async def _punish(self, event, reason: str, ban_time: int) -> str:
        group_id = self.group_id(event)
        user_id = self.sender_id(event)
        text = self._extract_text(event)
        cfg = self.cfg_for(event)

        # 撤回最近一条
        await self._recall_last(event, user_id)

        # 记录警告
        expire = int(cfg.warning.get("warn_expire_days", 30))
        warn_count = self.db.add_warning(group_id, user_id, reason, expire)

        # 禁言
        msg = f"⚠️ 检测到 {reason}，已处理。"
        if ban_time > 0:
            ban_time = cfg.clamp_ban_time(ban_time)
            from ..utils import format_duration

            ok, err = await self.call_api(
                event,
                "set_group_ban",
                group_id=group_id,
                user_id=user_id,
                duration=ban_time,
            )
            if ok:
                msg = f"⚠️ {reason}，已撤回并禁言 {format_duration(ban_time)}（累计警告 {warn_count} 次）"

        # 触发警告升级
        try:
            from .warning import check_escalation

            esc = await check_escalation(cfg, self.db, event, warn_count, reason)
            if esc:
                msg += f"\n{esc}"
        except Exception as e:
            logger.warning(f"[磐石] 警告升级检查失败: {e}")

        return msg

    async def _recall_last(self, event, user_id) -> None:
        """撤回该用户最近一条消息（若有缓存）。"""
        group_id = str(event.get_group_id())
        dq = self._recent.get(group_id)
        if not dq:
            return
        for item in reversed(dq):
            if str(item["user_id"]) == str(user_id) and item.get("message_id"):
                mid = safe_int(item["message_id"])
                if mid is None:
                    # 非数字 message_id（如十六进制 ID），本适配器无法撤回，跳过。
                    break
                await self.call_api(event, "delete_msg", message_id=mid)
                break

    # ========== 辅助 ==========
    def _cleanup(self) -> None:
        """定期清理过期的内存缓存，避免长期运行内存缓慢增长。

        每小时最多执行一次；_recent 保留 2 小时（够「撤回/净化」用），
        _msg_times 清掉刷屏窗口外的空记录，_repeat 自带 maxlen 无需处理。
        """
        now = time.time()
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        try:
            for key in list(self._msg_times.keys()):
                dq = self._msg_times[key]
                while dq and now - dq[0] > 3600:
                    dq.popleft()
                if not dq:
                    self._msg_times.pop(key, None)
            for gid in list(self._recent.keys()):
                dq = self._recent[gid]
                while dq and now - dq[0].get("ts", 0) > 7200:
                    dq.popleft()
                if not dq:
                    self._recent.pop(gid, None)
        except Exception:
            pass

    def _is_exempt(self, event) -> bool:
        """管理员及以上豁免。"""
        try:
            from ..utils import PermLevel, get_user_level

            return get_user_level(event, self.cfg.super_admins) >= PermLevel.ADMIN
        except Exception:
            return False

    def _is_whitelisted(self, event) -> bool:
        """风控白名单：命中则豁免防护处罚（如官方客服号）。"""
        try:
            uid = str(event.get_sender_id())
            return bool(uid) and uid in self.cfg.whitelist
        except Exception:
            return False

    @staticmethod
    def _is_self_message(event) -> bool:
        """判断消息是否由机器人自己发出（bot 自身身份）。

        匿名树洞等插件以 ``context.send_message`` 转发内容，群内这条消息的
        发送者即机器人自身。命中时直接豁免，避免误禁机器人/真实用户。
        """
        try:
            self_id = str(event.get_self_id() or "").strip()
            sender_id = str(event.get_sender_id() or "").strip()
            return bool(self_id) and self_id == sender_id
        except Exception:
            return False

    def _is_anon_relay(self, text: str) -> bool:
        """识别匿名树洞的转述内容。

        识别方式（任一命中即视为匿名转述）：
        1. 文本中包含配置的匿名昵称池中的昵称；
        2. 文本匹配匿名树洞的默认转述格式 ``【昵称】：内容`` / ``【匿名倾诉】``。

        Returns:
            True 表示这是匿名转述，应豁免处罚。
        """
        if not text:
            return False
        # 1) 昵称黑名单（配置的匿名昵称）
        for name in self.cfg.anon_nicknames:
            if name and name in text:
                return True
        # 2) 格式识别：【xxx】开头（匿名树洞默认 relay_format）
        if re.match(r"^\s*【[^】]{1,20}】", text):
            return True
        if "【匿名倾诉】" in text or "【匿名转述】" in text:
            return True
        return False

    def _cache_message(self, group_id, user_id, message_id, text) -> None:
        dq = self._recent.setdefault(group_id, deque(maxlen=100))
        dq.append(
            {
                "user_id": str(user_id),
                "message_id": str(message_id) if message_id else None,
                "text": text,
                "ts": time.time(),
            }
        )

    @staticmethod
    def _extract_text(event) -> str:
        try:
            return (event.message_str or "").strip()
        except Exception:
            return ""

    def get_recent(self, group_id: str, limit: int = 50) -> list[dict]:
        """供「净化」指令读取最近消息。"""
        dq = self._recent.get(str(group_id))
        if not dq:
            return []
        return list(dq)[-limit:]
