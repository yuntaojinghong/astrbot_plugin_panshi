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
    chance_summary,
    draw_prize,
    find_item,
    listable_items,
    parse_config,
    parse_penalties,
    render_reward,
)


def _int(value, default: int = 0) -> int:
    """稳妥转 int；失败或为负时回退默认值。"""
    try:
        if isinstance(value, bool):
            return default
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def _slug(name: str) -> str:
    """把名字转成可用作 id 的短串（保留中英文数字）。"""
    import re
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "_", str(name or "").strip())
    return s.strip("_")[:24] or "item"


def _unique_id(given: str, name: str, seen: set[str]) -> str:
    """生成不重复的条目 id。

    用户看不到 id，但它是购买/记录的键，重复会导致两件商品互相覆盖，
    所以这里保证唯一。
    """
    base = given or _slug(name)
    if base not in seen:
        return base
    i = 2
    while f"{base}_{i}" in seen:
        i += 1
    return f"{base}_{i}"


def _parse_switch(arg: str) -> bool | None:
    """把开关参数解析成 True/False；认不出来返回 None（表示「只是查询」）。

    只认明确的开关词。空字符串返回 None，这样「/积分开关」不带参数
    就是查询当前状态，而不是误改成关闭。
    """
    t = str(arg or "").strip().lower()
    if not t:
        return None
    if t in ("on", "开", "开启", "启用", "true", "1", "yes", "是"):
        return True
    if t in ("off", "关", "关闭", "停用", "false", "0", "no", "否"):
        return False
    return None


class ShopHandle(BaseHandle):
    """积分消费与惩罚。"""

    # ------------------------------------------------------------------ #
    #  按群总开关
    # ------------------------------------------------------------------ #

    def points_enabled(self, event=None, group_id=None) -> bool:
        """本群是否启用积分系统。

        总开关就是 ``shop.enable``，但读取时走**按群视角**——于是管理员
        既可以在全局配置里开关，也可以用「/积分开关 off」只关掉某个群。
        关掉后：商城、购买、抽奖、违规扣分全部不生效。
        """
        if event is not None:
            raw = self.cfg_for(event).shop
        elif group_id not in (None, ""):
            raw = self.cfg.for_group(group_id).shop
        else:
            raw = self.cfg.shop
        cfg, _ = parse_config(raw or {})
        return bool(cfg.enable)

    async def toggle(self, event, arg: str = "") -> str:
        """查看或设置本群的积分系统开关。"""
        group_id = self.group_id(event)
        want = _parse_switch(arg)
        current = self.points_enabled(event)

        if want is None:
            state = "开启" if current else "关闭"
            return (f"💎 本群积分系统当前**{state}**\n"
                    f"用法：/积分开关 on|off（只影响本群，不改全局配置）")

        if want == current:
            return f"💎 本群积分系统已经是{'开启' if current else '关闭'}状态，未改动。"

        # 写的是本群的 override，不动全局——这样"只关这个群"才是真的只关这个群
        self.db.set_group_override(str(group_id), "shop", {"enable": bool(want)})
        return (f"✅ 已{'开启' if want else '关闭'}本群积分系统"
                f"（只影响本群）。\n"
                f"{'群友现在可以签到、逛商城、抽奖了。' if want else '商城、抽奖与违规扣分在本群都不再生效。'}")

    async def show_chances(self, event) -> str:
        """展示奖池概率，便于管理员核对配置。"""
        cfg = self._config(event)
        if not cfg.lottery.prizes:
            return "🎰 奖池还没有配置奖品。"
        lines = ["🎰 本群抽奖概率"]
        lines += chance_summary(cfg.lottery)
        lines.append("")
        lines.append(f"每次消耗 {cfg.lottery.cost} 积分，"
                     f"每日 {cfg.lottery.daily_limit or '不限'} 次"
                     + (f"，{cfg.lottery.pity} 次保底" if cfg.lottery.pity else ""))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  配置
    # ------------------------------------------------------------------ #

    def _config(self, event=None):
        """解析商城/抽奖配置（按群视角，允许每群不同）。

        商品与奖池优先取**面板里编辑过的数据**（存在 storage 里），
        没有则回退到 ``_conf_schema.json`` 里的默认值。
        这样既有出厂默认，又能用图形界面自由增删。
        """
        raw = self.cfg_for(event).shop if event is not None else self.cfg.shop
        raw = dict(raw) if isinstance(raw, dict) else {}

        stored_items = self.db.get_shop_items()
        if stored_items is not None:
            raw["items"] = stored_items

        lot_raw = raw.get("lottery")
        lot_raw = dict(lot_raw) if isinstance(lot_raw, dict) else {}
        stored_prizes = self.db.get_prizes()
        if stored_prizes is not None:
            lot_raw["prizes"] = stored_prizes
        raw["lottery"] = lot_raw

        cfg, notes = parse_config(raw)
        for n in notes:
            logger.info(f"[磐石] 商城配置提示：{n}")
        return cfg

    # ------------------------------------------------------------------ #
    #  面板用的编辑接口（商品 / 奖池）
    # ------------------------------------------------------------------ #

    #: 奖品"已中次数"复用 sold 表，加前缀避免与商品 id 撞车
    @staticmethod
    def _prize_key(prize_id: str) -> str:
        return f"prize::{prize_id}"

    def _prize_stock(self, prize_id: str) -> int | None:
        """奖品剩余可中次数。``None`` = 不限。"""
        total = self.db.get_stock(self._prize_key(prize_id))
        if total is None:
            return None
        return total - self.db.sold_count(self._prize_key(prize_id))

    def set_prize_stock(self, prize_id: str, total: int | None) -> None:
        self.db.set_stock(self._prize_key(prize_id), total)

    def _prize_exhausted(self, prize_id: str) -> bool:
        """该奖品是否已经抽完（设了库存且已发满）。"""
        left = self._prize_stock(prize_id)
        return left is not None and left <= 0

    def editable_items(self) -> list[dict]:
        """给面板用的商品列表（已归一化，附带已售/剩余）。"""
        cfg = self._config()
        return [{
            "id": it.item_id, "name": it.name, "cost": it.cost,
            "stock": it.stock, "description": it.description,
            "limit_per_user": it.limit_per_user,
            "limit_per_day": it.limit_per_day,
            "reward": it.reward, "value": it.value,
            "enabled": it.enabled,
            "sold": self.db.sold_count(it.item_id),
            "left": self.stock_left(it),
        } for it in cfg.items]

    def editable_prizes(self) -> list[dict]:
        """给面板用的奖池（含归一化后的实际概率与剩余次数）。"""
        cfg = self._config()
        return [{
            "id": p.prize_id, "name": p.name,
            "chance": p.chance, "weight": p.weight,
            "rare": p.rare, "reward": p.reward, "value": p.value,
            "enabled": p.enabled,
            "stock": self._prize_stock(p.prize_id),
            "won": self.db.sold_count(self._prize_key(p.prize_id)),
        } for p in cfg.lottery.prizes]

    def save_items(self, items: list) -> dict:
        """保存商品列表。返回 ``{ok, problems, items}``。

        校验不通过的条目会被**挡下并说明原因**，而不是静默丢弃——
        否则用户点了保存却少了一件商品，很难发现。
        """
        problems: list[str] = []
        clean: list[dict] = []
        seen: set[str] = set()

        for i, raw in enumerate(items or []):
            if not isinstance(raw, dict):
                problems.append(f"第 {i + 1} 项不是对象")
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                problems.append(f"第 {i + 1} 项缺少商品名")
                continue
            try:
                cost = int(raw.get("cost", 0) or 0)
            except (TypeError, ValueError):
                problems.append(f"「{name}」价格不是数字")
                continue
            if cost < 0:
                problems.append(f"「{name}」价格不能为负")
                continue

            item_id = _unique_id(str(raw.get("id") or "").strip(), name, seen)
            seen.add(item_id)

            stock_raw = raw.get("stock")
            stock = None
            if stock_raw is not None and str(stock_raw).strip() != "":
                try:
                    stock = int(stock_raw)
                except (TypeError, ValueError):
                    stock = None
                if stock is not None and stock < 0:
                    stock = None

            clean.append({
                "id": item_id, "name": name, "cost": cost,
                "description": str(raw.get("description") or ""),
                "stock": stock,
                "limit_per_user": _int(raw.get("limit_per_user"), 0),
                "limit_per_day": _int(raw.get("limit_per_day"), 0),
                "reward": str(raw.get("reward") or "manual"),
                "value": str(raw.get("value") or ""),
                "enabled": bool(raw.get("enabled", True)),
            })

        if problems:
            return {"ok": False, "problems": problems, "items": []}
        self.db.set_shop_items(clean)
        return {"ok": True, "problems": [], "items": clean}

    def save_prizes(self, prizes: list) -> dict:
        """保存奖池。**校验概率总和不得超过 1**，超了直接拒绝并说明。"""
        clean: list[dict] = []
        seen: set[str] = set()
        problems: list[str] = []
        total = 0.0

        for i, raw in enumerate(prizes or []):
            if not isinstance(raw, dict):
                problems.append(f"第 {i + 1} 项不是对象")
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                problems.append(f"第 {i + 1} 项缺少奖品名")
                continue
            try:
                chance = float(raw.get("chance", raw.get("weight", 0)) or 0)
            except (TypeError, ValueError):
                problems.append(f"「{name}」概率不是数字")
                continue
            if chance < 0:
                problems.append(f"「{name}」概率不能为负")
                continue
            if chance > 1:
                problems.append(f"「{name}」概率不能大于 1（当前 {chance}）")
                continue

            enabled = bool(raw.get("enabled", True))
            if enabled:
                total += chance

            prize_id = _unique_id(str(raw.get("id") or "").strip(), name, seen)
            seen.add(prize_id)
            clean.append({
                "id": prize_id, "name": name, "weight": chance,
                "rare": bool(raw.get("rare", False)),
                "reward": str(raw.get("reward") or "points"),
                "value": str(raw.get("value") or "0"),
                "enabled": enabled,
            })

        # 这是用户明确要求的约束：总概率不超过 1
        if total > 1.0 + 1e-9:
            problems.append(
                f"所有奖品的概率加起来是 {total:.4f}，超过了 1。"
                f"要么调低某些奖品，要么留一部分作为「未中奖」概率。")
        if problems:
            return {"ok": False, "problems": problems, "prizes": []}

        self.db.set_prizes(clean)
        # 库存单独存（不是 schema 字段，走 storage）
        for raw in (prizes or []):
            if not isinstance(raw, dict) or "stock" not in raw:
                continue
            nm = str(raw.get("name") or "").strip()
            if not nm:
                continue
            match = next((c for c in clean if c["name"] == nm), None)
            if match is None:
                continue
            s = raw.get("stock")
            try:
                s = None if s is None or str(s).strip() == "" else int(s)
            except (TypeError, ValueError):
                s = None
            if s is not None and s < 0:
                s = None
            self.set_prize_stock(match["id"], s)

        return {"ok": True, "problems": [], "prizes": clean,
                "total_chance": total}

    def reset_shop_data(self) -> None:
        """清空面板编辑过的商品/奖池，回到配置默认值。"""
        self.db.reset_shop_data()

    # ------------------------------------------------------------------ #
    #  文字指令版的增删（不想开面板时用）
    # ------------------------------------------------------------------ #

    async def add_item_from_text(self, text: str) -> str:
        """``/上架 名字 价格 [库存] [发放方式] [内容]``。"""
        parts = str(text or "").split()
        if len(parts) < 2:
            return ("❓ 用法：/上架 <商品名> <价格> [库存] [发放方式] [内容]\n"
                    "例如：/上架 奶茶 50 10\n"
                    "      /上架 专属头衔 200 5 title 学霸\n"
                    "库存省略或填 0 表示不限量。\n"
                    "发放方式：points（积分）/ title（头衔）/ manual（人工发放）")

        name = parts[0]
        try:
            cost = int(parts[1])
        except ValueError:
            return f"❓ 价格「{parts[1]}」不是数字。用法：/上架 奶茶 50 [库存]"
        if cost < 0:
            return "❓ 价格不能为负。"

        stock = None
        idx = 2
        if len(parts) > 2:
            try:
                v = int(parts[2])
                stock = v if v > 0 else None
                idx = 3
            except ValueError:
                pass          # 不是数字就当作发放方式

        reward = parts[idx].lower() if len(parts) > idx else "manual"
        if reward not in ("points", "title", "manual", "action"):
            reward = "manual"
        value = " ".join(parts[idx + 1:]) if len(parts) > idx + 1 else ""

        items = self.editable_items()
        items.append({"name": name, "cost": cost, "stock": stock,
                      "reward": reward, "value": value, "enabled": True})
        result = self.save_items(items)
        if not result.get("ok"):
            return "❌ 上架失败：" + "；".join(result.get("problems") or [])
        stock_txt = "不限量" if stock is None else f"库存 {stock}"
        return f"✅ 已上架「{name}」：{cost} 积分 · {stock_txt}"

    async def remove_item_by_name(self, name: str) -> str:
        key = str(name or "").strip()
        if not key:
            return "❓ 用法：/下架 <商品名>"
        items = self.editable_items()
        hit = [it for it in items
               if it["name"] == key or it["id"] == key]
        if not hit:
            return f"❓ 没找到商品「{key}」。用「/商城」看看有哪些。"
        remain = [it for it in items if it not in hit]
        result = self.save_items(remain)
        if not result.get("ok"):
            return "❌ 下架失败：" + "；".join(result.get("problems") or [])
        return f"✅ 已下架「{hit[0]['name']}」"

    async def add_prize_from_text(self, text: str) -> str:
        """``/奖池 名字 概率 [库存]``。"""
        parts = str(text or "").split()
        if len(parts) < 2:
            return ("❓ 用法：/奖池 <奖品名> <概率> [库存]\n"
                    "例如：/奖池 谢谢参与 0.3\n"
                    "      /奖池 限定头衔 0.01 1\n"
                    "概率是 0~1 的小数；所有奖品概率之和不能超过 1。\n"
                    "库存省略表示不限量（可以无限次被抽中）。")

        name = parts[0]
        try:
            chance = float(parts[1])
        except ValueError:
            return f"❓ 概率「{parts[1]}」不是数字。用法：/奖池 谢谢参与 0.3"
        if chance < 0 or chance > 1:
            return "❓ 概率要在 0~1 之间（0.3 表示 30%）。"

        stock = None
        if len(parts) > 2:
            try:
                v = int(parts[2])
                stock = v if v > 0 else None
            except ValueError:
                return f"❓ 库存「{parts[2]}」不是数字。"

        prizes = self.editable_prizes()
        # 新奖品的发放方式：名字里带"积分"就给积分，否则按人工处理
        reward, value = "manual", ""
        if "积分" in name:
            import re
            m = re.search(r"\d+", name)
            reward, value = "points", (m.group(0) if m else "10")

        prizes.append({"name": name, "chance": chance, "stock": stock,
                       "reward": reward, "value": value, "enabled": True})
        result = self.save_prizes(prizes)
        if not result.get("ok"):
            return "❌ 加奖品失败：" + "；".join(result.get("problems") or [])
        total = result.get("total_chance", 0)
        return (f"✅ 已加奖品「{name}」：{chance * 100:.2f}%"
                + ("（不限量）" if stock is None else f"（限 {stock} 次）")
                + f"\n🎰 当前中奖概率合计 {total * 100:.2f}%，"
                f"未中奖 {(1 - total) * 100:.2f}%")

    async def remove_prize_by_name(self, name: str) -> str:
        key = str(name or "").strip()
        if not key:
            return "❓ 用法：/删奖品 <奖品名>"
        prizes = self.editable_prizes()
        hit = [p for p in prizes if p["name"] == key or p["id"] == key]
        if not hit:
            return f"❓ 没找到奖品「{key}」。用「/奖池」看看有哪些。"
        remain = [p for p in prizes if p not in hit]
        result = self.save_prizes(remain)
        if not result.get("ok"):
            return "❌ 删奖品失败：" + "；".join(result.get("problems") or [])
        total = result.get("total_chance", 0)
        return (f"✅ 已删除奖品「{hit[0]['name']}」\n"
                f"🎰 当前中奖概率合计 {total * 100:.2f}%")

    def penalties(self, event=None) -> dict:
        raw = self.cfg_for(event).shop if event is not None else self.cfg.shop
        raw = raw if isinstance(raw, dict) else {}
        return parse_penalties(raw.get("penalties", raw.get("惩罚", {})))

    # ------------------------------------------------------------------ #
    #  商城
    # ------------------------------------------------------------------ #

    async def show_shop(self, event) -> str:
        if not self.points_enabled(event):
            return "💎 本群积分系统已关闭。（管理员可用「/积分开关 on」开启）"

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
        if not self.points_enabled(event):
            return "💎 本群积分系统已关闭。（管理员可用「/积分开关 on」开启）"

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
        if not self.points_enabled(event):
            return "💎 本群积分系统已关闭。（管理员可用「/积分开关 on」开启）"

        cfg = self._config(event)
        lot = cfg.lottery
        group_id = self.group_id(event)
        user_id = self.sender_id(event)

        today = self.db.draw_count_today(group_id, user_id)
        points = self.db.get_points(group_id, user_id)

        verdict = can_draw(lot, points=points, drawn_today=today)
        if not verdict.ok:
            return f"🎰 {verdict.reason}"

        # 抽完的奖品要从池子里排除：
        # 某个奖品设了「可中次数 N」，表示最多只能被抽中 N 次，之后不再出。
        exhausted = {p.prize_id for p in lot.prizes
                     if p.prize_id and self._prize_exhausted(p.prize_id)}

        # 保底计数：从历史记录里数「连续没中稀有档」
        streak = self._miss_streak(group_id, user_id, lot)

        if lot.cost:
            self.db.add_points(group_id, user_id, -lot.cost)

        prize, by_pity = draw_prize(lot, miss_streak=streak, skip=exhausted)
        name = await get_nickname(event, user_id)

        if prize is None:
            self.db.record_draw(group_id, user_id, "", lot.cost, note="奖池为空")
            # 没有可抽的奖品：退还消耗，并说明是配置问题
            if lot.cost:
                self.db.add_points(group_id, user_id, lot.cost)
            return ("🎰 没有可抽的奖品了（奖池为空或奖品都已抽完）。\n"
                    "本次消耗已退还，请联系管理员补充奖品。")
        if prize.reward == "none":
            # 「谢谢参与」这类
            self.db.record_draw(group_id, user_id, prize.prize_id, lot.cost,
                                note=prize.name)
            left = self.db.get_points(group_id, user_id)
            return (f"🎰 {name} 抽到了「{prize.name}」\n"
                    f"消耗 {lot.cost} 积分，剩余 {left}。下次加油！")

        # 记一次中奖占用（用于奖品库存）
        if prize.prize_id:
            self.db.bump_sold(self._prize_key(prize.prize_id))

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
        if not self.points_enabled(event):
            return "💎 本群积分系统已关闭。（管理员可用「/积分开关 on」开启）"

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
