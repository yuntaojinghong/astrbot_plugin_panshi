"""群内小游戏：猜数字 / 摇骰子 / 猜拳（可选押注）。

为什么单独成一个模块：这几个都需要**局状态**（进行中的那一局），
和投票/接龙同类，但状态更短命、触发词更"裸"，而且涉及发分，风险更高。

三条设计红线：

1. **限流是硬要求。** 没有冷却与每日发奖上限，群里连发「猜数字」就能把
   机器人刷爆，而且每局都要发分——等于开了台无限印钞机。
2. **只有"整条消息恰好是一个数字"才算猜**，且必须**本群正有一局在进行中**。
   否则群友随口说个「50」也会被当成猜数字，属于抢话。
   超出范围、不是纯数字的，一律**不响应**（静默放行），也不报错——
   报错同样是在抢话。
3. **押注默认关闭。** 凭空发奖是福利，玩家对赌是另一回事：必须显式开启，
   并且有单次上限与每日净输上限，免得有人一夜清零。

状态都在内存（局是短会话，重启即作废，不需要落盘）。
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass

from ..utils import get_nickname
from .base_handle import BaseHandle
from .interact import normalize_bare_word

#: 猜数字的范围
GUESS_MIN, GUESS_MAX = 1, 100

#: 一局猜数字最多存活多久（秒）。超时后静默作废，不再吃数字。
GUESS_TTL = 180.0

#: 猜拳：手势 -> 它能赢过的手势
_RPS_BEATS = {"石头": "剪刀", "剪刀": "布", "布": "石头"}
_ALL_MOVES = tuple(_RPS_BEATS)

#: 骰子点数的显示字符（下标 0 对应 1 点）
_DICE_FACES = "⚀⚁⚂⚃⚄⚅"


@dataclass
class GuessRound:
    """进行中的一局猜数字。"""

    answer: int
    started: float
    tries: int = 0

    def alive(self, now: float) -> bool:
        return now - self.started < GUESS_TTL


class GamesHandle(BaseHandle):
    """猜数字 / 摇骰子 / 猜拳 / 押注。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        #: 进行中的猜数字局：{group_id: GuessRound}
        self._round: dict[str, GuessRound] = {}
        #: 上次开局时间：{group_id: ts}（用于冷却）
        self._last_start: dict[str, float] = {}
        #: 当日已发奖局数：{group_id: (date, n)}
        self._awarded: dict[str, tuple[str, int]] = {}
        #: 当日净输：{(group_id, uid): (date, n)}（押注用）
        self._bet_lost: dict[tuple[str, str], tuple[str, int]] = {}

    # ------------------------------------------------------------------ #
    #  工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    @staticmethod
    def _int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _points_enabled(self, event) -> bool:
        """本群积分系统是否开着。发奖前必须问一句，否则会出现
        「积分系统已关闭、猜数字却还在加积分」的自相矛盾。"""
        try:
            from .shop_handle import ShopHandle

            return ShopHandle(self.cfg, self.db).points_enabled(event)
        except Exception:
            return True

    # ------------------------------------------------------------------ #
    #  猜数字
    # ------------------------------------------------------------------ #
    async def start_guess(self, event, cfg) -> str:
        gid = str(event.get_group_id())
        if not bool(cfg.get("guess_enable", True)):
            return "🔢 猜数字还没开启。管理员可在「小游戏」里打开。"

        now = time.time()
        cur = self._round.get(gid)
        if cur is not None and cur.alive(now):
            return "🔢 本群已经有一局在进行啦，直接发数字来猜。"

        cooldown = self._int(cfg.get("guess_cooldown", 60))
        last = self._last_start.get(gid, 0.0)
        if cooldown > 0 and now - last < cooldown:
            wait = int(cooldown - (now - last)) + 1
            return f"🔢 刚开过一局，{wait} 秒后再来。"

        self._round[gid] = GuessRound(
            answer=random.randint(GUESS_MIN, GUESS_MAX), started=now)
        self._last_start[gid] = now
        reward = self._int(cfg.get("guess_reward", 20))
        tail = f"，猜中者得 {reward} 积分" if reward > 0 else ""
        return (f"🔢 我想好了一个 {GUESS_MIN}~{GUESS_MAX} 之间的数字{tail}。\n"
                f"直接发数字来猜（3 分钟内有效）。")

    async def try_guess(self, event, word: str, cfg) -> str | None:
        """整条消息是一个数字时尝试猜。

        没在游戏中、数字越界、不是纯数字 —— 一律返回 ``None``（静默放行），
        绝不抢话，也不报错。
        """
        gid = str(event.get_group_id())
        cur = self._round.get(gid)
        if cur is None:
            return None
        now = time.time()
        if not cur.alive(now):
            self._round.pop(gid, None)
            return None
        if not word.isdigit():
            return None
        n = int(word)
        if not (GUESS_MIN <= n <= GUESS_MAX):
            return None

        cur.tries += 1
        uid = str(event.get_sender_id())
        name = await get_nickname(event, uid)

        if n != cur.answer:
            if n < cur.answer:
                return f"🔽 {name}：{n} 太小了，往上猜"
            return f"🔼 {name}：{n} 太大了，往下猜"

        # 猜中
        self._round.pop(gid, None)
        reward = self._int(cfg.get("guess_reward", 20))
        if reward <= 0:
            return f"🎉 {name} 猜中了！答案就是 {n}（共猜 {cur.tries} 次）"
        if not self._points_enabled(event):
            return (f"🎉 {name} 猜中了！答案就是 {n}\n"
                    f"（本群积分系统已关闭，这次不发积分）")

        # 每日发奖局数上限：到顶了就只祝贺，不再发分
        cap = self._int(cfg.get("guess_daily_games", 10))
        day, used = self._awarded.get(gid, ("", 0))
        if day != self._today():
            used = 0
        if cap > 0 and used >= cap:
            return (f"🎉 {name} 猜中了！答案就是 {n}\n"
                    f"（本群今天的小游戏积分已经发完 {cap} 局，这局只记名不发分）")

        total = self.db.add_points(gid, uid, reward)
        self._awarded[gid] = (self._today(), used + 1)
        return (f"🎉 {name} 猜中了！答案就是 {n}（共猜 {cur.tries} 次）\n"
                f"💎 +{reward} 积分，当前 {total}")

    # ------------------------------------------------------------------ #
    #  摇骰子
    # ------------------------------------------------------------------ #
    async def roll_dice(self, event, word: str, cfg) -> str | None:
        if not bool(cfg.get("dice_enable", True)):
            return None
        n = 1
        m = re.search(r"(\d{1,2})", word)
        if m:
            n = max(1, min(6, int(m.group(1))))
        vals = [random.randint(1, 6) for _ in range(n)]
        uid = str(event.get_sender_id())
        name = await get_nickname(event, uid)
        faces = " ".join(_DICE_FACES[v - 1] for v in vals)
        return (f"🎲 {name} 摇了 {n} 个骰子：{faces}\n"
                f"点数 {vals}，合计 {sum(vals)}")

    # ------------------------------------------------------------------ #
    #  猜拳
    # ------------------------------------------------------------------ #
    async def play_rps(self, event, move: str, cfg) -> str | None:
        if not bool(cfg.get("rps_enable", True)):
            return None
        move = str(move or "").strip()
        if move not in _RPS_BEATS:
            return f"✊✌️✋ 用法：猜拳 石头 / 猜拳 剪刀 / 猜拳 布"
        bot = random.choice(_ALL_MOVES)
        uid = str(event.get_sender_id())
        name = await get_nickname(event, uid)
        if bot == move:
            verdict = "平局，再来一次？"
        elif _RPS_BEATS[move] == bot:
            verdict = "你赢了 🎉"
        else:
            verdict = "我赢了 😎"
        return f"✊ {name}：{move}　我：{bot} → {verdict}"

    # ------------------------------------------------------------------ #
    #  押注（默认关闭）
    # ------------------------------------------------------------------ #
    async def bet(self, event, amount: int, cfg) -> str | None:
        """押注：你与机器人各摇一个骰子比大小。赢 +N、输 -N、平局退还。"""
        if not bool(cfg.get("bet_enable", False)):
            return "🎲 押注没开启。管理员可在「小游戏 → 启用押注」里打开。"

        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = await get_nickname(event, uid)

        max_bet = max(1, self._int(cfg.get("bet_max", 20), 20))
        amt = min(max(1, int(amount)), max_bet)

        if not self._points_enabled(event):
            return "🎲 本群积分系统已关闭，押注不可用。"

        points = self.db.get_points(gid, uid)
        if points < amt:
            return f"💎 {name} 积分不够：想押 {amt}，你只有 {points}。"

        # 每日净输上限：防止有人一直押到清零
        cap = self._int(cfg.get("bet_daily_loss", 100))
        day, lost = self._bet_lost.get((gid, uid), ("", 0))
        if day != self._today():
            lost = 0
        if cap > 0 and lost + amt > cap:
            return (f"💎 {name} 今天已经输到上限（{cap} 积分）了，明天再来。")

        mine, theirs = random.randint(1, 6), random.randint(1, 6)
        if mine == theirs:
            return (f"🎲 {name}：{mine}　我：{theirs} → 平局，积分退还。\n"
                    f"💎 当前 {points}")
        if mine > theirs:
            total = self.db.add_points(gid, uid, amt)
            return (f"🎲 {name}：{mine}　我：{theirs} → 你赢了！\n"
                    f"💎 +{amt} 积分，当前 {total}")
        # 输：扣分（不穿负；实际扣了多少以返回值为准）
        new_points = self.db.add_points(gid, uid, -amt)
        actual = points - new_points
        self._bet_lost[(gid, uid)] = (self._today(), lost + actual)
        return (f"🎲 {name}：{mine}　我：{theirs} → 我赢了。\n"
                f"💎 -{actual} 积分，当前 {new_points}")

    # ------------------------------------------------------------------ #
    #  总入口
    # ------------------------------------------------------------------ #
    async def play(self, event, text: str, cfg) -> str | None:
        """小游戏总入口：命中就返回要发到群里的文本，没命中返回 ``None``。

        只认**明确的游戏词汇**，所以不会抢正常聊天。
        「纯数字」是唯一一个需要额外条件的分支——必须本群正好有一局
        猜数字在进行中，否则一律放行（见 :meth:`try_guess`）。
        """
        word = normalize_bare_word(text)
        if not word:
            return None

        # 1) 猜数字开局
        if word in ("猜数字", "猜数", "猜个数", "数字游戏"):
            return await self.start_guess(event, cfg)

        # 2) 摇骰子（可带个数）
        if re.match(r"^[摇掷]骰子\d{0,2}$", word):
            return await self.roll_dice(event, word, cfg)

        # 3) 猜拳
        m = re.match(r"^猜拳(石头|剪刀|布|锤子)?$", word)
        if m:
            move = m.group(1) or ""
            if move == "锤子":
                move = "石头"
            if not move:
                return "✊✌️✋ 想猜拳就这样发：猜拳石头 / 猜拳剪刀 / 猜拳布"
            return await self.play_rps(event, move, cfg)

        # 4) 押注
        m = re.match(r"^押注(\d{0,7})$", word)
        if m:
            if not m.group(1):
                cap = max(1, self._int(cfg.get("bet_max", 20), 20))
                return f"🎲 押注用法：押注 10（单次上限 {cap} 分，比骰子点数大小）"
            return await self.bet(event, int(m.group(1)), cfg)

        # 5) 猜数字报数（只在有局时才会真的处理）
        if word.isdigit():
            return await self.try_guess(event, word, cfg)

        return None
