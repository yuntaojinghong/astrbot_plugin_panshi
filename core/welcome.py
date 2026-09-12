"""入群欢迎（增强版）+ 算术验证 + 退群处理。

欢迎语支持变量占位符：
    {at}     @新人
    {昵称}   新人昵称
    {群名}   群名称
    {人数}   当前群人数
    {时间}   入群时间
"""

from __future__ import annotations

import asyncio
import random
import time

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

from .base_handle import BaseHandle


class WelcomeHandle(BaseHandle):
    """处理入群 / 退群事件。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        # 待验证用户: {(gid, uid): {"answer": int, "expire": ts, "task": Task}}
        self._pending: dict[tuple, dict] = {}

    # ========== 入群 ==========
    async def on_member_increase(self, event, user_id: str, sub_type: str = "approve") -> str | None:
        """成员入群事件。"""
        group_id = str(event.get_group_id())
        cfg = self.cfg.welcome

        # 黑名单：直接踢出
        if self.db.is_blacklisted(group_id, user_id):
            await self.call_api(
                event,
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=True,
            )
            return None

        # 入群自动禁言
        join_ban = int(cfg.get("join_ban_time", 0) or 0)
        if join_ban > 0:
            await self.call_api(
                event,
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=self.cfg.clamp_ban_time(join_ban),
            )

        # 需要验证时，先发验证码，不欢迎
        if cfg.get("verify_enable", False):
            return await self._start_verify(event, group_id, user_id)

        # 普通欢迎
        if cfg.get("welcome_enable", True):
            return await self._build_welcome(event, group_id, user_id)
        return None

    async def _build_welcome(self, event, group_id: str, user_id: str) -> str:
        """构造欢迎语（支持变量、随机模板、图片）。"""
        cfg = self.cfg.welcome

        # 群名与人数
        group_name = await self._group_name(event)
        member_count = await self._member_count(event)

        templates = cfg.get("welcome_templates", []) or ["欢迎 {at} 加入本群！"]
        template = random.choice(templates)

        text = (
            str(template)
            .replace("{at}", f"[CQ:at,qq={user_id}]")
            .replace("{昵称}", await self._nickname(event, user_id))
            .replace("{群名}", group_name)
            .replace("{人数}", str(member_count))
            .replace("{时间}", time.strftime("%H:%M:%S"))
        )

        image = cfg.get("welcome_image", "") or ""
        delay = int(cfg.get("welcome_delay", 1) or 0)

        if delay > 0:
            await asyncio.sleep(delay)

        if image:
            return f"{text}\n[CQ:image,file={image}]"
        return text

    # ========== 算术验证 ==========
    async def _start_verify(self, event, group_id: str, user_id: str) -> str:
        a = random.randint(1, 20)
        b = random.randint(1, 20)
        op = random.choice(["+", "-", "×"])
        if op == "+":
            answer = a + b
        elif op == "-":
            a, b = max(a, b), min(a, b)
            answer = a - b
        else:
            a, b = random.randint(1, 9), random.randint(1, 9)
            answer = a * b

        timeout = int(self.cfg.welcome.get("verify_timeout", 120))
        key = (group_id, user_id)
        self._pending[key] = {"answer": answer, "expire": time.time() + timeout}

        # 超时任务：未答对则拒绝
        asyncio.create_task(self._verify_timeout(event, group_id, user_id, timeout))

        return (
            f"👋 欢迎新成员！请回答下面的问题以完成验证（{timeout} 秒内）：\n"
            f"**{a} {op} {b} = ?**\n"
            f"直接在群里回复答案即可。"
        )

    async def check_verify_reply(self, event) -> str | None:
        """检查用户回复是否为验证答案。返回提示文本或 None。"""
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        key = (group_id, user_id)
        pending = self._pending.get(key)
        if not pending:
            return None

        text = (event.message_str or "").strip()
        try:
            num = int("".join(ch for ch in text if ch.isdigit()))
        except ValueError:
            return None

        if num == pending["answer"]:
            self._pending.pop(key, None)
            # 验证通过：欢迎 + 解除入群禁言
            await self.call_api(
                event,
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=0,
            )
            if self.cfg.welcome.get("welcome_enable", True):
                return await self._build_welcome(event, group_id, user_id)
            return "✅ 验证通过，欢迎加入本群！"
        else:
            return "❌ 答案不对，请再试一次～"

    async def _verify_timeout(self, event, group_id: str, user_id: str, timeout: int) -> None:
        await asyncio.sleep(timeout)
        key = (group_id, user_id)
        if key in self._pending:
            self._pending.pop(key, None)
            await self.call_api(
                event,
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=False,
            )

    # ========== 退群 ==========
    async def on_member_decrease(self, event, user_id: str) -> str | None:
        group_id = str(event.get_group_id())
        cfg = self.cfg.welcome

        if cfg.get("leave_block", False):
            self.db.add_blacklist(group_id, user_id)

        if cfg.get("leave_notify", True):
            name = await self._nickname(event, user_id)
            return f"👋 成员 {name} 已离开本群。"
        return None

    # ========== 辅助 ==========
    async def _nickname(self, event, user_id) -> str:
        try:
            ok, info = await self.call_api(
                event, "get_group_member_info", group_id=int(event.get_group_id()),
                user_id=int(user_id), no_cache=True,
            )
            if ok and isinstance(info, dict):
                return str(info.get("card") or info.get("nickname") or user_id)
        except Exception:
            pass
        return str(user_id)

    async def _group_name(self, event) -> str:
        try:
            ok, info = await self.call_api(event, "get_group_info", group_id=int(event.get_group_id()))
            if ok and isinstance(info, dict):
                return str(info.get("group_name") or "")
        except Exception:
            pass
        return ""

    async def _member_count(self, event) -> int:
        try:
            ok, info = await self.call_api(event, "get_group_info", group_id=int(event.get_group_id()))
            if ok and isinstance(info, dict) and info.get("member_count"):
                return int(info["member_count"])
            ok, members = await self.call_api(
                event, "get_group_member_list", group_id=int(event.get_group_id())
            )
            if ok and isinstance(members, list):
                return len(members)
        except Exception:
            pass
        return 0
