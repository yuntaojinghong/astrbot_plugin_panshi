"""三公五：机器人坐庄的积分牌局。

牌型规则按传统「三公五」：每人 3 张牌比大小，牌型分四档
（豹子 > 顺子 > 对子 > 散牌），同档比点数。不看花色，
所以「同花顺」那种斗牛牌型在这里**不存在**。

三条红线（与 :mod:`.games` 保持一致）：

1. **默认关闭**。对赌是玩家之间的事，机器人只提供场地与发牌，
   必须由管理员显式开启。
2. **双层限额**：单局封顶（``sanguanwu_max_bet``）+ 每人每日净亏上限
   （``sanguanwu_daily_loss``）。只做单局上限不够——每天输一小笔
   也能把积分磨光。
3. **牌局状态有 TTL**。没人操作的桌子会过期自动清算，不留在内存里
   变成僵尸桌；重启插件也一并作废（局是短会话，不必落盘）。

设计上的一个刻意选择：**机器人不参与赢钱**。它只是庄家（发牌、收注、
算牌、播报），赢家通吃是**闲家之间**的账，机器人一分不抽。
这样避免"和机器人赌"变成变相的印钞机，也让输赢更清晰。

注在**入座时就扣**（不是在结算时扣）：如果先发牌、结算时才扣，中途
出异常就会留下一桌白送的积分。结算时只做「赢家拿走底池」这一件事。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from astrbot.api import logger

from ..utils import get_nickname
from .base_handle import BaseHandle

#: 牌面点数：2~10 记本数，J=11 Q=12 K=13 A=14
_RANKS = "23456789TJQKA"

#: 牌型等级（越大越强）
#:
#: 只有四档，**没有「同花顺」**——那是斗牛的规则。三公五比的是
#: 「豹子 > 顺子 > 对子 > 散牌」，同型再比点数。早先这里多写了一档
#: 同花顺，但发牌时只保留点数、丢弃花色，那一档永远判不出来，
#: 属于写了却永远走不到的死代码，已删。
HAND_TRIPLE = 4           # 豹子（3 张同点）
HAND_STRAIGHT = 3         # 顺子（3 张点数连续）
HAND_PAIR = 2             # 对子
HAND_HIGH = 1             # 散牌（比最大那张）

_HAND_NAMES = {
    HAND_TRIPLE: "豹子",
    HAND_STRAIGHT: "顺子",
    HAND_PAIR: "对子",
    HAND_HIGH: "散牌",
}

#: 一桌最多坐几个闲家（机器人是庄，不算在内）
MAX_SEATS = 3
#: 牌局最长存活时间（秒）。超时按当前状态强制结算。
TABLE_TTL = 600.0


def rank_value(rank: str) -> int:
    """``'A'`` -> 14，``'2'`` -> 2。"""
    return _RANKS.index(rank) + 2


def eval_hand(cards: list[str]) -> tuple:
    """评估三张牌。返回**可直接比较**的元组，越大越强。

    形如 ``(牌型等级, 主点数, 次点数, 末点数)``——同型比主点数，
    主点数相同再依次比后面几项，所以直接用 ``>`` 就能比大小。

    ``cards`` 是 3 个字符的字符串，如 ``["A", "K", "9"]``（只含点数，
    花色在发牌时就丢掉了——三公五不看花色）。
    """
    vals = sorted((rank_value(c) for c in cards), reverse=True)
    # 顺子：三张点数连续（QKA 算顺子，A234 这类环绕不算）
    straight = (vals[0] - vals[1] == 1 and vals[1] - vals[2] == 1)

    if vals[0] == vals[1] == vals[2]:
        return (HAND_TRIPLE, vals[0], 0, 0)
    if straight:
        return (HAND_STRAIGHT, vals[0], vals[1], vals[2])
    if vals[0] == vals[1] or vals[1] == vals[2]:
        pair = vals[0] if vals[0] == vals[1] else vals[1]
        kicker = vals[2] if vals[0] == vals[1] else vals[0]
        return (HAND_PAIR, pair, kicker, 0)
    return (HAND_HIGH, vals[0], vals[1], vals[2])


def hand_label(cards: list[str]) -> str:
    """把三张牌渲染成「豹子 K」这样的中文描述。"""
    return f"{_HAND_NAMES[eval_hand(cards)[0]]}"


def format_cards(cards: list[str]) -> str:
    """``["A", "K", "9"]`` -> ``A K 9``。"""
    return " ".join(cards)


def new_deck() -> list[str]:
    """一副 52 张牌，只保留点数（每点 4 张）。

    三公五不看花色，所以花色在生成时就丢掉——留着它只会让人误以为
    「同花」算一种牌型（实际不算）。
    """
    return [r for r in _RANKS for _ in range(4)]


def deal(deck: list[str], hands: int) -> tuple[list[list[str]], list[str]]:
    """从牌堆里给 ``hands`` 个人各发 3 张，返回 ``(手牌列表, 剩余牌堆)``。"""
    pool = list(deck)
    random.shuffle(pool)
    got: list[list[str]] = []
    for _ in range(hands):
        got.append(pool[:3])
        del pool[:3]
    return got, pool


@dataclass
class Seat:
    """一个闲家席位。"""

    user_id: str
    name: str
    bet: int = 0
    cards: list[str] = field(default_factory=list)
    folded: bool = False
    done: bool = False          # 已看牌并选择要留/不要


@dataclass
class Table:
    """一桌三公五。"""

    group_id: str
    dealer: Seat
    seats: list[Seat] = field(default_factory=list)
    deck: list[str] = field(default_factory=list)
    started: float = 0.0
    dealt: bool = False
    settled: bool = False
    pot: int = 0

    def alive(self, now: float) -> bool:
        return not self.settled and now - self.started < TABLE_TTL

    def occupied(self) -> int:
        return len(self.seats)

    def player(self, user_id: str) -> Seat | None:
        for s in self.seats:
            if s.user_id == str(user_id):
                return s
        return None


class SanguanwuHandle(BaseHandle):
    """三公五牌局。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        #: 进行中的牌桌：{group_id: Table}
        self._tables: dict[str, Table] = {}
        #: 每群开局时间（冷却）
        self._last_open: dict[str, float] = {}
        #: 当日净亏：{(group_id, uid): (date, n)}
        self._lost: dict[tuple[str, str], tuple[str, int]] = {}
        #: 本群最近一局的赢家（卖身契默认卖给ta）
        self._last_winner: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _int(self, value, default: int = 0) -> int:
        try:
            if isinstance(value, bool):
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    def _game_cfg(self, event) -> dict:
        return self.cfg_for(event).game or {}

    def _contract_cfg(self, event) -> dict:
        return self.cfg_for(event).contract or {}

    def _points_enabled(self, event) -> bool:
        try:
            from .shop_handle import ShopHandle

            return ShopHandle(self.cfg, self.db).points_enabled(event)
        except Exception:
            return True

    def _expire(self, gid: str) -> None:
        """清理超时或已结算的牌桌。"""
        t = self._tables.get(gid)
        if t is not None and not t.alive(time.time()):
            self._tables.pop(gid, None)

    def last_winner(self, group_id: str) -> str:
        """本群最近一局的赢家（供卖身契找主人）。没有就返回空串。"""
        return self._last_winner.get(str(group_id), "")

    # ------------------------------------------------------------------ #
    #  开桌 / 入座
    # ------------------------------------------------------------------ #
    async def open_table(self, event, cfg: dict) -> str:
        """机器人开一桌（它当庄）。"""
        gid = str(self.group_id(event))
        if not bool(cfg.get("sanguanwu_enable", False)):
            return ("🃏 三公五没开启。管理员可在「小游戏 → 启用三公五」里打开。")

        if not self._points_enabled(event):
            return "💎 本群积分系统已关闭，三公五不可用。"

        now = time.time()
        self._expire(gid)
        cur = self._tables.get(gid)
        if cur is not None:
            if cur.occupied() > 0 and not cur.settled:
                return ("🃏 本群已经有一桌在进行，还有 "
                        f"{cur.occupied()} 位闲家。要结束这桌请用「/三公五 散桌」。")
        # 冷却：避免连发「三公五」把桌子刷满
        last = self._last_open.get(gid, 0.0)
        if now - last < 10:
            return "🃏 刚开过桌了，稍等一下。"

        entry = max(1, self._int(cfg.get("sanguanwu_entry", 10), 10))
        max_seats = min(MAX_SEATS, max(1, self._int(cfg.get("sanguanwu_seats", 2), 2)))
        bot_name = "我（庄）"
        dealer = Seat(user_id="__bot__", name=bot_name)
        self._tables[gid] = Table(group_id=gid, dealer=dealer, started=now)
        self._last_open[gid] = now
        return (f"🃏 我坐庄开了一桌（底注 {entry} 分）。\n"
                f"发「/三公五 入座 <金额>」抢座位，"
                f"坐满 {max_seats} 人自动发牌（也可 1 人单挑我）。\n"
                f"发牌后看自己的牌用「/三公五 看牌」。")

    async def sit(self, event, amount: int, cfg: dict) -> str:
        """群友入座。"""
        gid = str(self.group_id(event))
        self._expire(gid)
        t = self._tables.get(gid)
        if t is None:
            return "🃏 现在没有开着的桌。发「/三公五」我开一桌。"

        uid = str(self.sender_id(event))
        name = await get_nickname(event, uid)
        max_seats = min(MAX_SEATS, max(1, self._int(cfg.get("sanguanwu_seats", 2), 2)))

        if t.player(uid) is not None:
            return f"🃏 {name} 你已经在桌上了。"

        entry = max(1, self._int(cfg.get("sanguanwu_entry", 10), 10))
        max_bet = max(entry, self._int(cfg.get("sanguanwu_max_bet", 200), 200))
        bet = max(entry, min(int(amount or entry), max_bet))

        points = self.db.get_points(gid, uid)
        if points < bet:
            return (f"💎 {name} 积分不够：这一局最少要 {bet}，你只有 {points}。\n"
                    f"（积分见底也可以在群里玩「/卖身契」——纯娱乐）")

        # 每日净亏上限
        cap = self._int(cfg.get("sanguanwu_daily_loss", 300), 300)
        day, lost = self._lost.get((gid, uid), ("", 0))
        if day != self._today():
            lost = 0
        if cap > 0 and lost + bet > cap:
            return (f"💎 {name} 今天已经输到上限（{cap} 分）了，明天再来。")

        if t.occupied() >= max_seats:
            return f"🃏 这一桌已经坐满 {max_seats} 人了，等下一桌。"

        seat = Seat(user_id=uid, name=name, bet=bet)
        t.seats.append(seat)
        t.pot += bet
        # 真的把注扣掉。**必须扣**——只记 pot 不扣等于凭空造分，
        # 赢家通吃时会净增积分，几天就能把全群积分刷爆。
        self.db.add_points(gid, uid, -bet)
        msg = (f"🃏 {name} 入座，下注 {bet} 分（底注 {entry}，单局封顶 {max_bet}）。\n"
               f"当前 {t.occupied()}/{max_seats} 人。")
        if t.occupied() >= max_seats:
            msg += "\n" + await self._deal(event, t, cfg)
        return msg

    async def _deal(self, event, t: Table, cfg: dict) -> str:
        """发牌（机器人私聊给每人看牌，群里只播报座位）。"""
        hands, rest = deal(new_deck(), t.occupied())
        t.deck = rest
        t.dealt = True
        for seat, cards in zip(t.seats, hands):
            seat.cards = cards
        gid = t.group_id
        # 私聊告知各自手牌，避免群里直接摊牌
        for seat in t.seats:
            try:
                await self.call_api(
                    event, "send_private_msg", user_id=_int_or_text(seat.user_id),
                    message=f"🃏 三公五：你的牌是 {format_cards(seat.cards)}"
                            f"（{hand_label(seat.cards)}）\n"
                            f"在群里发「/三公五 看牌」选择要留还是不要。")
            except Exception as e:
                logger.info(f"[磐石] 私聊发牌失败（改为群里看牌）: {e}")
        names = "、".join(s.name for s in t.seats)
        return f"🂡 已发牌给 {names}。各自看牌后用「/三公五 看牌」决定，最后摊牌比大小。"

    # ------------------------------------------------------------------ #
    #  看牌 / 弃牌
    # ------------------------------------------------------------------ #
    async def look(self, event, cfg: dict) -> str:
        gid = str(self.group_id(event))
        self._expire(gid)
        t = self._tables.get(gid)
        if t is None or not t.dealt:
            return "🃏 现在没有进行中的一局牌。"

        uid = str(self.sender_id(event))
        seat = t.player(uid)
        if seat is None:
            return "🃏 你不在这一桌。先发「/三公五 入座」。"
        if seat.done:
            return f"🃏 {seat.name} 你已经看过牌了，等其他人。"

        name = await get_nickname(event, uid)
        # 看牌 = 确认要留（弃牌请用 /三公五 不要）
        seat.done = True
        ready = sum(1 for s in t.seats if s.done)
        tail = ""
        if ready == t.occupied():
            tail = "\n所有人都看牌了，摊牌！" + await self._settle(event, t)
        return (f"🃏 {name} 看牌：{format_cards(seat.cards)}（{hand_label(seat.cards)}），"
                f"要保留。（{ready}/{t.occupied()} 已看牌）{tail}")

    async def fold(self, event, cfg: dict) -> str:
        """弃牌：放弃这一局，已下的注不退。"""
        gid = str(self.group_id(event))
        self._expire(gid)
        t = self._tables.get(gid)
        if t is None or not t.dealt:
            return "🃏 现在没有进行中的一局牌。"
        uid = str(self.sender_id(event))
        seat = t.player(uid)
        if seat is None:
            return "🃏 你不在这一桌。"
        if seat.folded:
            return f"🃏 {seat.name} 你已经弃牌了。"
        seat.folded = True
        seat.done = True
        name = await get_nickname(event, uid)
        ready = sum(1 for s in t.seats if s.done)
        tail = ""
        if ready == t.occupied():
            tail = "\n" + await self._settle(event, t)
        return f"🃏 {name} 弃牌（已下的 {seat.bet} 分不退）。{tail}"

    # ------------------------------------------------------------------ #
    #  结算
    # ------------------------------------------------------------------ #
    async def _settle(self, event, t: Table) -> str:
        """摊牌比大小，赢家通吃。"""
        if t.settled:
            return ""
        t.settled = True
        self._tables.pop(t.group_id, None)

        active = [s for s in t.seats if not s.folded]
        lines = ["🃏 摊牌比大小："]
        for s in t.seats:
            if s.folded:
                lines.append(f"· {s.name}：弃牌（-{s.bet}）")
            else:
                lines.append(f"· {s.name}：{format_cards(s.cards)}"
                             f"（{hand_label(s.cards)}）")

        if not active:
            lines.append("所有人都弃牌，庄家收下底池。")
            return "\n".join(lines)

        best = max(active, key=lambda s: eval_hand(s.cards))
        bestv = eval_hand(best.cards)
        tied = [s for s in active if eval_hand(s.cards) == bestv]

        if len(tied) > 1:
            # 平局：底池在赢家之间平分。
            # 各人分到 pot//n（**不减自己那份**——入座时扣的本金就在 pot 里，
            # 分到就等于退回来了）。自测里守恒断言专门盯这一条。
            share = t.pot // len(tied)
            text = "\n".join(lines) + f"\n🎴 多人同点，最大牌平局，底池平分。"
            for s in tied:
                got = self.db.add_points(t.group_id, s.user_id, share)
                text += f"\n· {s.name} 平分 +{share}（当前 {got}）"
            return text

        # 赢家拿走**整个底池**（含自己入座时扣的那份，等于退回本金 + 赢走别人的）。
        # 净增 = pot。因为本金已在入座时扣过，这里再减一次反而会吞掉赢家自己那份。
        # 守恒验证：输家净减自己注，赢家净增 pot，所有人的注在 pot 里循环，总量不变。
        win_amount = t.pot
        won = self.db.add_points(t.group_id, best.user_id, win_amount)
        text = "\n".join(lines) + f"\n🎉 {best.name} 通吃底池 {t.pot} 分！"
        # 记负输（赢家之外的活跃闲家算净亏）
        day = self._today()
        for s in active:
            if s.user_id == best.user_id:
                continue
            k = (t.group_id, s.user_id)
            d, lost = self._lost.get(k, ("", 0))
            if d != day:
                lost = 0
            self._lost[k] = (day, lost + s.bet)
        # 记赢家，卖身契默认会卖给他
        self._last_winner[t.group_id] = best.user_id
        text += f"\n💎 {best.name} +{win_amount}（当前 {won}）"
        return text

    async def close_table(self, event) -> str:
        """散桌：强制结算当前牌局（管理员或坐庄的人用）。"""
        gid = str(self.group_id(event))
        self._expire(gid)
        t = self._tables.get(gid)
        if t is None:
            return "🃏 现在没有开着的桌。"
        if not t.dealt:
            self._tables.pop(gid, None)
            return "🃏 已把没发牌的那桌散了。"
        return "🃏 强制收桌：" + await self._settle(event, t)

    # ------------------------------------------------------------------ #
    #  状态查询
    # ------------------------------------------------------------------ #
    async def status(self, event) -> str:
        gid = str(self.group_id(event))
        self._expire(gid)
        t = self._tables.get(gid)
        if t is None:
            return "🃏 现在没有开着的桌。发「/三公五」我开一桌。"
        names = "、".join(f"{s.name}({s.bet})" for s in t.seats) or "暂无"
        if not t.dealt:
            return f"🃏 桌上有：{names}\n等发牌（坐满自动发）。"
        return f"🃏 进行中，闲家 {names}，底池 {t.pot} 分。发「/三公五 看牌」看牌。"

    async def help(self) -> str:
        return ("🃏 三公五玩法（我坐庄，2~3 人玩）\n"
                "/三公五            — 我开一桌\n"
                "/三公五 入座 50    — 坐下并下注（机器人只会播报别人的牌）\n"
                "/三公五 看牌       — 私聊看自己的牌并确认保留\n"
                "/三公五 不要       — 弃牌（已下注不退）\n"
                "/三公五 状态       — 看当前桌况\n"
                "/三公五 散桌       — 强制收桌结算\n"
                "牌型：豹子 > 顺子 > 对子 > 散牌；同型比点数（QKA 算顺子）。\n"
                "（纯积分娱乐，机器人不抽水；输光了可以「/卖身契」——开玩笑的）")


def _int_or_text(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return value
