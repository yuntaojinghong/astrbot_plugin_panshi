"""积分商城 / 抽奖 / 积分惩罚的业务层。

把 :mod:`core.shop` 的纯规则接到存储与协议端上。
"""

from __future__ import annotations

from astrbot.api import logger

from ..utils import get_nickname
from .base_handle import BaseHandle
from .shop import (
    apply_points_floor,
    can_buy,
    can_draw,
    draw_prize,
    find_item,
    listable_items,
    parse_config,
    parse_penalties,
    render_reward,
)


class ShopHandle(BaseHandle):
    """积分消费与惩罚。"""

    # ------------------------------------------------------------------ #
    #  配置
    # ------------------------------------------------------------------ #

    def _config(self, event=None):
        """解析商城/抽奖配置（按群视角，允许每群不同）。"""
        raw = self.cfg_for(event).shop if event is not None else self.cfg.shop
        cfg, notes = parse_config(raw or {})
        for n in notes:
            logger.info(f"[磐石] 商城配置提示：{n}")
        return cfg

    def penalties(self, event=None) -> dict:
        raw = self.cfg_for(event).shop if event is not None else self.cfg.shop
        raw = raw if isinstance(raw, dict) else {}
        return parse_penalties(raw.get("penalties", raw.get("惩罚", {})))

    # ------------------------------------------------------------------ #
    #  商城
    # ------------------------------------------------------------------ #

    async def show_shop(self, event) -> str:
        cfg = self._config(event)
        if not cfg.enable:
            return "🛒 积分商城未开启。（管理员可在配置里打开「积分商城 → 启用」）"
        items = listable_items(cfg)
        if not items:
            return "🛒 商城还没有上架商品。"
        group_id = self.group_id(event)
        user_id = self.sender_id(event)
        points = self.db.get_points(group_id, user_id)

        lines = [f"🛒 积分商城（你有 {points} 积分）", ""]
        for it in items:
            left = self.stock_left(it)
            if it.stock is None:
                stock_txt = "不限量"
            elif left <= 0:
                stock_txt = "已售罄"
            else:
                stock_txt = f"剩 {left}/{it.stock}"
            tag = "（已售罄）" if (it.stock is not None and left <= 0) else ""
            lines.append(f"· {it.name} — {it.cost} 积分{tag}")
            meta = [stock_txt]
            if it.limit_per_user:
                meta.append(f"每人限 {it.limit_per_user}")
            if it.limit_per_day:
                meta.append(f"每日限 {it.limit_per_day}")
            lines.append(f"    {' · '.join(meta)}")
            if it.description:
                lines.append(f"    {it.description}")
        lines.append("")
        lines.append("用「购买 <商品名>」下单；「抽奖」试试手气。")
        return "\n".join(lines)

    def stock_left(self, item) -> int | None:
        """剩余库存。``None`` = 不限量。

        库存以**配置里的总量**为准，数据库里只记卖出了多少。
        这样管理员改配置就能直接调整总量，也不会出现"配置写 2、
        数据库里却是另一个数"的两份真相。
        """
        if item.stock is None:
            return None
        return int(item.stock) - self.db.sold_count(item.item_id)

    async def buy(self, event, key: str) -> str:
        cfg = self._config(event)
        if not cfg.enable:
            return "🛒 积分商城未开启。"

        item = find_item(cfg, key)
        if item is None:
            return f"❓ 没找到商品「{key}」。用「商城」看看有哪些。"

        group_id = self.group_id(event)
        user_id = self.sender_id(event)
        points = self.db.get_points(group_id, user_id)

        verdict = can_buy(
            item,
            points=points,
            stock=self.stock_left(item),
            bought_total=self.db.purchase_count_total(group_id, user_id, item.item_id),
            bought_today=self.db.purchase_count_today(group_id, user_id, item.item_id),
        )
        if not verdict.ok:
            return f"❌ {verdict.reason}"

        # 先扣积分：库存用"已售数量"记，扣错了还能看出来；
        # 反过来先记销量再扣分，扣分失败就会凭空少一件货。
        left_points = self.db.add_points(group_id, user_id, -item.cost)
        self.db.bump_sold(item.item_id)
        name = await get_nickname(event, user_id)

        reward_note, delivered = await self._deliver(
            event, item.reward, item.value, user_id, name)

        self.db.record_purchase(group_id, user_id, item.item_id,
                               item.cost, note=reward_note)

        lines = [
            f"✅ {name} 购买「{item.name}」成功",
            f"花费 {item.cost} 积分，剩余 {left_points}",
        ]
        if reward_note:
            lines.append(f"🎁 {reward_note}")
        if not delivered:
            lines.append("📮 该商品需要管理员人工发放，已记录。")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  抽奖
    # ------------------------------------------------------------------ #

    async def draw(self, event) -> str:
        cfg = self._config(event)
        lot = cfg.lottery
        group_id = self.group_id(event)
        user_id = self.sender_id(event)

        today = self.db.draw_count_today(group_id, user_id)
        points = self.db.get_points(group_id, user_id)

        verdict = can_draw(lot, points=points, drawn_today=today)
        if not verdict.ok:
            return f"🎰 {verdict.reason}"

        # 保底计数：从历史记录里数「连续没中稀有档」
        streak = self._miss_streak(group_id, user_id, lot)

        if lot.cost:
            self.db.add_points(group_id, user_id, -lot.cost)

        prize, by_pity = draw_prize(lot, miss_streak=streak)
        name = await get_nickname(event, user_id)

        if prize is None:
            self.db.record_draw(group_id, user_id, "", lot.cost, note="奖池为空")
            return "🎰 奖池是空的，已退还本次消耗，请联系管理员配置奖品。"
        if prize.reward == "none":
            # 「谢谢参与」这类
            self.db.record_draw(group_id, user_id, prize.prize_id, lot.cost,
                                note=prize.name)
            left = self.db.get_points(group_id, user_id)
            return (f"🎰 {name} 抽到了「{prize.name}」\n"
                    f"消耗 {lot.cost} 积分，剩余 {left}。下次加油！")

        reward_note, delivered = await self._deliver(
            event, prize.reward, prize.value, user_id, name)
        self.db.record_draw(group_id, user_id, prize.prize_id, lot.cost,
                            note=reward_note or prize.name)

        left = self.db.get_points(group_id, user_id)
        lines = [f"🎉 {name} 抽到了「{prize.name}」！"]
        if by_pity:
            lines.append(f"（触发了 {lot.pity} 次保底）")
        if reward_note:
            lines.append(f"🎁 {reward_note}")
        if not delivered:
            lines.append("📮 需要管理员人工发放，已记录。")
        lines.append(f"消耗 {lot.cost} 积分，剩余 {left}")
        return "\n".join(lines)

    def _miss_streak(self, group_id, user_id, lot) -> int:
        """数最近连续多少次没抽到稀有档（用于保底）。"""
        rare_ids = {p.prize_id for p in lot.prizes if p.rare}
        if not rare_ids:
            return 0
        streak = 0
        for rec in reversed(self.db.recent_draws(group_id, user_id, limit=200)):
            if str(rec.get("prize") or "") in rare_ids:
                break
            streak += 1
        return streak

    # ------------------------------------------------------------------ #
    #  记录
    # ------------------------------------------------------------------ #

    async def my_records(self, event) -> str:
        group_id = self.group_id(event)
        user_id = self.sender_id(event)
        points = self.db.get_points(group_id, user_id)
        buys = self.db.recent_purchases(group_id, user_id, limit=5)
        draws = self.db.recent_draws(group_id, user_id, limit=5)

        # 记录里存的是 id，展示时换成商品名/奖品名更好读
        cfg = self._config(event)
        item_names = {it.item_id: it.name for it in cfg.items}
        prize_names = {p.prize_id: p.name for p in cfg.lottery.prizes}

        lines = [f"💎 你的积分：{points}",
                 f"🎰 今日已抽奖 {self.db.draw_count_today(group_id, user_id)} 次"]
        if buys:
            lines.append("")
            lines.append("最近购买：")
            for r in buys:
                iid = str(r.get("item") or "")
                label = item_names.get(iid, iid or "未知商品")
                note = str(r.get("note") or "")
                tail = f"（{note}）" if note and note != label else ""
                lines.append(f"· {label} -{r.get('cost')} 积分{tail}  {r.get('date')}")
        if draws:
            lines.append("")
            lines.append("最近抽奖：")
            for r in draws:
                pid = str(r.get("prize") or "")
                label = str(r.get("note") or "") or prize_names.get(pid, pid or "未中奖")
                lines.append(f"· {label} -{r.get('cost')} 积分  {r.get('date')}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  发放
    # ------------------------------------------------------------------ #

    async def _deliver(self, event, reward: str, value: str,
                       user_id: str, nickname: str) -> tuple[str, bool]:
        """执行发放。

        Returns:
            ``(给用户看的说明, 是否已自动发放)``。
        """
        text = render_reward(reward, value, user_id=user_id, nickname=nickname)
        if reward == "points":
            delta = 0
            try:
                delta = int(str(value or "0").strip() or 0)
            except (TypeError, ValueError):
                delta = 0
            if delta:
                self.db.add_points(self.group_id(event), user_id, delta)
                return f"获得 {delta} 积分", True
            return "", True

        if reward == "title":
            v = str(value or "").strip()
            if not v:
                return "头衔为空，未发放", False
            ok, err = await self.call_api(
                event, "set_group_special_title",
                group_id=self.group_id(event),
                user_id=user_id, special_title=v,
            )
            if ok:
                return f"已设置头衔「{v}」", True
            return f"设置头衔失败（{self.failure_text('set_group_special_title', err)}），请联系管理员", False

        if reward == "action":
            # 自定义动作：不在这里直接执行任意指令（那等于把权限交给配置），
            # 而是把它作为待办告诉管理员，由管理员决定怎么发。
            # 这样即使配置被写坏，也不会变成越权执行入口。
            return f"待执行：{text}", False

        return text, False
