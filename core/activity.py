"""群活跃：签到 / 积分 / 排行榜 / 群员概览。"""

from __future__ import annotations

import random

from ..utils import get_nickname
from .base_handle import BaseHandle


class ActivityHandle(BaseHandle):
    """签到与积分系统。"""

    async def checkin(self, event) -> str:
        group_id = self.group_id(event)
        user_id = self.sender_id(event)

        if self.db.has_checked_in(group_id, user_id):
            points = self.db.get_points(group_id, user_id)
            return f"📅 你今天已经签到过啦，当前积分 {points}"

        base = int(self.cfg.activity.get("checkin_points", 10))
        bonus_max = int(self.cfg.activity.get("checkin_random_bonus", 10))
        bonus = random.randint(0, bonus_max) if bonus_max > 0 else 0
        total = base + bonus

        new_points = self.db.add_points(group_id, user_id, total)
        self.db.set_checkin(group_id, user_id)

        name = await get_nickname(event, user_id)
        return (
            f"✅ {name} 签到成功！\n"
            f"本次获得 {total} 积分（基础 {base} + 随机 {bonus}）\n"
            f"当前总积分：{new_points}"
        )

    async def query_points(self, event, target_id: str | int | None = None) -> str:
        group_id = self.group_id(event)
        user_id = str(target_id) if target_id else str(self.sender_id(event))
        points = self.db.get_points(group_id, user_id)
        name = await get_nickname(event, user_id)
        return f"💎 {name} 当前积分：{points}"

    async def rank_points(self, event, limit: int = 10) -> str:
        group_id = self.group_id(event)
        rows = self.db.top_points(group_id, limit)
        if not rows:
            return "📊 本群还没有积分记录。"
        lines = ["💎 本群积分排行："]
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, pts) in enumerate(rows):
            name = await get_nickname(event, uid)
            prefix = medals[i] if i < len(medals) else f"{i + 1}."
            lines.append(f"{prefix} {name} — {pts}")
        return "\n".join(lines)

    async def rank_messages(self, event, limit: int = 10) -> str:
        group_id = self.group_id(event)
        rows = self.db.top_messages(group_id, limit)
        rows = [r for r in rows if r[1] > 0]
        if not rows:
            return "📊 本群还没有发言统计。"
        lines = ["🗣️ 本群发言排行："]
        medals = ["🥇", "🥈", "🥉"]
        for i, (uid, cnt) in enumerate(rows):
            name = await get_nickname(event, uid)
            prefix = medals[i] if i < len(medals) else f"{i + 1}."
            lines.append(f"{prefix} {name} — {cnt} 条")
        return "\n".join(lines)
