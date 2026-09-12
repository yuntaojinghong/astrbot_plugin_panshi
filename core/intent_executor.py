"""意图执行器：把 IntentParser 的结构化结果落到实际群管操作。

负责：
- target 解析（reply / at / QQ号 / 名字模糊匹配 / recent_offender）
- 高风险操作的确认机制
- 调用 NormalHandle / WarningHandle 执行
"""

from __future__ import annotations

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

from ..utils import get_ats, parse_duration


class IntentExecutor:
    """执行智能识别的结果。"""

    def __init__(self, config, storage, context, normal, warning):
        self.cfg = config
        self.db = storage
        self.ctx = context
        self.normal = normal
        self.warning = warning
        # 待确认操作: {(gid, uid): {"intent": dict, "expire": ts}}
        self._pending_confirm: dict[tuple, dict] = {}

    # ========== 入口 ==========
    async def execute(self, event, intent: dict) -> str | None:
        action = intent.get("action", "none")
        if action == "none":
            return None

        # 解析目标
        target = await self._resolve_target(event, intent)

        # 高风险操作需确认
        if action in ("kick", "block") and self.cfg.smart.get("smart_confirm_dangerous", True):
            return self._ask_confirm(event, intent, target)

        return await self._dispatch(event, intent, target)

    async def confirm(self, event, reply_text: str) -> str | None:
        """处理用户对高风险操作的确认回复。"""
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        key = (group_id, user_id)
        pending = self._pending_confirm.get(key)
        if not pending:
            return None

        import time

        if time.time() > pending["expire"]:
            self._pending_confirm.pop(key, None)
            return None

        answer = (reply_text or "").strip().lower()
        if answer in ("确认", "确定", "是", "yes", "y", "ok", "同意"):
            self._pending_confirm.pop(key, None)
            return await self._dispatch(event, pending["intent"], pending["target"])
        elif answer in ("取消", "不用", "否", "no", "n", "算了"):
            self._pending_confirm.pop(key, None)
            return "✅ 已取消该操作。"
        return None

    def has_pending(self, event) -> bool:
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        return (group_id, user_id) in self._pending_confirm

    # ========== 目标解析 ==========
    async def _resolve_target(self, event, intent: dict) -> str | None:
        target = intent.get("target")
        if not target:
            return None
        target = str(target).strip()

        if target == "reply":
            return self._reply_sender(event)
        if target == "at":
            ats = get_ats(event)
            return ats[0] if ats else None
        if target == "recent_offender":
            return self.ctx.find_recent_offender(str(event.get_group_id()))
        if target.isdigit():
            return target

        # 名字模糊匹配群成员
        matched = await self._match_by_name(event, target)
        return matched or target

    async def _match_by_name(self, event, name: str) -> str | None:
        """在群成员中按昵称/名片模糊匹配。"""
        try:
            ok, members = await self.normal.call_api(
                event, "get_group_member_list", group_id=int(event.get_group_id())
            )
            if not ok or not isinstance(members, list):
                return None
            name = name.strip()
            # 精确匹配优先
            for m in members:
                for field in ("card", "nickname"):
                    if m.get(field) and str(m[field]) == name:
                        return str(m.get("user_id"))
            # 包含匹配
            for m in members:
                for field in ("card", "nickname"):
                    if m.get(field) and name and name in str(m[field]):
                        return str(m.get("user_id"))
        except Exception:
            pass
        return None

    @staticmethod
    def _reply_sender(event) -> str | None:
        try:
            from astrbot.api.message_components import Reply

            for seg in event.get_messages():
                if isinstance(seg, Reply):
                    sid = getattr(seg, "sender_id", None)
                    if sid:
                        return str(sid)
        except Exception:
            pass
        return None

    # ========== 确认机制 ==========
    def _ask_confirm(self, event, intent: dict, target: str | None) -> str:
        import time

        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        self._pending_confirm[(group_id, user_id)] = {
            "intent": intent,
            "target": target,
            "expire": time.time() + 60,
        }
        action_name = {"kick": "踢出", "block": "踢出并拉黑"}.get(intent.get("action"), "操作")
        reason = intent.get("reason", "")
        tip = f"{action_name} {target or '（未明确对象）'}"
        if reason:
            tip += f"，原因：{reason}"
        return f"⚠️ 即将执行高风险操作：{tip}\n请在 60 秒内回复「确认」执行，或「取消」放弃。"

    # ========== 分发执行 ==========
    async def _dispatch(self, event, intent: dict, target: str | None) -> str | None:
        action = intent.get("action")

        if action == "ban":
            seconds = self._duration(event, intent, self.cfg.default_ban_time)
            return await self.normal.set_ban(event, target, seconds)
        if action == "unban":
            return await self.normal.cancel_ban(event, target)
        if action == "kick":
            return await self.normal.kick(event, target, reject=False, reason=intent.get("reason", ""))
        if action == "block":
            return await self.normal.kick(event, target, reject=True, reason=intent.get("reason", ""))
        if action == "recall":
            return await self.normal.recall(event, count=int(intent.get("count", 1) or 1))
        if action == "purge":
            return await self.normal.purge(event, count=int(intent.get("count", 30) or 30))
        if action == "whole_ban":
            return await self.normal.whole_ban(event, enable=bool(intent.get("enable", True)))
        if action == "warn":
            return await self.warning.add_warning(event, target, intent.get("reason", ""))
        if action == "query_warn":
            return await self.warning.query_warning(event, target)
        if action == "set_card":
            return await self.normal.set_card(event, target, str(intent.get("name", "")))
        if action == "set_title":
            return await self.normal.set_special_title(event, target, str(intent.get("title", "")))
        if action == "set_admin":
            return await self.normal.set_admin(event, target, enable=True)
        if action == "unset_admin":
            return await self.normal.set_admin(event, target, enable=False)
        if action == "notice":
            return await self.normal.send_notice(event, str(intent.get("content", "")))
        if action == "set_name":
            return await self.normal.set_group_name(event, str(intent.get("name", "")))
        if action == "essence":
            return await self.normal.set_essence(event, enable=bool(intent.get("enable", True)))
        return None

    def _duration(self, event, intent: dict, default: int) -> int:
        dur = intent.get("duration")
        if dur is None:
            return default
        if isinstance(dur, (int, float)):
            return int(dur)
        return parse_duration(str(dur), default)
