"""智能上下文收集：为自然语言意图识别提供判断依据。

收集内容：
- 引用消息（被引用者 ID、内容）
- 最近若干条发言（谁说了什么，用于"刚才刷屏的""发广告的"）
- @ 对象
- 群成员列表（用于按名字模糊匹配"张三"）
"""

from __future__ import annotations

import time


class ContextCollector:
    """维护每群的近期消息上下文。"""

    def __init__(self, max_len: int = 50):
        self.max_len = max_len
        # {group_id: [{"user_id","name","text","message_id","ts","is_bot"}]}
        self._history: dict[str, list[dict]] = {}

    def record(self, event, message_id: str | None = None, is_bot: bool = False) -> None:
        """记录一条消息到上下文。"""
        try:
            group_id = str(event.get_group_id())
        except Exception:
            return
        if not group_id or group_id == "None":
            return

        item = {
            "user_id": str(self._safe(event.get_sender_id)),
            "name": self._safe(event.get_sender_name),
            "text": (getattr(event, "message_str", "") or "").strip()[:200],
            "message_id": str(message_id) if message_id else None,
            "ts": time.time(),
            "is_bot": is_bot,
        }
        self._history.setdefault(group_id, []).append(item)
        if len(self._history[group_id]) > self.max_len:
            self._history[group_id] = self._history[group_id][-self.max_len:]

    def recent(self, group_id: str, limit: int = 15) -> list[dict]:
        """获取最近 limit 条消息（按时间正序）。"""
        return list(self._history.get(str(group_id), []))[-limit:]

    def format_for_llm(self, group_id: str, limit: int = 15) -> str:
        """把最近消息格式化为供 LLM 阅读的文本。"""
        rows = self.recent(group_id, limit)
        if not rows:
            return "（暂无历史消息）"
        lines = []
        for r in rows:
            who = "机器人" if r["is_bot"] else f"{r['name']}({r['user_id']})"
            lines.append(f"{who}: {r['text']}")
        return "\n".join(lines)

    def find_recent_offender(self, group_id: str, minutes: float = 5) -> str | None:
        """找出最近发言最频繁的用户（可能正在刷屏）。"""
        cutoff = time.time() - minutes * 60
        rows = [r for r in self.recent(group_id, self.max_len) if r["ts"] >= cutoff and not r["is_bot"]]
        if not rows:
            return None
        counter: dict[str, int] = {}
        for r in rows:
            counter[r["user_id"]] = counter.get(r["user_id"], 0) + 1
        return max(counter, key=counter.get)

    @staticmethod
    def _safe(func):
        try:
            return func() or ""
        except Exception:
            return ""
