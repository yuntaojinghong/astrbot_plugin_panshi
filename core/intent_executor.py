"""意图执行器：把 IntentParser 的结构化结果落到实际群管操作。

负责：
- target 解析（reply / at / QQ号 / 名字模糊匹配 / recent_offender）
- 高风险操作的确认机制
- 调用 NormalHandle / WarningHandle 执行
"""

from __future__ import annotations

import re

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

from ..utils import get_ats, parse_duration


class IntentExecutor:
    """执行智能识别的结果。"""

    def __init__(self, config, storage, context, normal, warning, automate=None):
        self.cfg = config
        self.db = storage
        self.ctx = context
        self.normal = normal
        self.warning = warning
        # AutomateHandle（可选）：set_curfew 需要它即时启停宵禁任务
        self.automate = automate
        # 待确认操作: {(gid, uid): {"intent": dict, "expire": ts}}
        self._pending_confirm: dict[tuple, dict] = {}

    # ========== 入口 ==========
    async def execute(self, event, intent: dict) -> str | None:
        action = intent.get("action", "none")
        if action == "none":
            return None

        # 解析目标
        target = await self._resolve_target(event, intent)

        # 高风险操作需确认
        if action in ("kick", "block") and self.cfg.smart.get("smart_confirm_dangerous", True):
            return self._ask_confirm(event, intent, target)

        return await self._dispatch(event, intent, target)

    async def confirm(self, event, reply_text: str) -> str | None:
        """处理用户对高风险操作的确认回复。"""
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        key = (group_id, user_id)
        pending = self._pending_confirm.get(key)
        if not pending:
            return None

        import time

        if time.time() > pending["expire"]:
            self._pending_confirm.pop(key, None)
            return None

        answer = (reply_text or "").strip().lower()
        if answer in ("确认", "确定", "是", "yes", "y", "ok", "同意"):
            self._pending_confirm.pop(key, None)
            return await self._dispatch(event, pending["intent"], pending["target"])
        elif answer in ("取消", "不用", "否", "no", "n", "算了"):
            self._pending_confirm.pop(key, None)
            return "✅ 已取消该操作。"
        return None

    def has_pending(self, event) -> bool:
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        return (group_id, user_id) in self._pending_confirm

    # ========== 目标解析 ==========
    async def _resolve_target(self, event, intent: dict) -> str | None:
        target = intent.get("target")
        if not target:
            return None
        target = str(target).strip()

        if target == "reply":
            return self._reply_sender(event)
        if target == "at":
            ats = get_ats(event)
            return ats[0] if ats else None
        if target == "recent_offender":
            return self.ctx.find_recent_offender(str(event.get_group_id()))
        if target.isdigit():
            return target

        # 名字模糊匹配群成员
        matched = await self._match_by_name(event, target)
        return matched or target

    async def _match_by_name(self, event, name: str) -> str | None:
        """在群成员中按昵称/名片模糊匹配。"""
        try:
            ok, members = await self.normal.call_api(
                event, "get_group_member_list", group_id=int(event.get_group_id())
            )
            if not ok or not isinstance(members, list):
                return None
            name = name.strip()
            # 精确匹配优先
            for m in members:
                for field in ("card", "nickname"):
                    if m.get(field) and str(m[field]) == name:
                        return str(m.get("user_id"))
            # 包含匹配
            for m in members:
                for field in ("card", "nickname"):
                    if m.get(field) and name and name in str(m[field]):
                        return str(m.get("user_id"))
        except Exception:
            pass
        return None

    @staticmethod
    def _reply_sender(event) -> str | None:
        try:
            from astrbot.api.message_components import Reply

            for seg in event.get_messages():
                if isinstance(seg, Reply):
                    sid = getattr(seg, "sender_id", None)
                    if sid:
                        return str(sid)
        except Exception:
            pass
        return None

    # ========== 确认机制 ==========
    def _ask_confirm(self, event, intent: dict, target: str | None) -> str:
        import time

        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        self._pending_confirm[(group_id, user_id)] = {
            "intent": intent,
            "target": target,
            "expire": time.time() + 60,
        }
        action_name = {"kick": "踢出", "block": "踢出并拉黑"}.get(intent.get("action"), "操作")
        reason = intent.get("reason", "")
        tip = f"{action_name} {target or '（未明确对象）'}"
        if reason:
            tip += f"，原因：{reason}"
        return f"⚠️ 即将执行高风险操作：{tip}\n请在 60 秒内回复「确认」执行，或「取消」放弃。"

    # ========== 分发执行 ==========
    async def _dispatch(self, event, intent: dict, target: str | None) -> str | None:
        action = intent.get("action")

        if action == "ban":
            seconds = self._duration(event, intent, self.cfg.default_ban_time)
            return await self.normal.set_ban(event, target, seconds)
        if action == "unban":
            return await self.normal.cancel_ban(event, target)
        if action == "kick":
            return await self.normal.kick(event, target, reject=False, reason=intent.get("reason", ""))
        if action == "block":
            return await self.normal.kick(event, target, reject=True, reason=intent.get("reason", ""))
        if action == "recall":
            return await self.normal.recall(event, count=int(intent.get("count", 1) or 1))
        if action == "purge":
            return await self.normal.purge(event, count=int(intent.get("count", 30) or 30))
        if action == "whole_ban":
            return await self.normal.whole_ban(event, enable=bool(intent.get("enable", True)))
        if action == "warn":
            return await self.warning.add_warning(event, target, intent.get("reason", ""))
        if action == "query_warn":
            return await self.warning.query_warning(event, target)
        if action == "set_card":
            return await self.normal.set_card(event, target, str(intent.get("name", "")))
        if action == "set_title":
            return await self.normal.set_special_title(event, target, str(intent.get("title", "")))
        if action == "set_admin":
            return await self.normal.set_admin(event, target, enable=True)
        if action == "unset_admin":
            return await self.normal.set_admin(event, target, enable=False)
        if action == "notice":
            return await self.normal.send_notice(event, str(intent.get("content", "")))
        if action == "set_name":
            return await self.normal.set_group_name(event, str(intent.get("name", "")))
        if action == "essence":
            return await self.normal.set_essence(event, enable=bool(intent.get("enable", True)))
        if action == "set_curfew":
            return await self.set_curfew(intent)
        return None

    # ========== 宵禁设置（自然语言） ==========
    async def set_curfew(self, intent: dict) -> str:
        """处理 set_curfew 意图：写配置 + 立即启停宵禁任务。

        支持「宵禁改到23点半到7点」「开启宵禁」「关闭宵禁」等说法；
        只带部分参数时，未提及的项保持原值。
        """
        payload: dict = {}
        enable = intent.get("enable")
        if isinstance(enable, bool):
            payload["curfew_enable"] = enable

        for key in ("start", "end"):
            raw = intent.get(key)
            if raw in (None, ""):
                continue
            norm = _norm_hhmm(raw)
            if norm is None:
                return f"❌ 宵禁{ '开始' if key == 'start' else '结束' }时间「{raw}」看不懂，请用 HH:MM 格式，如 23:30。"
            payload[f"curfew_{key}"] = norm

        if not payload:
            return "🤔 没听懂要怎么调整宵禁。可以说「宵禁改到 23:30-07:00」或「关闭宵禁」。"

        start = payload.get("curfew_start") or self.cfg.get("automate", "curfew_start", "23:00")
        end = payload.get("curfew_end") or self.cfg.get("automate", "curfew_end", "07:00")
        if start == end:
            return "❌ 宵禁开始和结束时间不能相同。"

        try:
            self.cfg.apply_payload({"automate": payload})
        except ValueError as e:
            return f"❌ 参数无效：{e}"

        # 立即同步：启动/停止后台任务，必要时马上开/关全体禁言
        state = "off"
        if self.automate is not None:
            try:
                state = await self.automate.apply_now()
            except Exception as e:  # pragma: no cover - 防御性
                logger.warning(f"[磐石] 宵禁即时同步失败: {e}")

        changed = []
        if "curfew_enable" in payload:
            changed.append("已开启" if payload["curfew_enable"] else "已关闭")
        if "curfew_start" in payload or "curfew_end" in payload:
            changed.append(f"时段 {start} ~ {end}")

        tail = {
            "banned_now": "\n🔴 当前正处于宵禁时段，已自动开启全体禁言。",
            "lifted_now": "\n🟢 已解除全体禁言。",
            "waiting": "\n🟢 已开启，到点会自动全体禁言。",
            "in_window": "\n🔴 当前正处于宵禁时段（全体禁言中）。",
            "off": "",
        }.get(state, "")
        return f"🌙 宵禁{' · '.join(changed)}。{tail}"

    def _duration(self, event, intent: dict, default: int) -> int:
        dur = intent.get("duration")
        if dur is None:
            return default
        if isinstance(dur, (int, float)):
            return int(dur)
        return parse_duration(str(dur), default)


def _norm_hhmm(value) -> str | None:
    """把 LLM 给的时间归一化为 "HH:MM"，识别不了返回 None。

    接受 "23:30" / "7:00" / "2330" / 730（数字）/ "23时30分" 等常见形态。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None

    # HH:MM / H:MM（支持全角冒号）
    m = re.match(r"^(\d{1,2})[:：](\d{1,2})$", text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
        return None

    # HHMM 纯数字（LLM 偶尔输出 2330 / 700）
    digits = re.sub(r"\D", "", text)
    if len(digits) in (3, 4) and digits.isdigit():
        h, mi = int(digits[:-2]), int(digits[-2:])
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return f"{h:02d}:{mi:02d}"
    return None
