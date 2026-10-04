"""权限系统：超级管理员 > 群主 > 管理员 > 成员。"""

from __future__ import annotations

import time
from enum import IntEnum
from functools import wraps

#: 群身份查询结果的缓存：{(group_id, user_id): (role, 时间戳)}
#: 权限判定在消息处理路径上会被调用多次，逐个查协议端太浪费；角色变动很少，
#: 缓存 60 秒足够，同时改权限后最多 1 分钟就能生效。
_ROLE_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_ROLE_TTL = 60.0


class PermLevel(IntEnum):
    """权限等级，数值越大权限越高。"""

    MEMBER = 0  # 普通成员
    ADMIN = 1  # 群管理员
    OWNER = 2  # 群主
    SUPER = 3  # 插件超级管理员


def _safe_sender(event) -> str:
    try:
        return str(event.get_sender_id() or "")
    except Exception:
        return ""


def _safe_group(event) -> str:
    try:
        return str(event.get_group_id() or "")
    except Exception:
        return ""


async def get_group_role(event, user_id: str) -> str:
    """异步查询某成员在本群的角色。

    .. important::
        必须走协议端查询，**不能**读 ``event.message_obj.sender.role``。

        AstrBot 的 ``MessageMember`` 只定义了 ``user_id`` 与 ``nickname``
        两个字段，适配器也从不下发 ``role``。所以那种读法恒为空串——
        于是「群主/管理员发指令」被判成普通成员，直接回「⛔ 权限不足」。

        这正是用户实测反馈的现象：自己明明是管理员，机器人却说他没有权限。
        协议端查询本模块已有可靠实现（``normal._check_target_role`` 用的就是它），
        这里复用同一套两层取数逻辑。

    Returns:
        ``"owner"`` / ``"admin"`` / ``"member"``；无法确证时返回 ``""``。
    """
    uid = str(user_id or "").strip()
    gid = _safe_group(event)
    if not uid or not gid:
        return ""

    key = (gid, uid)
    now = time.time()
    hit = _ROLE_CACHE.get(key)
    if hit and now - hit[1] < _ROLE_TTL:
        return hit[0]

    role = ""
    bot = getattr(event, "bot", None)
    if bot is not None:
        # 第一层：单人查询
        try:
            info = await bot.get_group_member_info(
                group_id=int(gid), user_id=int(uid), no_cache=False
            )
            if isinstance(info, dict):
                got = str(info.get("role") or "").strip().lower()
                if got in ("owner", "admin", "member"):
                    role = got
        except Exception:
            pass

        # 第二层：单人查询拿不到 owner/admin 时，用成员列表交叉验证。
        # 协议端对 role 的上报并不总是可靠，多一层更稳。
        if role in ("", "member"):
            try:
                members = await bot.get_group_member_list(group_id=int(gid))
                for m in members or []:
                    if not isinstance(m, dict):
                        continue
                    if str(m.get("user_id") or "") != uid:
                        continue
                    got = str(m.get("role") or "").strip().lower()
                    if got in ("owner", "admin", "member"):
                        role = got
                    break
            except Exception:
                pass

    if role:
        _ROLE_CACHE[key] = (role, now)
    return role


async def get_user_level_async(event, super_admins: list[str] | None = None) -> PermLevel:
    """异步判定权限等级（会查协议端，结果准确）。"""
    sender = _safe_sender(event)
    super_admins = [str(x) for x in (super_admins or [])]

    if sender and sender in super_admins:
        return PermLevel.SUPER

    try:
        if event.is_admin():
            return PermLevel.ADMIN
    except Exception:
        pass

    role = await get_group_role(event, sender)
    if role == "owner":
        return PermLevel.OWNER
    if role == "admin":
        return PermLevel.ADMIN

    return PermLevel.MEMBER


def get_user_level(event, super_admins: list[str] | None = None) -> PermLevel:
    """同步判定权限等级（只做零成本的本地判断）。

    .. warning::
        本函数**拿不到群主/管理员身份**，因为那需要异步查协议端。
        它只用于「已经确定无群身份信息也无妨」的同步场景
        （例如风控豁免的快速预判）。

        真正决定「要不要放行管理指令」的地方，必须用
        :func:`get_user_level_async`，否则会把群管理员误判成普通成员。
    """
    sender = _safe_sender(event)
    super_admins = [str(x) for x in (super_admins or [])]

    if sender and sender in super_admins:
        return PermLevel.SUPER

    try:
        if event.is_admin():
            return PermLevel.ADMIN
    except Exception:
        pass

    # 命中了缓存就直接用（可能来自本进程早先的异步查询）
    hit = _ROLE_CACHE.get((_safe_group(event), sender))
    if hit and time.time() - hit[1] < _ROLE_TTL:
        if hit[0] == "owner":
            return PermLevel.OWNER
        if hit[0] == "admin":
            return PermLevel.ADMIN

    return PermLevel.MEMBER


async def check_permission_async(event, required: PermLevel,
                                 super_admins: list[str] | None = None) -> bool:
    """异步权限校验（推荐用于所有会真正执行动作的地方）。"""
    return await get_user_level_async(event, super_admins) >= required


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
            # 必须用异步版本：同步版拿不到群主/管理员身份
            if not await check_permission_async(event, required, supers):
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
