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

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("panshi")

from ..utils import get_nickname
from .base_handle import BaseHandle


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
        return f"✅ {name} 已投票给「{vote['options'][idx - 1]}」"

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
        return await self.render_chain(event)

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
            expire = int(self.cfg.warning.get("warn_expire_days", 30))
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
