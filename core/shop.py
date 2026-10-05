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
    #: 中奖概率（0~1 的小数）。所有奖品之和 ≤ 1；
    #: 不足 1 的部分自动成为「未中奖」的概率。
    #: 也兼容把权重写成 60 这种整数——解析时会统一归一化成概率。
    weight: float = 0.0
    #: 稀有档：保底会从这里挑
    rare: bool = False
    reward: str = "points"
    value: str = ""
    enabled: bool = True
    #: 归一化后的实际概率（解析时填），用于面板展示
    chance: float = 0.0


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


def _as_float(value, default: float = 0.0) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_prizes(raw_items, *, max_items: int = 100) -> tuple[list[Prize], list[str]]:
    """解析奖池配置。

    **概率规则**：每个奖品的 ``weight`` 就是它的中奖概率，取值 0~1。
    所有奖品概率之和**不得超过 1**；不足 1 的剩余部分自动成为「未中奖」的概率。

    例如：``0.05 + 0.02 + 0.93 = 1.0`` → 全部覆盖；
    ``0.01 + 0.01 = 0.02`` → 剩下 98% 是没中奖。

    为了兼容手写配置，也接受把权重写成 ``60`` 这种整数：
    当所有值都 > 1 时按**权重**处理，自动归一化到总和为 1
    （此时不再有"未中奖"余量，因为权重已经铺满整个区间）。
    """
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

        raw_w = raw.get("weight", raw.get("概率", raw.get("权重")))
        w = _as_float(raw_w, -1.0)
        if w < 0:
            skipped.append(f"「{name}」概率无效（需为 0~1 的小数）")
            continue

        reward = str(raw.get("reward", raw.get("发放", "points")) or "points").strip().lower()
        if reward not in ("points", "title", "action", "manual", "none"):
            skipped.append(f"「{name}」发放方式「{reward}」无法识别，按积分处理")
            reward = "points"
        out.append(Prize(
            prize_id=_pick_id(raw, i, "prize"),
            name=name,
            weight=w,
            rare=_as_bool(raw.get("rare", raw.get("稀有", False)), False),
            reward=reward,
            value=str(raw.get("value", raw.get("数值", "0")) or "0"),
            enabled=_as_bool(raw.get("enabled", raw.get("启用", True)), True),
        ))

    if not out:
        return out, skipped

    active = [p for p in out if p.enabled]
    total = sum(float(p.weight) for p in active)

    # 全部 ≥ 1 → 当作权重，归一化到 1
    #
    # 注意是 **≥ 1** 而不是 > 1：权重写 1 很常见（「其余都是 1」），
    # 用 > 1 会把这种配置漏掉，然后落到"总和超过 1"的分支里被误删奖品。
    #
    # 兼容手写配置里 "0.3" 这种字符串：先统一成 float 再判断，
    # 否则字符串比较既不报错也不生效，最后表现为"概率完全没起作用"。
    if total > 1.0 and all(float(p.weight) >= 1.0 for p in active):
        for p in active:
            p.chance = float(p.weight) / total
        for p in out:
            if not p.enabled:
                p.chance = 0.0
        skipped.append(
            f"概率写成了权重（合计 {total:g}），已按比例归一化："
            + "、".join(f"{p.name} {p.chance * 100:.1f}%" for p in active))
        return out, skipped

    # 标准路径：每个值就是概率，总和不得超过 1
    if total > 1.0 + 1e-9:
        # 超了就按顺序累加，越界的那一项起全部剔除——保留能用的，
        # 而不是让整个奖池失效
        acc = 0.0
        dropped = []
        for p in active:
            w = float(p.weight)
            if acc + w > 1.0 + 1e-9:
                p.enabled = False
                p.chance = 0.0
                dropped.append(p.name)
                continue
            acc += w
            p.chance = w
        skipped.append(
            f"奖品概率之和为 {total:.4f}，超过 1；已剔除超出的"
            f"「{'、'.join(dropped)}」，保留部分合计 {acc:.4f}")
        return out, skipped

    for p in active:
        p.chance = float(p.weight)
    miss = max(0.0, 1.0 - total)
    if miss > 1e-9:
        skipped.append(f"奖品概率合计 {total:.4f}，剩余 {miss:.4f} 为未中奖概率")
    return out, skipped


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
    real = [p for p in lot.prizes if p.enabled and p.chance > 0]
    if not real:
        return Check(False, "奖池是空的（或所有奖品概率都是 0），请管理员先配置奖品")
    if lot.daily_limit and drawn_today >= lot.daily_limit:
        return Check(False, f"今天已经抽过 {drawn_today} 次，明天再来")
    if points < lot.cost:
        return Check(False, f"积分不足：抽一次需要 {lot.cost}，你只有 {points}")
    return Check(True)


def chance_summary(lot: LotteryConfig, *, limit: int = 8) -> list[str]:
    """把奖池概率渲染成人能读的清单，供「抽奖概率」指令与面板展示。"""
    lines: list[str] = []
    active = [p for p in lot.prizes if p.enabled]
    for p in active[:limit]:
        mark = "★" if p.rare else " "
        lines.append(f"{mark} {p.name} — {p.chance * 100:.2f}%")
    if len(active) > limit:
        lines.append(f"… 另有 {len(active) - limit} 个")
    total = sum(p.chance for p in active)
    miss = max(0.0, 1.0 - total)
    lines.append(f"合计中奖概率 {total * 100:.2f}%"
                 + (f"，未中奖 {miss * 100:.2f}%" if miss > 1e-9 else "（已铺满）"))
    return lines


# --------------------------------------------------------------------------- #
#  抽奖
# --------------------------------------------------------------------------- #

def draw_prize(lot: LotteryConfig, *, miss_streak: int = 0,
               rng: random.Random | None = None) -> tuple[Prize | None, bool]:
    """抽一个奖品。

    Args:
        miss_streak: 连续未中稀有档的次数（用于保底）。

    Returns:
        ``(奖品, 是否由保底触发)``。

        ``奖品为 None`` 有两种含义，调用方要分清：

        * 奖池里一个可用奖品都没有 → 应当**退还本次消耗**（不可能中奖）
        * 概率合计不足 1，这次落在那段余量里 → 正常的「没抽中」

        这个区分很重要：前者是配置问题，不该收钱；后者是玩家运气问题。
        为区分两者，未中奖时返回一个 ``reward="none"`` 的占位奖品而不是 None——
        见下面 ``miss_placeholder``。真正"无奖池"才返回 None。
    """
    rnd = rng or random
    pool = [p for p in lot.prizes if p.enabled and p.chance > 0]
    rares = [p for p in pool if p.rare]

    # 没有任何可用奖品 → 配置问题
    if not pool:
        return None, False

    # 保底：连击达标且存在稀有档 → 必出稀有
    if lot.pity > 0 and rares and miss_streak >= lot.pity:
        return rnd.choices(rares, weights=[p.chance for p in rares], k=1)[0], True

    total = sum(p.chance for p in pool)
    # 落在"未中奖"余量里 → 返回占位奖品，让调用方知道这是正常空手
    if total < 1.0 and rnd.random() > total:
        return miss_placeholder(), False

    return rnd.choices(pool, weights=[p.chance for p in pool], k=1)[0], False


def miss_placeholder() -> Prize:
    """表示「这次没中奖」的占位奖品。

    用占位对象而不是 ``None``，是为了把「概率余量导致空手」与
    「奖池为空（配置错误）」区分开——后者要退还消耗。
    """
    return Prize(prize_id="", name="未中奖", weight=0.0, reward="none",
                 value="", enabled=True, chance=0.0)


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
    """解析「违规 → 扣多少积分」的配置。

    接受**两种**写法：

    1. 列表（推荐，也是面板里的标准写法）::

        [{"reason": "刷屏", "points": 20, "ban": true}, ...]

    2. 字典（旧写法，保留兼容）::

        {"刷屏": {"points": 20, "ban": true}, "违禁词": 50}

    为什么推荐列表：AstrBot 的配置解析器遇到 ``type: object`` 会要求同时给出
    ``items`` 子结构（用于描述 dict 里每个值的字段），少给就抛
    ``KeyError: 'items'`` 并**导致整个插件加载失败**。
    列表没有这个约束，和其它配置项（商品、奖池）写法也一致。

    键是风控原因，与 ``guard`` 里传入的 reason 一致。
    """
    out: dict[str, PenaltyRule] = {}

    def add(reason: str, spec) -> None:
        key = str(reason or "").strip()
        if not key:
            return
        # 简写成数字：{"违禁词": 50} 或 "违禁词=50"
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            out[key] = PenaltyRule(reason=key, points=max(0, int(spec)))
            return
        if not isinstance(spec, dict):
            return
        out[key] = PenaltyRule(
            reason=key,
            points=max(0, _as_int(spec.get("points", spec.get("扣分")), 0)),
            ban=_as_bool(spec.get("ban", spec.get("禁言", True)), True),
            enabled=_as_bool(spec.get("enabled", spec.get("启用", True)), True),
        )

    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, dict):
                continue
            add(str(item.get("reason") or item.get("原因") or ""), item)
        return out

    if isinstance(raw, dict):
        # 旧写法：{"刷屏": {...}}
        for reason, spec in raw.items():
            add(reason, spec)
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
