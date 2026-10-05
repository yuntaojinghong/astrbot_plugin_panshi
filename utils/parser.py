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

# 匹配 "1h30m" "30秒" "10分钟" 这类组合，也匹配纯数字。
# 注意必须接受小数：此前只匹配 (\d+)，"1.5h" 会被拆成 "1"+"5h" 累加成 18001 秒。
_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d|秒|分钟|分|小时|时|天|日)?",
    re.IGNORECASE,
)

# 单个 token 是否「看起来就是时长」：``10m`` / ``10分钟`` / ``600`` / ``1d``。
# 裸数字限制在 4 位以内，避免把 5-12 位 QQ 号误判为时长。
_DURATION_TOKEN_RE = re.compile(
    r"(?:\d{1,4}(?:\.\d+)?(?:s|sec|m|min|h|hr|d|秒|分钟|分|小时|时|天|日)"
    r"|\d{1,4}(?:\.\d+)?)",
    re.IGNORECASE,
)


def safe_int(value, default: int | None = None) -> int | None:
    """把可能不是纯数字的值安全地转成 int，绝不抛异常。

    OneBot 的 ``message_id`` / ``user_id`` 并不保证是十进制数字：
    - 有些协议端（如 NapCat / Lagrange）的 ``message_id`` 是 32 位十六进制
      字符串，例如 ``ba2aa5429ac14ea5968fcb797d35c952``；
    - 引用消息、转发消息里也可能混入非数字内容。

    直接 ``int()`` 会抛 ``ValueError: invalid literal for int() with base 10``
    并导致整条指令崩溃。此函数把这种输入统一收敛为 ``default``。

    Args:
        value: 待转换的值（str / int / float / None / 其他）。
        default: 无法转换时返回的值，默认 None。

    Returns:
        转换成功返回 int，否则返回 default。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    text = str(value).strip()
    if not text:
        return default
    # 允许 "123" / "-123" / " 123 "
    if re.fullmatch(r"[+-]?\d+", text):
        try:
            return int(text)
        except (TypeError, ValueError):
            return default
    return default


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

    Note:
        裸数字（不带单位）只在 **4 位以内** 才当作秒。QQ 号是 5-12 位数字，
        此前 ``/禁言 123456789 10m`` 会把 QQ 号解析成 123456789 秒并触发
        30 天上限。这里宁可回退默认值，也不把 QQ 号当时长。
    """
    if text is None:
        return default
    if isinstance(text, int):
        return text

    # CQ 码（如 [CQ:at,qq=123456789]）必须整段剥掉，否则其中的 QQ 号会被
    # _DURATION_RE 当成一个裸数字累加进总时长。
    text = re.sub(r"\[CQ:[^\]]*\]", " ", str(text)).strip()
    if not text:
        return default

    # 特殊词
    if any(word in text for word in ("无限", "永久", "forever", "∞")):
        return 2592000  # 30 天

    if not re.search(r"\d", text):
        return default

    total = 0.0
    matched = False
    for num, unit in _DURATION_RE.findall(text):
        if not num:
            continue
        unit_lower = (unit or "").lower()
        # 裸数字（无单位）只在 4 位以内按秒解释；更长的数字是 QQ 号而非时长。
        if not unit_lower:
            digits = num.split(".")[0]
            if len(digits) > 4:
                continue
        matched = True
        # 中文 "分" 优先按分钟，避免与 "分钟" 冲突
        multiplier = _UNIT_MAP.get(unit_lower, 1 if not unit else 60)
        try:
            value = float(num)
        except (TypeError, ValueError):
            continue
        # 纯裸小数（"1.5" 没带单位）含义不明，按秒向下取整。
        total += value * multiplier

    if not matched:
        return default

    # 仅有裸数字时按秒；小数向下取整，至少 1 秒
    seconds = int(total)
    if total > 0 and seconds <= 0:
        seconds = 1
    return seconds if seconds > 0 else default


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


def parse_amount(text: str, *, default: int | None = None,
                 maximum: int = 1_000_000) -> tuple[int | None, str]:
    """从指令文本里解析出「加/扣多少积分」。

    这是积分指令最容易被写坏的地方：消息里往往**同时**有别人的 QQ 号和数量，
    直接取数字就会把 QQ 号（如 2226175932）当成积分，
    变成「给某人加 22 亿分」。

    区分办法按可靠性排序：

    1. 带正负号或数量单位的数字（``+10`` / ``10分`` / ``10积分``）——最明确
    2. 明确写出的 ``数量`` / ``num`` / ``amount`` 参数
    3. **排除 QQ 号后取第一个数字**
       ——指令格式是「目标 数量 [理由]」，数量在理由**之前**，
       所以取第一个而不是最后一个；否则「连刷 3 条」里的 3 会被当数量
    4. 都没有则用 ``default``

    什么算"像 QQ 号"：位数 ≥ 9 的整数。QQ 号至少 5 位但常见的是 9~11 位，
    而积分数量很少超过 8 位（超过 ``maximum`` 也会被拒），
    所以用 9 位做分界；再配合 ``maximum`` 双重兜底。

    Args:
        text: 待解析文本（一般是指令后面的参数部分）。
        default: 一个数字都找不到时的默认值；``None`` 表示要求用户明确给出。
        maximum: 上限，超过视为无效。

    Returns:
        ``(数量, 说明)``。数量为 ``None`` 表示没解析出来。
    """
    raw = str(text or "").strip()
    if not raw:
        return (default, "默认值") if default is not None else (None, "缺少数量")

    # 1) 带符号或单位：最明确的写法
    m = re.search(r"([+-]\s*\d+|\d+\s*(?:积分|分|点|points?))", raw, re.IGNORECASE)
    if m:
        digits = re.search(r"\d+", m.group(1))
        if digits:
            val = int(digits.group(0))
            if m.group(1).lstrip().startswith("-"):
                val = -val
            if abs(val) <= maximum:
                return val, "带符号/单位"
            return None, f"数量 {val} 超出上限 {maximum}"

    # 2) 明确写出的关键字参数
    m = re.search(r"(?:数量|num|amount)\s*[:=]?\s*(\d+)", raw, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if abs(val) <= maximum:
            return val, "显式数量"

    # 3) 排除 QQ 号后取第一个数字
    for mm in re.finditer(r"\d+", raw):
        token = mm.group(0)
        if len(token) >= _QQ_MIN_DIGITS:
            continue                      # 像 QQ 号，跳过
        val = int(token)
        if val <= maximum:
            return val, "首个有效数字"

    if default is not None:
        return default, "默认值（没找到有效数量）"
    return None, "没找到数量（数字都像 QQ 号或超出上限）"


#: 达到这个位数的整数视为「像 QQ 号」，不作为数量候选。
#: QQ 号常见 9~11 位；积分数量超过 8 位没有实际意义。
_QQ_MIN_DIGITS = 9


def strip_amount(text: str, amount: int) -> str:
    """从指令文本里去掉「数量」与 @目标，剩下的当理由。

    「@小明 10 表现好」→ ``表现好``。

    只剥掉**第一个**匹配到的数量写法，并且对"裸数字"只剥掉**第一个**
    非 QQ 号数字——理由里常带数字（「连刷 3 条」），那些要保住。
    """
    raw = str(text or "")

    # 与 parse_amount 相同的优先级：带符号/单位 > 关键字 > 首个裸数字
    for pat in (r"[+-]\s*\d+", r"\d+\s*(?:积分|分|点|points?)",
                r"(?:数量|num|amount)\s*[:=]?\s*\d+"):
        m = re.search(pat, raw, re.IGNORECASE)
        if m:
            raw = raw[:m.start()] + " " + raw[m.end():]
            break
    else:
        for mm in re.finditer(r"\d+", raw):
            if len(mm.group(0)) >= _QQ_MIN_DIGITS:
                continue
            raw = raw[:mm.start()] + " " + raw[mm.end():]
            break

    # 去掉 @某人 文本与独立出现的 QQ 号，再清掉多余分隔符
    raw = re.sub(r"@\S+", " ", raw)
    raw = re.sub(r"(?<!\d)\d{%d,}(?!\d)" % _QQ_MIN_DIGITS, " ", raw)
    return re.sub(r"\s+", " ", raw).strip(" ,，。.、:：")


def parse_switch(text: str) -> bool | None:
    """把开关参数解析成 True/False；认不出来返回 None（表示「只是查询」）。

    只认明确的开关词。空字符串返回 None——这样「/某开关」不带参数
    就是查询状态，而不是被误改成关闭。
    """
    t = str(text or "").strip().lower()
    if not t:
        return None
    if t in ("on", "开", "开启", "启用", "打开", "true", "1", "yes", "是"):
        return True
    if t in ("off", "关", "关闭", "停用", "停", "false", "0", "no", "否"):
        return False
    return None


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
