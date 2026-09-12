"""权限系统：超级管理员 > 群主 > 管理员 > 成员。"""

from __future__ import annotations

from enum import IntEnum
from functools import wraps


class PermLevel(IntEnum):
    """权限等级，数值越大权限越高。"""

    MEMBER = 0  # 普通成员
    ADMIN = 1  # 群管理员
    OWNER = 2  # 群主
    SUPER = 3  # 插件超级管理员


def get_user_level(event, super_admins: list[str] | None = None) -> PermLevel:
    """判断当前消息发送者的权限等级。

    Args:
        event: AstrMessageEvent
        super_admins: 插件配置的超级管理员 QQ 号列表

    Returns:
        PermLevel
    """
    sender = str(_safe_sender(event))
    super_admins = [str(x) for x in (super_admins or [])]

    if sender and sender in super_admins:
        return PermLevel.SUPER

    # AstrBot 提供的 is_admin 判定（一般是全局管理员）
    try:
        if event.is_admin():
            return PermLevel.ADMIN
    except Exception:
        pass

    # 通过群成员信息判断 QQ 群身份
    role = _get_group_role(event, sender)
    if role == "owner":
        return PermLevel.OWNER
    if role == "admin":
        return PermLevel.ADMIN

    return PermLevel.MEMBER


def check_permission(event, required: PermLevel, super_admins: list[str] | None = None) -> bool:
    """检查当前用户是否达到所需权限等级。"""
    return get_user_level(event, super_admins) >= required


def perm_required(required: PermLevel, attr: str = "super_admins"):
    """权限校验装饰器（同步判定，仅用于指令方法）。

    用法::

        @perm_required(PermLevel.ADMIN)
        async def cmd(self, event, ...):
            ...

    Note:
        被装饰方法的所在类需具备 ``_super_admins`` 属性（list[str]）。
    """

    def decorator(func):
        @wraps(func)
        async def wrapper(self, event, *args, **kwargs):
            supers = getattr(self, "_super_admins", []) or []
            if not check_permission(event, required, supers):
                level_name = {
                    PermLevel.MEMBER: "成员",
                    PermLevel.ADMIN: "管理员",
                    PermLevel.OWNER: "群主",
                    PermLevel.SUPER: "超级管理员",
                }.get(required, "管理员")
                yield event.plain_result(f"⛔ 权限不足，该操作需要「{level_name}」及以上权限。")
                return
            async for result in func(self, event, *args, **kwargs):
                yield result

        return wrapper

    return decorator


def _safe_sender(event) -> str:
    try:
        return str(event.get_sender_id())
    except Exception:
        return ""


def _get_group_role(event, user_id: str) -> str:
    """同步获取群成员角色。aiocqhttp 事件对象通常带 role 字段。"""
    try:
        raw = getattr(event, "message_obj", None)
        sender = getattr(raw, "sender", None) if raw else None
        if sender is not None:
            role = getattr(sender, "role", None)
            if role:
                return str(role)
    except Exception:
        pass
    return ""
