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
        cfg = self.cfg.welcome

        # 黑名单直接拒绝
        if self.db.is_blacklisted(group_id, user_id):
            await self._handle(event, flag, False, "黑名单用户")
            return None

        if not cfg.get("join_review_enable", False):
            return None

        comment = (comment or "").strip()
        accept_words = [str(w) for w in (cfg.get("join_accept_words", []) or [])]
        reject_words = [str(w) for w in (cfg.get("join_reject_words", []) or [])]

        # 黑词优先
        if any(w and w in comment for w in reject_words):
            await self._handle(event, flag, False, "命中拒绝关键词")
            return f"🚫 已自动拒绝 {user_id} 的加群申请（拒绝关键词）"

        # 白词批准
        if any(w and w in comment for w in accept_words):
            await self._handle(event, flag, True, "")
            return f"✅ 已自动批准 {user_id} 的加群申请"

        # 未命中策略
        if cfg.get("join_no_match_reject", False):
            await self._handle(event, flag, False, "未命中准入关键词")
            return f"🚫 已自动拒绝 {user_id} 的加群申请（未命中关键词）"

        return None

    async def _handle(self, event, flag: str, approve: bool, reason: str) -> bool:
        action = "set_group_add_request"
        ok, _ = await self.call_api(
            event,
            action,
            flag=flag,
            sub_type="add",
            approve=approve,
            reason=reason,
        )
        return ok
