"""群积分：签到 / 积分 / 排行榜 / 群员概览。"""

from __future__ import annotations

import random
import time

from ..utils import get_nickname
from .base_handle import BaseHandle
from .shop import apply_points_floor


class ActivityHandle(BaseHandle):
    """签到与积分系统。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        #: 发言得分的冷却表：{(group_id, user_id): ts}
        self._chat_cd: dict[tuple, float] = {}

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
        rnd_bonus = random.randint(0, bonus_max) if bonus_max > 0 else 0

        # 连签加成：连续签到每多一天多给一份，**必须封顶**——
        # 不封顶的话连签一个月会变成一个谁都没预期的数字。
        prev_date = self.db.get_checkin_date(group_id, user_id)
        yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        streak = self.db.get_streak(group_id, user_id) + 1 \
            if prev_date == yesterday else 1
        step = max(0, int(activity.get("checkin_streak_bonus", 0) or 0))
        cap = max(0, int(activity.get("checkin_streak_bonus_cap", 0) or 0))
        streak_bonus = min(step * (streak - 1), cap) if step else 0

        # 早鸟奖：当天**第一个**签到的额外奖励。
        # 用 claim_daily 在锁内抢占名额，避免两人同时签到都拿到早鸟。
        first_bonus = 0
        first_cfg = max(0, int(activity.get("checkin_first_bonus", 0) or 0))
        if first_cfg > 0 and self.db.claim_daily(group_id, "checkin_first"):
            first_bonus = first_cfg

        total = base + rnd_bonus + streak_bonus + first_bonus
        new_points = self.db.add_points(group_id, user_id, total)
        self.db.set_checkin(group_id, user_id)
        self.db.set_streak(group_id, user_id, streak)

        name = await get_nickname(event, user_id)
        parts = [f"基础 {base}"]
        if rnd_bonus:
            parts.append(f"随机 {rnd_bonus}")
        if streak_bonus:
            parts.append(f"连签 {streak} 天 +{streak_bonus}")
        if first_bonus:
            parts.append(f"早鸟 +{first_bonus}")
        head = f"🐦 {name} 是今天第一个签到的！" if first_bonus \
            else f"✅ {name} 签到成功！"
        lines = [
            head,
            f"本次获得 {total} 积分（{' + '.join(parts)}）",
            f"当前总积分：{new_points}",
        ]
        if streak > 1:
            lines.append(f"🔥 已连续签到 {streak} 天")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  发言得积分
    # ------------------------------------------------------------------ #
    async def award_chat_points(self, event, text: str, activity: dict) -> int:
        """给「有效发言」发积分，返回实际加的分（``0`` = 没加）。

        **限流是硬要求**：没有冷却和每日上限的"发言给分"，等于把积分系统
        直接交给刷屏脚本——一次连发就能把分刷满。所以这里四道闸：
        开关 / 最短字数 / 冷却 / 每日上限。

        另有两条一次性奖励：每日首次发言、新人礼包（入群后第一次发言）。
        """
        if not bool(activity.get("chat_points_enable", False)):
            return 0
        value = int(activity.get("chat_points_value", 1) or 0)
        if value <= 0:
            return 0
        body = str(text or "").strip()
        min_len = int(activity.get("chat_points_min_len", 4) or 0)
        if min_len and len(body) < min_len:
            return 0

        gid = str(self.group_id(event))
        uid = str(self.sender_id(event))
        now = time.time()

        cooldown = int(activity.get("chat_points_cooldown", 60) or 0)
        if cooldown > 0 and now - self._chat_cd.get((gid, uid), 0.0) < cooldown:
            return 0

        cap = int(activity.get("chat_points_daily_cap", 50) or 0)
        if cap > 0 and self.db.daily_count(gid, uid, "chat_points") >= cap:
            return 0

        got = value
        first = int(activity.get("chat_first_bonus", 0) or 0)
        if first > 0 and self.db.claim_daily(gid, "chat_first", uid):
            got += first

        # 新人礼包：入群后的**第一次发言**给一份额外奖励，只给一次。
        # 用「累计发言 <= 1」判断"刚来"，比记入群时间简单，也不需要额外事件。
        newbie = int(activity.get("newbie_bonus", 0) or 0)
        if newbie > 0 and self.db.get_message_count(gid, uid) <= 1 \
                and self.db.claim_once(gid, "newbie", uid):
            got += newbie

        # 上限按**积分**算，不按次数：不夹紧的话「上限 50、每笔 +20」
        # 到第三笔就会变成 60，用户看到的数字和配置对不上。
        cap = int(activity.get("chat_points_daily_cap", 50) or 0)
        if cap > 0:
            room = cap - self.db.daily_count(gid, uid, "chat_points")
            if room <= 0:
                return 0
            got = min(got, room)

        self._chat_cd[(gid, uid)] = now
        self.db.bump_daily_count(gid, uid, "chat_points", got)
        self.db.add_points(gid, uid, got)
        return got

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
