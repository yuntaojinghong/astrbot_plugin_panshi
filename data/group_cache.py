"""群列表自动识别与缓存。

通过 OneBot 适配器的 ``get_group_list`` 拉取机器人所在的所有群，
并补充群主、机器人在群内的角色等信息，供 WebUI 面板展示。

群信息会缓存一段时间（默认 60 秒），避免面板刷新频繁打扰协议端。
"""

from __future__ import annotations

import time
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger("panshi")

# 缓存有效期（秒）
DEFAULT_TTL = 60


class GroupInfoCache:
    """群信息缓存。

    Args:
        context: AstrBot 的 Context，用来枚举平台适配器
        ttl: 缓存有效期（秒）
    """

    def __init__(self, context: Any, ttl: int = DEFAULT_TTL):
        self.context = context
        self.ttl = ttl
        self._groups: list[dict] = []
        self._updated_at: float = 0.0
        self._last_error: str = ""

    # ---------- 对外接口 ----------
    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def updated_at(self) -> float:
        return self._updated_at

    def invalidate(self) -> None:
        """强制下次调用时重新拉取。"""
        self._updated_at = 0.0

    def snapshot(self) -> list[dict]:
        """返回当前缓存（不触发网络请求）。"""
        return [dict(g) for g in self._groups]

    async def list_groups(self, force: bool = False) -> list[dict]:
        """获取群列表，必要时刷新缓存。

        Returns:
            群信息字典列表，每项::

                {
                  "group_id": "123456",
                  "group_name": "示例群",
                  "member_count": 128,
                  "max_member_count": 500,
                  "owner_id": "789",
                  "bot_role": "admin" | "member" | "owner" | "unknown",
                  "enabled": true
                }
        """
        now = time.time()
        if not force and self._groups and (now - self._updated_at) < self.ttl:
            return self.snapshot()
        await self.refresh()
        return self.snapshot()

    async def refresh(self) -> list[dict]:
        """从协议端拉取最新群列表。"""
        raw: list[dict] = []
        error = ""
        try:
            raw, error = await self._fetch_group_list()
        except Exception as e:  # pragma: no cover - 防御性
            error = str(e)
            logger.warning(f"[磐石] 拉取群列表异常: {e}")

        if error:
            self._last_error = error
        elif raw or not self._groups:
            self._last_error = ""

        if raw:
            groups = [self._normalize(g) for g in raw]
            groups.sort(key=lambda g: _num(g.get("member_count")), reverse=True)
            self._groups = groups
            self._updated_at = time.time()
        elif not self._groups:
            # 首次拉取失败，保留空列表
            self._updated_at = time.time()

        return self.snapshot()

    # ---------- 内部实现 ----------
    async def _fetch_group_list(self) -> tuple[list[dict], str]:
        """遍历平台适配器，调用 get_group_list。"""
        platforms = self._iter_platforms()
        if not platforms:
            return [], "未找到可用的平台适配器，请确认已连接 NapCat"

        last_err = ""
        for platform in platforms:
            client = None
            try:
                if hasattr(platform, "get_client"):
                    client = platform.get_client()
            except Exception as e:
                last_err = str(e)
                continue
            if client is None:
                continue

            call = getattr(client, "call_action", None)
            if not callable(call):
                continue
            try:
                resp = await call("get_group_list")
            except Exception as e:
                last_err = str(e)
                continue

            groups = _extract_list(resp)
            if groups is not None:
                return groups, ""
            last_err = last_err or "协议端返回了无法识别的数据"

        return [], last_err or "未能从协议端获取群列表"

    def _iter_platforms(self) -> list[Any]:
        """枚举所有平台实例。"""
        out: list[Any] = []
        try:
            pm = getattr(self.context, "platform_manager", None)
            if pm is None:
                return out
            instances = getattr(pm, "get_instances", None)
            if callable(instances):
                got = instances()
                if isinstance(got, dict):
                    out.extend(got.values())
                elif isinstance(got, (list, tuple)):
                    out.extend(got)
        except Exception as e:
            logger.warning(f"[磐石] 枚举平台实例失败: {e}")
        return out

    @staticmethod
    def _normalize(g: dict) -> dict:
        """把协议端原始群信息归一化为面板用的结构。"""
        gid = str(g.get("group_id", "") or "").strip()
        return {
            "group_id": gid,
            "group_name": str(g.get("group_name", "") or "").strip() or f"群 {gid}",
            "member_count": _num(g.get("member_count")),
            "max_member_count": _num(g.get("max_member_count")),
            "owner_id": str(g.get("owner_id", "") or ""),
            "bot_role": _detect_bot_role(g),
        }


# ======================================================================
#  工具函数
# ======================================================================
def _extract_list(resp: Any) -> list[dict] | None:
    """从各种可能的返回结构里取出群列表。"""
    if isinstance(resp, list):
        return [x for x in resp if isinstance(x, dict)]
    if isinstance(resp, dict):
        # 常见包装：{"data": [...]} / {"retcode":0,"data":[...]}
        for key in ("data", "groups", "list", "result"):
            val = resp.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        # status/retcode 表示失败
        if resp.get("status") == "failed" or resp.get("retcode") not in (None, 0, "0"):
            return None
    return None


def _num(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _detect_bot_role(group: dict) -> str:
    """尽力推断机器人在该群的角色。"""
    for key in ("bot_role", "role", "self_role"):
        val = group.get(key)
        if isinstance(val, str) and val in ("owner", "admin", "member"):
            return val
    return "unknown"
