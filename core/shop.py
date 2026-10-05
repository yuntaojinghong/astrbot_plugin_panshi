"""积分商城与抽奖的规则层。

这一层只做**纯计算**：解析配置、判断能不能买/能不能抽、按权重选奖品、
算该退多少积分。不碰数据库、不调协议端，便于单测覆盖边界情况。

设计要点
--------

* 商品与奖池来自配置（``_conf_schema.json`` 里的对象列表），管理员自己编辑。
  配置写错时**逐项跳过并给出原因**，不让一条坏配置把整个商城打挂。
* 概率用**权重**而不是百分比：权重写起来直观（1 / 3 / 96），
  也不用要求管理员保证总和等于 100。
* 抽奖支持「保底」：连续 N 次没中稀有档时，下一次强制从稀有档里选。
  保底计数存在用户记录里，可跨天累计。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

#: 商品/奖品的标识允许的字符（用于指令里按 id 精确指定）
_ID_RE = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,24}$")


@dataclass
class ShopItem:
    """一件商品。"""

    item_id: str
    name: str
    cost: int
    description: str = ""
    #: 总库存。None = 不限量
    stock: int | None = None
    #: 每人限购。0 = 不限
    limit_per_user: int = 0
    #: 每人每日限购。0 = 不限
    limit_per_day: int = 0
    #: 发放方式：
    #:   points   —— 直接加积分（"buy points" 这类兑换）
    #:   title    —— 设置群头衔（需要机器人有权限）
    #:   action   —— 执行一条管理员配置的指令模板，{user} 会被替换成 QQ 号
    #:   manual   —— 不自动发放，通知管理员人工处理
    reward: str = "manual"
    #: reward=action 时的指令模板；reward=points 时的积分数
    value: str = ""
    #: 是否上架（配置里可以留一些草稿商品）
    enabled: bool = True


@dataclass
class Prize:
    """一个奖品。"""

    prize_id: str
    name: str
    #: 相对权重。越大越容易中。0 表示不参与随机（但可被保底指定）
    weight: int = 1
    #: 稀有档：保底会从这里挑
    rare: bool = False
    reward: str = "points"
    value: str = ""
    enabled: bool = True


@dataclass
class LotteryConfig:
    """抽奖设置。"""

    enable: bool = False
    #: 每次抽奖消耗
    cost: int = 10
    #: 每人每日次数上限。0 = 不限
    daily_limit: int = 3
    #: 连续未中稀有档达到此值后必中。0 = 关闭保底
    pity: int = 10
    prizes: list[Prize] = field(default_factory=list)


@dataclass
class ShopConfig:
    enable: bool = False
    items: list[ShopItem] = field(default_factory=list)
    lottery: LotteryConfig = field(default_factory=LotteryConfig)


# --------------------------------------------------------------------------- #
#  解析
# --------------------------------------------------------------------------- #

def _as_int(value, default: int = 0) -> int:
    try:
        if isinstance(value, bool):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in ("true", "1", "yes", "on", "是", "开", "开启"):
        return True
    if text in ("false", "0", "no", "off", "否", "关", "关闭", ""):
        return False
    return default


def _pick_id(raw: dict, index: int, prefix: str) -> str:
    """取条目标识：优先用配置里的 id，否则按名字，再否则用序号。"""
    for key in ("id", "item_id", "prize_id", "key"):
        v = str(raw.get(key) or "").strip()
        if v and _ID_RE.match(v):
            return v
    name = str(raw.get("name") or raw.get("名称") or "").strip()
    if name and _ID_RE.match(name):
        return name
    return f"{prefix}{index + 1}"


def parse_shop_items(raw_items, *, max_items: int = 100) -> tuple[list[ShopItem], list[str]]:
    """解析商品配置。

    Returns:
        ``(商品列表, 被跳过项的原因)``。坏条目跳过而不是整体失败——
        管理员配错一条不该让整个商城不可用。
    """
    out: list[ShopItem] = []
    skipped: list[str] = []
    if not isinstance(raw_items, (list, tuple)):
        return out, ["商品配置不是列表"]

    for i, raw in enumerate(raw_items):
        if len(out) >= max_items:
            skipped.append(f"超过 {max_items} 件，其余已忽略")
            break
        if not isinstance(raw, dict):
            skipped.append(f"第 {i + 1} 项不是对象")
            continue
        name = str(raw.get("name") or raw.get("名称") or "").strip()
        if not name:
            skipped.append(f"第 {i + 1} 项缺少名称")
            continue
        cost = _as_int(raw.get("cost", raw.get("价格")), -1)
        if cost < 0:
            skipped.append(f"「{name}」价格无效（需 ≥ 0）")
            continue

        stock_raw = raw.get("stock", raw.get("库存"))
        stock = None
        if stock_raw is not None and str(stock_raw).strip() != "":
            stock = _as_int(stock_raw, -1)
            if stock < 0:
                stock = None          # 填错就当不限量，比"卖不出去"友好

        reward = str(raw.get("reward", raw.get("发放", "manual")) or "manual").strip().lower()
        if reward not in ("points", "title", "action", "manual"):
            skipped.append(f"「{name}」发放方式「{reward}」无法识别，按人工处理")
            reward = "manual"

        out.append(ShopItem(
            item_id=_pick_id(raw, i, "item"),
            name=name,
            cost=cost,
            description=str(raw.get("description", raw.get("说明", "")) or ""),
            stock=stock,
            limit_per_user=max(0, _as_int(raw.get("limit_per_user", raw.get("限购")), 0)),
            limit_per_day=max(0, _as_int(raw.get("limit_per_day", raw.get("每日限购")), 0)),
            reward=reward,
            value=str(raw.get("value", raw.get("数值", "")) or ""),
            enabled=_as_bool(raw.get("enabled", raw.get("上架", True)), True),
        ))
    return out, skipped


def parse_prizes(raw_items, *, max_items: int = 100) -> tuple[list[Prize], list[str]]:
    """解析奖池配置。规则同 :func:`parse_shop_items`。"""
    out: list[Prize] = []
    skipped: list[str] = []
    if not isinstance(raw_items, (list, tuple)):
        return out, ["奖池配置不是列表"]

    for i, raw in enumerate(raw_items):
        if len(out) >= max_items:
            skipped.append(f"超过 {max_items} 个奖品，其余已忽略")
            break
        if not isinstance(raw, dict):
            skipped.append(f"第 {i + 1} 项不是对象")
            continue
        name = str(raw.get("name") or raw.get("名称") or "").strip()
        if not name:
            skipped.append(f"第 {i + 1} 项缺少名称")
            continue
        weight = _as_int(raw.get("weight", raw.get("权重")), 1)
        if weight < 0:
            weight = 0
        reward = str(raw.get("reward", raw.get("发放", "points")) or "points").strip().lower()
        if reward not in ("points", "title", "action", "manual", "none"):
            skipped.append(f"「{name}」发放方式「{reward}」无法识别，按积分处理")
            reward = "points"
        out.append(Prize(
            prize_id=_pick_id(raw, i, "prize"),
            name=name,
            weight=weight,
            rare=_as_bool(raw.get("rare", raw.get("稀有", False)), False),
            reward=reward,
            value=str(raw.get("value", raw.get("数值", "0")) or "0"),
            enabled=_as_bool(raw.get("enabled", raw.get("启用", True)), True),
        ))
    return out, skipped


def parse_config(raw: dict) -> tuple[ShopConfig, list[str]]:
    """从配置字典解析出商城 + 抽奖设置。"""
    raw = raw if isinstance(raw, dict) else {}
    notes: list[str] = []

    items, s1 = parse_shop_items(raw.get("items", raw.get("商品", [])))
    notes += [f"商品：{x}" for x in s1]

    lot_raw = raw.get("lottery", raw.get("抽奖", {})) or {}
    if not isinstance(lot_raw, dict):
        lot_raw = {}
        notes.append("抽奖配置不是对象，已按默认处理")
    prizes, s2 = parse_prizes(lot_raw.get("prizes", lot_raw.get("奖池", [])))
    notes += [f"奖池：{x}" for x in s2]

    lottery = LotteryConfig(
        enable=_as_bool(lot_raw.get("enable", lot_raw.get("启用", False)), False),
        cost=max(0, _as_int(lot_raw.get("cost", lot_raw.get("消耗", 10)), 10)),
        daily_limit=max(0, _as_int(lot_raw.get("daily_limit", lot_raw.get("每日次数", 3)), 3)),
        pity=max(0, _as_int(lot_raw.get("pity", lot_raw.get("保底", 10)), 10)),
        prizes=prizes,
    )
    return ShopConfig(
        enable=_as_bool(raw.get("enable", raw.get("启用", False)), False),
        items=items,
        lottery=lottery,
    ), notes


# --------------------------------------------------------------------------- #
#  判定
# --------------------------------------------------------------------------- #

@dataclass
class Check:
    """一次「能不能做」的判定结果。"""

    ok: bool
    reason: str = ""


def find_item(cfg: ShopConfig, key: str) -> ShopItem | None:
    """按 id 或名称找商品（不区分大小写，允许只写名字）。"""
    k = str(key or "").strip().lower()
    if not k:
        return None
    for it in cfg.items:
        if it.item_id.lower() == k or it.name.lower() == k:
            return it
    return None


def listable_items(cfg: ShopConfig) -> list[ShopItem]:
    return [it for it in cfg.items if it.enabled]


def can_buy(item: ShopItem, *, points: int, stock: int | None,
            bought_total: int, bought_today: int) -> Check:
    """判断能否购买。顺序按「先看商品本身，再看用户条件」，提示更自然。"""
    if not item.enabled:
        return Check(False, "该商品已下架")
    if stock is not None and stock <= 0:
        return Check(False, "该商品已售罄")
    if item.limit_per_user and bought_total >= item.limit_per_user:
        return Check(False, f"你已达到该商品的限购上限（{item.limit_per_user} 件）")
    if item.limit_per_day and bought_today >= item.limit_per_day:
        return Check(False, f"你今天买这个已到上限（{item.limit_per_day} 件）")
    if points < item.cost:
        return Check(False, f"积分不足：需要 {item.cost}，你只有 {points}")
    return Check(True)


def can_draw(lot: LotteryConfig, *, points: int, drawn_today: int) -> Check:
    if not lot.enable:
        return Check(False, "抽奖未开启")
    if not [p for p in lot.prizes if p.enabled]:
        return Check(False, "奖池是空的，请管理员先配置奖品")
    if lot.daily_limit and drawn_today >= lot.daily_limit:
        return Check(False, f"今天已经抽过 {drawn_today} 次，明天再来")
    if points < lot.cost:
        return Check(False, f"积分不足：抽一次需要 {lot.cost}，你只有 {points}")
    return Check(True)


# --------------------------------------------------------------------------- #
#  抽奖
# --------------------------------------------------------------------------- #

def draw_prize(lot: LotteryConfig, *, miss_streak: int = 0,
               rng: random.Random | None = None) -> tuple[Prize | None, bool]:
    """抽一个奖品。

    Args:
        miss_streak: 连续未中稀有档的次数（用于保底）。

    Returns:
        ``(奖品, 是否由保底触发)``。奖池为空时返回 ``(None, False)``。
    """
    rnd = rng or random
    pool = [p for p in lot.prizes if p.enabled and p.weight > 0]
    rares = [p for p in pool if p.rare]

    # 保底：连击达标且存在稀有档 → 必出稀有
    if lot.pity > 0 and rares and miss_streak >= lot.pity:
        return rnd.choices(rares, weights=[p.weight for p in rares], k=1)[0], True

    if not pool:
        return None, False
    return rnd.choices(pool, weights=[p.weight for p in pool], k=1)[0], False


def reward_points(prize: Prize | None) -> int:
    """奖品带来的积分变化（正负都可能）。解析不出来按 0。"""
    if prize is None or prize.reward != "points":
        return 0
    return _as_int(prize.value, 0)


def render_reward(reward: str, value: str, *, user_id: str,
                  nickname: str = "") -> str:
    """把发放方式渲染成可直接执行/展示的文本。

    ``action`` 支持 ``{user}``（QQ 号）与 ``{name}``（昵称）占位。
    """
    v = str(value or "")
    if reward == "action":
        return (v.replace("{user}", str(user_id))
                 .replace("{name}", str(nickname or user_id)))
    if reward == "points":
        return f"{_as_int(v, 0)} 积分"
    if reward == "title":
        return f"头衔「{v}」"
    if reward == "manual":
        return "需要管理员人工发放"
    if reward == "none":
        return "谢谢参与"
    return v or "无"


# --------------------------------------------------------------------------- #
#  惩罚联动
# --------------------------------------------------------------------------- #

@dataclass
class PenaltyRule:
    """一条「违规 → 扣分（可叠加禁言）」的规则。"""

    reason: str
    #: 扣多少积分（正数表示扣除）
    points: int = 0
    #: 是否同时禁言
    ban: bool = True
    #: 禁言时长由调用方传入的 ban_time 决定
    enabled: bool = True


def parse_penalties(raw) -> dict[str, PenaltyRule]:
    """解析每条风控原因的扣分配置。

    配置形如::

        {"刷屏": {"points": 20, "ban": true},
         "违禁词": {"points": 50}}

    键是风控原因（与 guard 里传入的 reason 一致）。
    """
    out: dict[str, PenaltyRule] = {}
    if not isinstance(raw, dict):
        return out
    for reason, spec in raw.items():
        key = str(reason or "").strip()
        if not key:
            continue
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            out[key] = PenaltyRule(reason=key, points=max(0, int(spec)))
            continue
        if not isinstance(spec, dict):
            continue
        out[key] = PenaltyRule(
            reason=key,
            points=max(0, _as_int(spec.get("points", spec.get("扣分")), 0)),
            ban=_as_bool(spec.get("ban", spec.get("禁言", True)), True),
            enabled=_as_bool(spec.get("enabled", spec.get("启用", True)), True),
        )
    return out


def apply_points_floor(current: int, delta: int, *, floor: int = 0) -> tuple[int, int]:
    """计算扣分后的积分，并施加下限。

    扣分**不允许把积分扣成负数**（默认下限 0）：
    负积分会让排行榜和商城都变得难以解释，用户也容易觉得是 bug。

    Returns:
        ``(扣减后的积分, 实际扣掉的数量)``。
    """
    cur = int(current)
    target = cur + int(delta)
    if target < floor:
        target = floor
    return target, cur - target
