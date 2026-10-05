"""互动工具：群投票 / 接龙 / 自助查询 / 关键词自动回复。

设计目标（参考 QQ 群管的「群应用」玩法）：
- **投票**：发起一个投票，群友回复编号投票，实时统计。
- **接龙**：发起接龙，群友按序号接龙，自动整理成列表。
- **自助查询**：普通成员也能查自己的积分 / 违规记录 / 禁言状态。
- **关键词自动回复**：管理员可配置「关键词 -> 回复」，命中即自动回复。

状态保存在内存中（投票/接龙是短期会话），需要跨重启的配置走 storage。
"""

from __future__ import annotations

import re
import time

from astrbot.api import logger

from ..utils import get_nickname
from .base_handle import BaseHandle

# ======================================================================
#  裸词快捷通道
# ======================================================================
#
# 群友不会为了查个积分去记「/积分」还要带斜杠。像「积分」「签到」
# 「积分排行」「积分商城」这种两三个字的词，直接打出来就应该能用。
#
# 匹配规则刻意做得很**严**：把消息去掉 @提及、空白、标点和句末语气词之后，
# **必须整句恰好等于某个词**才算命中。句中夹着这些词一律不触发
# （「我积分怎么还没到」不会命中）。这是这个功能最容易出事的地方——
# 一旦放宽成"包含即触发"，群里正常聊天就会被抢答。
_BARE_ACTIONS: dict[str, str] = {
    "积分": "points", "我的积分": "points", "查积分": "points",
    "我有多少积分": "points", "积分查询": "points", "查分": "points",
    "签到": "checkin", "我要签到": "checkin", "打卡": "checkin",
    "积分排行": "rank", "排行榜": "rank", "积分榜": "rank", "排行": "rank",
    "发言排行": "rank_msg", "发言榜": "rank_msg",
    "积分商城": "shop", "商城": "shop", "商店": "shop",
    "抽奖": "lottery", "抽一次": "lottery",
    "我的记录": "records", "消费记录": "records", "积分记录": "records",
    "我的": "self", "我的信息": "self", "我的档案": "self", "我的群档案": "self",
    "群管帮助": "help", "帮助": "help", "菜单": "help",
}

#: 自定义裸词时允许填的动作名 -> 说明（面板上照这个填）
BARE_ACTIONS: dict[str, str] = {
    "points": "查积分",
    "checkin": "签到",
    "rank": "积分排行",
    "rank_msg": "发言排行",
    "shop": "积分商城",
    "lottery": "抽奖",
    "records": "消费记录",
    "self": "我的群档案",
    "help": "帮助",
}

#: 归一化：开头的 @某人、空白、中英文标点
_BARE_AT_RE = re.compile(r"^@\S+\s*")
_BARE_STRIP_RE = re.compile(
    r"[\s,，。.!！?？~～、:：;；\"'“”‘’()（）\[\]【】<>《》\-—_·]+")
#: 句末语气词（「积分呢」「签到吧」也应当命中）
_BARE_TAIL_RE = re.compile(r"[呢啊吧呀哦噢啦嘛嘞哈]+$")


def normalize_bare_word(text: str) -> str:
    """把一条消息归一化成「裸词」形态，便于整句精确比较。

    依次去掉：开头的 ``@某人``、空白与常见中英文标点、句末语气词。
    """
    t = str(text or "").strip()
    t = _BARE_AT_RE.sub("", t)
    t = _BARE_STRIP_RE.sub("", t)
    t = _BARE_TAIL_RE.sub("", t)
    return t


class InteractHandle(BaseHandle):
    """投票 / 接龙 / 自助查询 / 关键词回复。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        # 进行中的投票: {group_id: {"title","options":[...],"votes":{uid:idx},"start":ts,"closed":bool}}
        self._votes: dict[str, dict] = {}
        # 进行中的接龙: {group_id: {"title","items":[{"uid","text"}],"start":ts}}
        self._chains: dict[str, dict] = {}

    # ========== 投票 ==========
    async def start_vote(self, event, raw_args: str) -> str:
        """发起投票：/投票 标题 | 选项1 | 选项2 ..."""
        group_id = str(event.get_group_id())
        parts = [p.strip() for p in re.split(r"[|｜]", raw_args or "") if p.strip()]
        if len(parts) < 3:
            return (
                "🗳️ 用法：/投票 标题 | 选项1 | 选项2 | 选项3\n"
                "例如：/投票 周末去哪 | 爬山 | 桌游 | 宅家"
            )
        title, options = parts[0], parts[1:]
        if len(options) > 10:
            return "🗳️ 选项最多 10 个，请精简一下。"
        self._votes[group_id] = {
            "title": title,
            "options": options,
            "votes": {},
            "start": time.time(),
            "closed": False,
        }
        lines = [f"🗳️ 投票：{title}", "────────────"]
        for i, opt in enumerate(options, 1):
            lines.append(f"{i}. {opt}")
        lines.append("────────────")
        lines.append("回复 /投票 编号 即可投票（如 /投票 2）")
        return "\n".join(lines)

    async def cast_vote(self, event, arg: str) -> str:
        """投票：/投票 编号"""
        group_id = str(event.get_group_id())
        vote = self._votes.get(group_id)
        if not vote or vote.get("closed"):
            return "🗳️ 当前没有进行中的投票。用 /投票 标题 | 选项1 | 选项2 发起一个。"
        m = re.search(r"\d+", arg or "")
        if not m:
            return "🗳️ 请回复要投的编号，例如 /投票 2"
        idx = int(m.group(0))
        if idx < 1 or idx > len(vote["options"]):
            return f"🗳️ 编号要在 1~{len(vote['options'])} 之间。"
        uid = str(event.get_sender_id())
        vote["votes"][uid] = idx - 1
        name = await get_nickname(event, uid)
        msg = f"✅ {name} 已投票给「{vote['options'][idx - 1]}」"
        got = self._award_interact(event, "vote")
        if got:
            msg += f"\n💎 参与投票 +{got} 积分"
        return msg

    async def vote_result(self, event) -> str:
        """查看/结束投票：/投票结果"""
        group_id = str(event.get_group_id())
        vote = self._votes.get(group_id)
        if not vote:
            return "🗳️ 当前没有进行中的投票。"
        vote["closed"] = True
        options = vote["options"]
        tally = [0] * len(options)
        for idx in vote["votes"].values():
            if 0 <= idx < len(tally):
                tally[idx] += 1
        total = sum(tally)
        lines = [f"🗳️ 投票结果：{vote['title']}（共 {total} 票）", "────────────"]
        order = sorted(range(len(options)), key=lambda i: tally[i], reverse=True)
        for i in order:
            bar = "█" * tally[i] + "░" * max(0, max(tally) - tally[i])
            pct = f"{tally[i] * 100 // total}%" if total else "0%"
            lines.append(f"{options[i]}：{tally[i]} 票（{pct}）{bar}")
        return "\n".join(lines)

    # ========== 接龙 ==========
    async def start_chain(self, event, raw_args: str) -> str:
        """发起接龙：/接龙 主题"""
        group_id = str(event.get_group_id())
        title = (raw_args or "").strip() or "接龙"
        self._chains[group_id] = {"title": title, "items": [], "start": time.time()}
        return (
            f"🔗 接龙：{title}\n"
            "────────────\n"
            "回复 /接龙 你的内容 即可参与"
        )

    async def add_chain(self, event, raw_args: str) -> str:
        """参与接龙：/接龙 内容"""
        group_id = str(event.get_group_id())
        chain = self._chains.get(group_id)
        content = (raw_args or "").strip()
        if not chain:
            return await self.start_chain(event, content)
        if not content:
            return self.render_chain(event)
        uid = str(event.get_sender_id())
        # 同一人只保留最新一条
        chain["items"] = [it for it in chain["items"] if it["uid"] != uid]
        chain["items"].append({"uid": uid, "text": content})
        rendered = await self.render_chain(event)
        got = self._award_interact(event, "chain")
        if got:
            rendered += f"\n💎 参与接龙 +{got} 积分"
        return rendered

    async def render_chain(self, event) -> str:
        group_id = str(event.get_group_id())
        chain = self._chains.get(group_id)
        if not chain:
            return "🔗 当前没有进行中的接龙。用 /接龙 主题 发起一个。"
        lines = [f"🔗 接龙：{chain['title']}", "────────────"]
        if not chain["items"]:
            lines.append("（还没有人参与）")
        for i, it in enumerate(chain["items"], 1):
            name = await get_nickname(event, it["uid"])
            lines.append(f"{i}. {name}：{it['text']}")
        lines.append("────────────")
        lines.append("回复 /接龙 你的内容 参与")
        return "\n".join(lines)

    # ========== 自助查询 ==========
    async def self_query(self, event, what: str) -> str:
        """普通成员自助查询：/我的 积分|警告|发言|全部"""
        group_id = str(event.get_group_id())
        uid = str(event.get_sender_id())
        name = await get_nickname(event, uid)
        what = (what or "").strip()

        def _points():
            return self.db.get_points(group_id, uid)

        def _msgs():
            return self.db.get_message_count(group_id, uid)

        def _warns():
            # 与 /警告 的处置口径保持一致：按群视角读保留天数
            expire = int(self.cfg_for(event).warning.get("warn_expire_days", 30))
            ws = self.db.get_warnings(group_id, uid, expire)
            if not ws:
                return "✅ 暂无违规记录"
            lines = [f"共 {len(ws)} 次："]
            for i, w in enumerate(ws[-5:], 1):
                lines.append(f"  {i}. [{w.get('time','')}] {w.get('reason','违规')}")
            return "\n".join(lines)

        if "积分" in what:
            return f"💎 {name} 的积分：{_points()}"
        if "警告" in what or "违规" in what:
            return f"📋 {name} 的违规记录：\n{_warns()}"
        if "发言" in what:
            return f"🗣️ {name} 本群累计发言 {_msgs()} 条"
        if "签到" in what:
            done = self.db.has_checked_in(group_id, uid)
            return f"📅 {name} 今天{'已' if done else '还未'}签到"
        # 全部
        return (
            f"📇 {name} 的群档案\n"
            f"────────────\n"
            f"💎 积分：{_points()}\n"
            f"🗣️ 发言：{_msgs()} 条\n"
            f"📅 今日签到：{'已签' if self.db.has_checked_in(group_id, uid) else '未签'}\n"
            f"📋 违规：\n{_warns()}"
        )

    # ========== 裸词快捷通道 ==========
    def match_bare_word(self, text: str, extra: str = "") -> str | None:
        """判断一条消息是不是「裸词快捷词」，是则返回对应动作名。

        **只认整句精确匹配**（归一化之后）。宁可漏，绝不抢正常聊天。

        Args:
            text: 消息原文。
            extra: 管理员自定义规则，每行 ``关键词 => 动作``。

        Returns:
            动作名（见 :data:`BARE_ACTIONS`）；不是裸词时返回 ``None``。
        """
        word = normalize_bare_word(text)
        if not word or len(word) > 12:
            return None

        table = _BARE_ACTIONS
        custom = str(extra or "").strip()
        if custom:
            table = dict(_BARE_ACTIONS)
            for line in custom.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=>" not in line:
                    continue
                kw, _, act = line.partition("=>")
                kw, act = normalize_bare_word(kw), act.strip().lower()
                if kw and act in BARE_ACTIONS:
                    table[kw] = act
                else:
                    logger.info(f"[磐石] 裸词自定义规则已忽略（动作不识别）: {line!r}")
        return table.get(word)

    # ========== 参与互动得积分 ==========
    def _award_interact(self, event, scope: str) -> int:
        """参与投票/接龙发积分，返回加分（0 = 没加）。

        带**每日上限**：互动是能重复参与的，不封顶就等于多了个刷分入口。
        积分系统关闭时不发（与商城/风控扣分的口径一致）。
        """
        try:
            from .shop_handle import ShopHandle

            if not ShopHandle(self.cfg, self.db).points_enabled(event):
                return 0
        except Exception:
            return 0

        cfg = self.cfg_for(event).interact
        value = int(cfg.get(f"{scope}_points", 0) or 0)
        if value <= 0:
            return 0
        gid = str(event.get_group_id())
        uid = str(event.get_sender_id())
        cap = int(cfg.get("interact_points_daily_cap", 30) or 0)
        if cap > 0:
            room = cap - self.db.daily_count(gid, uid, "interact")
            if room <= 0:
                return 0
            value = min(value, room)      # 按积分夹紧，避免溢出上限
        self.db.bump_daily_count(gid, uid, "interact", value)
        self.db.add_points(gid, uid, value)
        return value

    # ========== 关键词自动回复 ==========
    def match_auto_reply(self, text: str) -> str | None:
        """命中关键词则返回回复内容，否则 None。

        配置格式（面板「互动」分组）::

            auto_replies:
              - keywords: "群规,规矩"
                reply: "群规见置顶公告～"
        """
        if not text:
            return None
        rules = self.cfg.parsed_auto_replies()
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            kws = rule.get("keywords") or ""
            reply = rule.get("reply") or ""
            if not reply:
                continue
            for kw in re.split(r"[,，、;；\s]+", str(kws)):
                kw = kw.strip()
                if kw and kw in text:
                    return str(reply)
        return None

    # ========== 清理 ==========
    def cleanup_stale(self, max_age: float = 86400) -> None:
        """清理超过 max_age 的投票/接龙，避免长期占内存。"""
        now = time.time()
        for gid in list(self._votes.keys()):
            if now - self._votes[gid].get("start", now) > max_age:
                self._votes.pop(gid, None)
        for gid in list(self._chains.keys()):
            if now - self._chains[gid].get("start", now) > max_age:
                self._chains.pop(gid, None)
