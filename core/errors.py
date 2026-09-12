"""OneBot / ActionFailed 错误信息人性化。

协议端返回的错误是英文机器码（`retcode=1200 cannot ban admin`），
直接回给群友既看不懂、也不知道下一步该做什么。这里把常见错误
翻译成中文提示，并给出可操作的建议。

无法识别的错误会退化为「去掉异常壳的原始 message」，至少不再显示
`<ActionFailed status='failed', retcode=...>` 这种调试代码。
"""

from __future__ import annotations

import re

# 常见 retcode 常量（OneBot v11 通用）
RETCODE_OK = 0
RETCODE_ASYNC = 1
RETCODE_BAD_REQUEST = 100
RETCODE_BAD_PARAM = 101
RETCODE_NOT_FOUND = 102
RETCODE_FORBIDDEN = 103
RETCODE_FAILED = 1200

# 已知 message -> 中文解释
_MESSAGE_MAP = {
    "cannot ban admin": "对方是管理员或群主，机器人无权禁言",
    "cannot kick admin": "对方是管理员或群主，机器人无权踢出",
    "cannot ban owner": "对方是群主，任何人都无法对其操作",
    "cannot kick owner": "对方是群主，任何人都无法对其操作",
    "not group admin": "机器人不是本群管理员，无法执行该操作",
    "cannot set admin": "无法设置该成员为管理员",
    "cannot set group name": "机器人无权修改群名",
    "group not found": "群不存在或机器人已不在该群",
    "user not found": "找不到该成员，可能已退群",
    "no permission": "机器人权限不足，请先授予管理员",
    "invalid group id": "群号无效",
    "invalid user id": "QQ 号无效",
    "msg not found": "消息不存在或已被撤回（撤回超过 2 分钟的消息会失败）",
    "message not found": "消息不存在或已被撤回（撤回超过 2 分钟的消息会失败）",
    "duration invalid": "禁言时长无效（QQ 限制最长 30 天）",
    "set_group_ban failed": "禁言失败，可能时长超出限制或权限不足",
    "already banned": "该成员已被禁言",
    "not in group": "该成员不在此群",
}


def _strip_exception_shell(err: object) -> str:
    """从异常对象/字符串里抽出可读的 message。

    典型输入::

        <ActionFailed status='failed', retcode=1200, data=None,
         message='cannot ban admin', wording='cannot ban admin', ...>

    取出 ``cannot ban admin``。
    """
    text = str(err or "").strip()
    if not text:
        return ""
    # 优先取 message='...' / message="..."
    m = re.search(r"\bmessage\s*=\s*(['\"])(.*?)\1", text, re.S)
    if m:
        return m.group(2).strip()
    # 其次取 wording='...'
    m = re.search(r"\bwording\s*=\s*(['\"])(.*?)\1", text, re.S)
    if m:
        return m.group(2).strip()
    return text


def extract_retcode(err: object) -> int | None:
    """从错误里抽 retcode。"""
    m = re.search(r"\bretcode\s*=\s*(-?\d+)", str(err or ""))
    return int(m.group(1)) if m else None


def humanize(err: object) -> str:
    """把协议端错误翻译成中文可读提示。"""
    raw = _strip_exception_shell(err)
    if not raw:
        return "协议端未返回具体原因"

    key = raw.strip().lower().rstrip(".")

    # 精确匹配
    if key in _MESSAGE_MAP:
        return _MESSAGE_MAP[key]

    # 包含匹配（协议端可能加前后缀）
    for pat, tip in _MESSAGE_MAP.items():
        if pat in key:
            return tip

    # 时长相关
    if "duration" in key and ("invalid" in key or "range" in key):
        return "禁言时长无效（QQ 限制最长 30 天）"

    # 权限相关兜底
    if "admin" in key and ("cannot" in key or "not" in key):
        return "机器人权限不足，无法操作管理员或群主"

    retcode = extract_retcode(err)
    if retcode in (RETCODE_FORBIDDEN, RETCODE_BAD_REQUEST):
        return f"协议端拒绝了该操作（{raw}）"

    # 未识别：至少把机器码去掉
    return raw


def hint_for(action: str) -> str:
    """针对某类操作给出补充建议（放在提示末尾）。"""
    if action in ("set_group_ban", "set_group_kick"):
        return "提示：机器人需为管理员，且不能操作群主或其他管理员。"
    if action == "delete_msg":
        return "提示：只能撤回 2 分钟内的消息，且需要管理员权限。"
    if action in ("set_group_name", "set_group_portrait"):
        return "提示：修改群资料需要机器人是群主。"
    if action == "_send_group_notice":
        return "提示：发布群公告需要机器人是管理员。"
    return ""
