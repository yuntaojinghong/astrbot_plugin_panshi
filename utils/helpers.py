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

    可靠性说明：协议端（尤其 NapCat）对 ``get_group_member_info`` 的 ``role``
    字段上报并不总是可信——查询机器人自身时尤其容易返回 ``member`` 或缺失该字段。
    因此这里做两层取数：先按单人查，再回退到群成员列表交叉验证；
    两者都拿不到可靠结论时返回 ``"unknown"``，由调用方决定是否放行
    （绝不可把 unknown 当作「无权限」）。

    Returns:
        ``"owner"`` / ``"admin"`` / ``"member"``；无法确证时返回 ``"unknown"``
    """
    uid = int(user_id)
    gid = _group_id(event)
    if not gid:
        return "unknown"

    # 第一层：单人查询
    role = await _role_from_single(event, gid, uid)
    if role in ("owner", "admin"):
        # 高权限是「确证」——直接采纳
        return role

    # 第二层：单人查询给出 member/unknown 时，用成员列表交叉验证
    listed = await _role_from_list(event, gid, uid)
    if listed in ("owner", "admin", "member"):
        return listed

    return role if role == "member" else "unknown"


async def _role_from_single(event, gid: str, uid: int) -> str:
    """按单人接口取角色。返回 owner/admin/member/unknown。"""
    try:
        info = await event.bot.get_group_member_info(
            group_id=int(gid), user_id=uid, no_cache=True
        )
    except TypeError:
        # 某些适配器不接受 no_cache 关键字
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(gid), user_id=uid
            )
        except Exception:
            return "unknown"
    except Exception:
        return "unknown"

    if isinstance(info, dict):
        role = info.get("role")
        if role in ("owner", "admin", "member"):
            return role
    return "unknown"


async def _role_from_list(event, gid: str, uid: int) -> str:
    """从群成员列表里找该成员的角色（交叉验证用）。"""
    try:
        members = await event.bot.get_group_member_list(group_id=int(gid))
    except Exception:
        return "unknown"
    if isinstance(members, dict):
        # 兼容 {"data": [...]} 形态
        members = members.get("data") or members.get("members") or []
    if not isinstance(members, (list, tuple)):
        return "unknown"
    for m in members:
        if not isinstance(m, dict):
            continue
        if str(m.get("user_id")) != str(uid):
            continue
        role = m.get("role")
        if role in ("owner", "admin", "member"):
            return role
    return "unknown"


async def get_bot_role(event) -> str:
    """查询机器人自己在当前群的角色。"""
    try:
        self_id = event.get_self_id()
    except Exception:
        return "unknown"
    if not self_id:
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
