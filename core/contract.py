"""卖身契：三公五输光后，把自己「卖」给赢家。

**这是纯娱乐功能，不涉及任何现实约束。** 契约只是群内的一个身份标记 +
积分分成规则，随时可解除，不构成任何现实义务。设计时特意把边界划死：

- 只在**游戏内的积分系统**里生效，绝不触碰真实的人身关系；
- 有效期强制有上限（``contract_hours``，最多 7 天），到期自动解除；
- 主人可以随时「赎回」，奴隶不能被要求做任何现实层面的事；
- 每天次数有限（``contract_daily_limit``），避免反复签契约刷存在感。

它存在的意义是给「输光了」一个体面的出口：不是直接归零退出，
而是换来一笔启动积分（``contract_bribe``）继续玩——这既是玩笑，
也给三公五设了一个软性的"破产保护"，不至于让人彻底玩不下去。
"""

from __future__ import annotations

import time

from ..utils import get_nickname
from .base_handle import BaseHandle

#: 契约状态
CONTRACT_ACTIVE = "active"
CONTRACT_REDEEMED = "redeemed"
CONTRACT_EXPIRED = "expired"


class ContractHandle(BaseHandle):
    """卖身契。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        #: 进行中的契约：{(group_id, user_id): {master_id, master_name, until, ...}}
        self._contracts: dict[tuple[str, str], dict] = {}
        #: 每日签约次数：{(group_id, uid): (date, n)}
        self._signed: dict[tuple[str, str], tuple[str, int]] = {}

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

    def _cfg(self, event) -> dict:
        return self.cfg_for(event).contract or {}

    def _expire(self, group_id: str, user_id: str) -> dict | None:
        """取契约；已过期就清掉并返回 None。"""
        key = (str(group_id), str(user_id))
        c = self._contracts.get(key)
        if not c:
            return None
        if c["until"] <= time.time():
            c["status"] = CONTRACT_EXPIRED
            self._contracts.pop(key, None)
            return None
        return c

    def active_contract(self, group_id, user_id) -> dict | None:
        """给外部（如发言得积分、签到）查询用：这条契约是否还有效。"""
        return self._expire(group_id, user_id)

    def tribute_ratio(self, group_id) -> float:
        """本群契约分成比例（0~1）。"""
        try:
            raw = self.cfg.for_group(str(group_id)).contract
        except Exception:
            return 0.0
        ratio = self._int((raw or {}).get("contract_tribute_ratio", 100), 100)
        return max(0.0, min(1000, ratio)) / 1000.0

    def tribute(self, group_id: str, user_id: str, earned: int) -> int:
        """积分分成钩子：``user_id`` 这次 earned 分，若有契约就抽成给主人。

        由 :meth:`Storage.add_points` 在**每次加分后**调用（见那里的注释）。
        直接改 ``_user`` 写主家的分，而不是递归调 ``add_points``——
        否则主人自己若也有契约会无限递归。

        Returns:
            actually 分给主人的积分（0 表示没有分成）。
        """
        c = self._expire(group_id, user_id)
        if not c or earned <= 0:
            return 0
        ratio = self.tribute_ratio(group_id)
        if ratio <= 0:
            return 0
        cut = int(earned * ratio)
        if cut <= 0:
            return 0
        master = str(c["master_id"])
        if master == str(user_id):
            return 0                    # 不给自己抽成
        with self.db._lock:
            mu = self.db._user(self.db._points_group(group_id), master)
            mu["points"] = int(mu.get("points", 0)) + cut
            c["given"] = int(c.get("given", 0)) + cut
            self.db.save()
        return cut

    # ------------------------------------------------------------------ #
    #  签契约
    # ------------------------------------------------------------------ #
    async def sign(self, event, master_id: str = "") -> str:
        """签卖身契。可指定主人；不指定就卖给三公五赢家 / 积分最高者。"""
        gid = str(self.group_id(event))
        cfg = self._cfg(event)
        if not bool(cfg.get("contract_enable", True)):
            return "📜 卖身契没开启。管理员可在「卖身契」里打开。"

        uid = str(self.sender_id(event))
        name = await get_nickname(event, uid)

        if self._expire(gid, uid):
            return (f"📜 {name} 你已经有一份在生效的卖身契了"
                    f"（主人：{self._expire(gid, uid)['master_name']}），"
                    f"到期或被赎回后才能再签。")

        # 每日次数
        limit = max(1, self._int(cfg.get("contract_daily_limit", 1), 1))
        day, n = self._signed.get((gid, uid), ("", 0))
        if day != self._today():
            n = 0
        if n >= limit:
            return f"📜 {name} 今天签卖身契的次数用完了（每天 {limit} 次）。"

        # 找主人
        if master_id:
            master_name = await get_nickname(event, master_id)
        else:
            found_id, _ = self._find_master(gid, uid)
            if not found_id:
                return ("📜 暂时没人可卖。指定一个主人："
                        "发「/卖身契 <主人QQ或@他>」。")
            master_id = found_id
            # _find_master 只认得 QQ 号，昵称要现查——漏这一步会出现
            # 契约里主人是空字符串，播报变成「主人：」很难看。
            master_name = await get_nickname(event, master_id)

        if str(master_id) == uid:
            return f"📜 {name} 不能把自己卖给自己。"

        bribe = max(0, self._int(cfg.get("contract_bribe", 50), 50))
        hours = min(168, max(1, self._int(cfg.get("contract_hours", 24), 24)))
        until = time.time() + hours * 3600
        label = str(cfg.get("contract_label", "契约奴") or "契约奴")

        self._contracts[(gid, uid)] = {
            "master_id": str(master_id),
            "master_name": master_name,
            "slave_name": name,
            "until": until,
            "bribe": bribe,
            "label": label,
            "status": CONTRACT_ACTIVE,
        }
        self._signed[(gid, uid)] = (self._today(), n + 1)

        # 卖身钱：给一笔启动积分，输了才签的，不至于彻底玩不动
        got = (self.db.add_points(gid, uid, bribe) if bribe > 0
               else self.db.get_points(gid, uid))
        return (f"📜 卖身契已生效！\n"
                f"· 主人：{master_name}\n"
                f"· 你的新身份：{master_name} 的{label}\n"
                f"· 有效期：{hours} 小时（到期自动解除，或由主人赎回）\n"
                f"· 卖身钱：+{bribe} 积分（当前 {got}）\n"
                f"· 契约期内你获得的积分，会按比例分给主人\n"
                f"💡 纯游戏内的玩笑，不涉及任何现实约束。发「/我的卖身契」随时查看。")

    def _find_master(self, gid: str, uid: str) -> tuple[str, str]:
        """找一个主人的 QQ 号：三公五最近赢家优先，否则本群积分最高者。

        只返回 QQ 号，**不返回昵称**——昵称要用 ``get_nickname`` 现查
        （它是 async 且需要 event）。第二个返回值保留为空串，仅为兼容签名。
        """
        # 1) 三公五最近赢家
        try:
            sg = getattr(self, "_sanguanwu", None)
            winner = sg.last_winner(gid) if sg else ""
            if winner and str(winner) != uid:
                return str(winner), ""
        except Exception:
            pass
        # 2) 本群积分排行第一名（跳过自己）
        try:
            for other, _pts in self.db.top_points(gid, limit=5):
                if str(other) != uid:
                    return str(other), ""
        except Exception:
            pass
        return "", ""

    # ------------------------------------------------------------------ #
    #  赎回 / 查询
    # ------------------------------------------------------------------ #
    async def redeem(self, event, target: str = "") -> str:
        """主人赎回（或超管强制解除）。"""
        gid = str(self.group_id(event))
        uid = str(self.sender_id(event))
        cfg = self._cfg(event)
        is_super = uid in (self.cfg.super_admins or [])

        key = (gid, str(target)) if target else (gid, uid)
        c = self._expire(gid, str(target)) if target else self._expire(gid, uid)
        if not c:
            who = target or "你"
            return f"📜 {who} 现在没有在生效的卖身契。"

        # 只有主人本人或超管能赎回
        if str(c["master_id"]) != uid and not is_super:
            return f"🚫 只有主人（{c['master_name']}）能赎回这份契约。"

        c["status"] = CONTRACT_REDEEMED
        self._contracts.pop(key, None)
        return f"✅ 已解除 {c['slave_name']} 的卖身契，身份恢复自由。"

    async def my_status(self, event, target: str = "") -> str:
        gid = str(self.group_id(event))
        who = str(target) if target else str(self.sender_id(event))
        c = self._expire(gid, who)
        if not c:
            return "📜 你现在没有卖身契，是自由的。"

        remain = int(max(0, c["until"] - time.time()))
        hours = remain // 3600
        mins = (remain % 3600) // 60
        return (f"📜 卖身契（{c['label']}）\n"
                f"· 主人：{c['master_name']}\n"
                f"· 剩余：{hours} 小时 {mins} 分（到期自动解除）\n"
                f"· 卖身钱：{c['bribe']} 积分\n"
                f"· 契约期内获得的积分会按比例分给主人\n"
                f"💡 纯游戏玩笑。主人发「/赎回」或超管可提前解除。")

    async def list_contracts(self, event) -> str:
        """本群当前生效的契约（管理员看的趣味榜单）。"""
        gid = str(self.group_id(event))
        rows = []
        for (g, uid), c in list(self._contracts.items()):
            if g != gid:
                continue
            if c["until"] <= time.time():
                self._contracts.pop((g, uid), None)
                continue
            rows.append(f"· {c['slave_name']} → {c['master_name']}")
        if not rows:
            return "📜 本群现在没有生效的卖身契。"
        return "📜 本群卖身契（纯玩笑）：\n" + "\n".join(rows)
