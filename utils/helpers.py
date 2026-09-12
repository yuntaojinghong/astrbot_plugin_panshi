"""通用辅助函数：取 @ 对象、取昵称、取图片 URL、取引用 ID。"""

from __future__ import annotations


def get_ats(event) -> list[str]:
    """取出消息中所有被 @ 的 QQ 号（排除 @全体）。"""
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


async def get_nickname(event, user_id: str | int) -> str:
    """获取群成员的群名片 / 昵称，失败时回退为 QQ 号。"""
    uid = int(user_id)
    gid = _group_id(event)
    if gid:
        try:
            info = await event.bot.get_group_member_info(
                group_id=gid, user_id=uid, no_cache=False
            )
            # 不同适配器字段名可能不同
            if isinstance(info, dict):
                card = info.get("card") or ""
                nick = info.get("nickname") or info.get("nick") or ""
                name = card or nick
                if name:
                    return str(name)
        except Exception:
            pass
    try:
        if str(uid) == str(event.get_sender_id()):
            return event.get_sender_name() or str(uid)
    except Exception:
        pass
    return str(uid)


def extract_image_url(event) -> str | None:
    """从消息中提取图片 URL。"""
    try:
        from astrbot.api.message_components import Image

        for seg in event.get_messages():
            if isinstance(seg, Image):
                url = getattr(seg, "url", None) or getattr(seg, "file", None)
                if url:
                    return str(url)
    except Exception:
        pass
    return None


def get_reply_id(event) -> str | None:
    """取出被引用消息的 message_id。"""
    try:
        from astrbot.api.message_components import Reply

        for seg in event.get_messages():
            if isinstance(seg, Reply):
                mid = getattr(seg, "id", None)
                if mid:
                    return str(mid)
    except Exception:
        pass
    return None


def get_group_id(event) -> str:
    """取出当前群号，取不到返回空串。"""
    return _group_id(event)


async def get_member_role(event, user_id: str | int) -> str:
    """查询某成员在本群的角色。

    Returns:
        ``"owner"`` / ``"admin"`` / ``"member"``；查询失败返回 ``"unknown"``
    """
    uid = int(user_id)
    gid = _group_id(event)
    if not gid:
        return "unknown"
    try:
        info = await event.bot.get_group_member_info(
            group_id=int(gid), user_id=uid, no_cache=False
        )
        role = (info or {}).get("role") if isinstance(info, dict) else None
        if role in ("owner", "admin", "member"):
            return role
    except Exception:
        pass
    return "unknown"


async def get_bot_role(event) -> str:
    """查询机器人自己在当前群的角色。"""
    try:
        self_id = event.get_self_id()
    except Exception:
        return "unknown"
    return await get_member_role(event, self_id)


def role_label(role: str) -> str:
    """角色中文名。"""
    return {
        "owner": "群主",
        "admin": "管理员",
        "member": "普通成员",
    }.get(role, "未知身份")


def _group_id(event) -> str | None:
    try:
        gid = event.get_group_id()
        return str(gid) if gid else None
    except Exception:
        return None
