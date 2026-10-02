"""进群申请审核：白词自动批准 / 黑词自动拒绝 / 未命中策略。"""

from __future__ import annotations

from .base_handle import BaseHandle


class JoinHandle(BaseHandle):
    """处理加群请求事件。"""

    async def on_request(self, event, flag: str, user_id: str, comment: str = "", sub_type: str = "add") -> str | None:
        """处理加群申请。

        Args:
            flag: 请求标识（用于同意/拒绝）
            user_id: 申请人 QQ
            comment: 申请理由
            sub_type: add / invite

        Returns:
            处理提示文本，None 表示交给人工。
        """
        group_id = str(event.get_group_id())
        # 按群视角取配置：面板「独立配置」里的入群审核项必须真的生效。
        cfg = self.cfg_for(event).welcome
        sub_type = str(sub_type or "add")

        # 黑名单直接拒绝
        if self.db.is_blacklisted(group_id, user_id):
            await self._handle(event, flag, False, "黑名单用户", sub_type)
            return None

        if not cfg.get("join_review_enable", False):
            return None

        comment = (comment or "").strip()
        accept_words = [str(w) for w in (cfg.get("join_accept_words", []) or [])]
        reject_words = [str(w) for w in (cfg.get("join_reject_words", []) or [])]

        # 黑词优先
        if any(w and w in comment for w in reject_words):
            ok = await self._handle(event, flag, False, "命中拒绝关键词", sub_type)
            if not ok:
                return f"⚠️ 自动拒绝 {user_id} 的加群申请失败（协议端拒绝或未连接）"
            return f"🚫 已自动拒绝 {user_id} 的加群申请（拒绝关键词）"

        # 白词批准
        if any(w and w in comment for w in accept_words):
            ok = await self._handle(event, flag, True, "", sub_type)
            if not ok:
                return f"⚠️ 自动批准 {user_id} 的加群申请失败（协议端拒绝或未连接）"
            return f"✅ 已自动批准 {user_id} 的加群申请"

        # 未命中策略
        if cfg.get("join_no_match_reject", False):
            ok = await self._handle(event, flag, False, "未命中准入关键词", sub_type)
            if not ok:
                return f"⚠️ 自动拒绝 {user_id} 的加群申请失败（协议端拒绝或未连接）"
            return f"🚫 已自动拒绝 {user_id} 的加群申请（未命中关键词）"

        return None

    async def _handle(
        self, event, flag: str, approve: bool, reason: str, sub_type: str = "add"
    ) -> bool:
        """下发加群申请处理。

        ``sub_type`` 必须是请求本身的类型（add / invite）：OneBot v11 对
        邀请类请求要求 ``invite``，此前硬编码 ``add`` 会让协议端拒绝，
        而调用方又把失败当成功回报给用户。
        """
        ok, _ = await self.call_api(
            event,
            "set_group_add_request",
            flag=flag,
            sub_type=sub_type or "add",
            approve=approve,
            reason=reason,
        )
        return ok
