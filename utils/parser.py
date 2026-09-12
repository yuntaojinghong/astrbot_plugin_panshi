"""解析工具：把"人话"转成程序能用的结构化数据。

- parse_duration: "10m" / "2h" / "1d" / "1h30m" / "600" -> 秒数
- format_duration: 秒数 -> 人话时长
- parse_target: 从消息里解析出操作目标（引用 / @ / QQ号）
"""

from __future__ import annotations

import re

# 时长单位 -> 秒
_UNIT_MAP = {
    "s": 1,
    "sec": 1,
    "秒": 1,
    "m": 60,
    "min": 60,
    "分": 60,
    "分钟": 60,
    "h": 3600,
    "hr": 3600,
    "小时": 3600,
    "时": 3600,
    "d": 86400,
    "天": 86400,
    "日": 86400,
}

# 匹配 "1h30m" "30秒" "10分钟" 这类组合，也匹配纯数字
_DURATION_RE = re.compile(
    r"(\d+)\s*(s|sec|m|min|h|hr|d|秒|分钟|分|小时|时|天|日)?",
    re.IGNORECASE,
)


def parse_duration(text: str | int | None, default: int = 60) -> int:
    """把时长文本解析成秒数。

    支持：
        "600"    -> 600   （纯数字视为秒）
        "10m"    -> 600
        "2h"     -> 7200
        "1d"     -> 86400
        "1h30m"  -> 5400
        "10分钟" -> 600
        "无限"/"永久" -> 2592000（30 天上限）

    Args:
        text: 待解析的文本，int 则直接返回。
        default: 解析失败时的默认值（秒）。

    Returns:
        秒数（int）。
    """
    if text is None:
        return default
    if isinstance(text, int):
        return text

    text = str(text).strip()
    if not text:
        return default

    # 特殊词
    if any(word in text for word in ("无限", "永久", "forever", "∞")):
        return 2592000  # 30 天

    if not re.search(r"\d", text):
        return default

    total = 0
    matched = False
    for num, unit in _DURATION_RE.findall(text):
        if not num:
            continue
        matched = True
        unit_lower = (unit or "").lower()
        # 中文 "分" 优先按分钟，避免与 "分钟" 冲突
        multiplier = _UNIT_MAP.get(unit_lower, 1 if not unit else 60)
        total += int(num) * multiplier

    if not matched:
        return default

    # 仅有裸数字时按秒
    return total if total > 0 else default


def format_duration(seconds: int) -> str:
    """秒数转人话时长，例如 5400 -> "1小时30分钟"。"""
    if seconds <= 0:
        return "0秒"
    if seconds >= 2592000:
        return "30天"

    parts: list[str] = []
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)

    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    if secs and not days:
        parts.append(f"{secs}秒")

    return "".join(parts) or "0秒"


# QQ 号匹配（5-12 位数字）
_QQ_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")
# @ 某人 的文本形式
_AT_TEXT_RE = re.compile(r"@(\S+)")


def parse_target(event, arg_text: str = "") -> tuple[str | None, str]:
    """从消息事件中解析操作目标。

    优先级：引用消息 > @某人 > 文本中的 QQ 号 > 文本中 @的昵称。
    返回值：(user_id 或 None, 来源说明)

    Args:
        event: AstrMessageEvent
        arg_text: 指令后的参数文本，用于兜底匹配 QQ 号
    """
    # 1. 引用消息
    reply_id = get_reply_id_safe(event)
    if reply_id:
        return reply_id, "引用"

    # 2. @某人
    try:
        ats = get_ats_safe(event)
        if ats:
            return ats[0], "at"
    except Exception:
        pass

    text = arg_text or _safe_message_str(event)

    # 3. 文本里的 QQ 号
    m = _QQ_RE.search(text)
    if m:
        return m.group(1), "QQ号"

    return None, ""


def get_reply_id_safe(event) -> str | None:
    """安全地从事件中取出被引用消息的发送者 ID。"""
    try:
        from astrbot.api.message_components import Reply

        chain = event.get_messages()
        for seg in chain:
            if isinstance(seg, Reply):
                # Reply.sender_id 在多数适配器可用
                sid = getattr(seg, "sender_id", None)
                if sid:
                    return str(sid)
    except Exception:
        pass
    return None


def get_ats_safe(event) -> list[str]:
    """安全地从事件中取出所有 @ 的 QQ 号。"""
    result: list[str] = []
    try:
        from astrbot.api.message_components import At

        for seg in event.get_messages():
            if isinstance(seg, At):
                qq = getattr(seg, "qq", None)
                if qq and str(qq) != "all":
                    result.append(str(qq))
    except Exception:
        pass
    return result


def _safe_message_str(event) -> str:
    try:
        return event.message_str or ""
    except Exception:
        return ""
