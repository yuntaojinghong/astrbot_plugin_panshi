"""警告系统：累计违规 -> 自动禁言 / 踢出。"""

from __future__ import annotations

from ..utils import format_duration, get_nickname
from .base_handle import BaseHandle


class WarningHandle(BaseHandle):
    """警告记录与自动升级处理。"""

    async def add_warning(self, event, target_id: str | int | None, reason: str = "") -> str:
        group_id = self.group_id(event)
        if not target_id:
            return "❓ 请 @某人、引用其消息或给出 QQ 号。"
        name = await get_nickname(event, target_id)
        expire = int(self.cfg.warning.get("warn_expire_days", 30))
        count = self.db.add_warning(group_id, target_id, reason or "违规", expire)
        msg = f"⚠️ 已警告 {name}（累计 {count} 次）"
        if reason:
            msg += f"\n原因：{reason}"
        esc = await check_escalation(self.cfg, self.db, event, count, reason, target_id)
        if esc:
            msg += f"\n{esc}"
        return msg

    async def remove_warning(self, event, target_id: str | int | None, count: int = 1) -> str:
        group_id = self.group_id(event)
        if not target_id:
            return "❓ 请指定要撤销警告的人。"
        name = await get_nickname(event, target_id)
        left = self.db.clear_warnings(group_id, target_id, count)
        return f"✅ 已为 {name} 撤销 {count} 次警告，剩余 {left} 次"

    async def query_warning(self, event, target_id: str | int | None) -> str:
        group_id = self.group_id(event)
        target_id = target_id or str(self.sender_id(event))
        name = await get_nickname(event, target_id)
        expire = int(self.cfg.warning.get("warn_expire_days", 30))
        warnings = self.db.get_warnings(group_id, target_id, expire)
        if not warnings:
            return f"✅ {name} 暂无违规记录"
        lines = [f"📋 {name} 的违规记录（共 {len(warnings)} 次）："]
        for i, w in enumerate(warnings[-10:], 1):
            lines.append(f"{i}. [{w.get('time', '')}] {w.get('reason', '违规')}")
        return "\n".join(lines)


async def check_escalation(cfg, db, event, count: int, reason: str = "", target_id=None) -> str | None:
    """根据累计警告数判断是否触发自动禁言 / 踢出。

    Returns:
        触发时的提示文本，否则 None。
    """
    if not cfg.warning.get("warning_enable", True):
        return None

    group_id = event.get_group_id()
    user_id = target_id or event.get_sender_id()

    kick_th = int(cfg.warning.get("warn_kick_threshold", 5))
    ban_th = int(cfg.warning.get("warn_ban_threshold", 3))

    try:
        if kick_th > 0 and count >= kick_th:
            await event.bot.set_group_kick(
                group_id=int(group_id), user_id=int(user_id), reject_add_request=False
            )
            db.clear_warnings(group_id, user_id, 0)
            return f"🚫 警告已达 {count} 次，已自动踢出该成员。"
        if ban_th > 0 and count >= ban_th:
            ban_time = cfg.clamp_ban_time(int(cfg.warning.get("warn_ban_time", 3600)))
            await event.bot.set_group_ban(
                group_id=int(group_id), user_id=int(user_id), duration=ban_time
            )
            return f"🔇 警告已达 {count} 次，已自动禁言 {format_duration(ban_time)}。"
    except Exception:
        return None
    return None
