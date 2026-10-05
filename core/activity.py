"""群积分：签到 / 积分 / 排行榜 / 群员概览。"""

from __future__ import annotations

import random

from ..utils import get_nickname
from .base_handle import BaseHandle
from .shop import apply_points_floor


class ActivityHandle(BaseHandle):
    """签到与积分系统。"""

    async def checkin(self, event) -> str:
        group_id = self.group_id(event)
        user_id = self.sender_id(event)

        # 按群视角取配置：面板「独立配置」里的签到积分必须真的生效。
        activity = self.cfg_for(event).activity

        # 「启用签到」必须真的能把它关掉。
        #
        # 回归背景：`checkin_enable` 以前只被面板/自检读去「显示状态」，
        # 签到逻辑本身从不看它——于是关掉开关后 /签到 照样加分，
        # 是个典型的「假开关」：面板上写着已关闭，功能却还在跑。
        if not bool(activity.get("checkin_enable", True)):
            return "📅 本群未开启签到。管理员可在「群积分 → 启用签到」里打开。"

        if self.db.has_checked_in(group_id, user_id):
            points = self.db.get_points(group_id, user_id)
            return f"📅 你今天已经签到过啦，当前积分 {points}"

        base = int(activity.get("checkin_points", 10))
        bonus_max = int(activity.get("checkin_random_bonus", 10))
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

    # ------------------------------------------------------------------ #
    #  加/扣积分（管理员手动调整）
    # ------------------------------------------------------------------ #

    async def adjust_points(self, event, target_id: str | None, delta: int,
                            reason: str = "") -> str:
        """给某人加（delta>0）或扣（delta<0）积分。

        回执里带上变动前后的数值，管理员一眼能确认改对了没有；
        扣分同样不会扣成负数（与违规扣分保持一致的语义）。
        """
        group_id = self.group_id(event)
        if not target_id:
            return "❓ 不知道要给谁加减积分。用「@某人」或引用他的消息。"
        if delta == 0:
            return "❓ 数量是 0，没有变化。"

        user_id = str(target_id)
        name = await get_nickname(event, user_id)
        cur = self.db.get_points(group_id, user_id)

        if delta > 0:
            new_points = self.db.add_points(group_id, user_id, delta)
            tail = f"（{reason}）" if reason else ""
            return (f"✅ 已给 {name} 加 {delta} 积分{tail}\n"
                    f"💎 {cur} → {new_points}")

        new_points, actual = apply_points_floor(cur, delta)
        if actual <= 0:
            return f"❓ {name} 当前积分为 {cur}，没有可扣的。"
        self.db.add_points(group_id, user_id, -actual)
        tail = f"（{reason}）" if reason else ""
        if actual < abs(delta):
            return (f"✅ 已扣 {name} {actual} 积分{tail}（积分不够扣满 "
                    f"{abs(delta)}）\n💎 {cur} → {new_points}")
        return (f"✅ 已扣 {name} {actual} 积分{tail}\n"
                f"💎 {cur} → {new_points}")

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
