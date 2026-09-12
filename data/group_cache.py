"""群列表自动识别与缓存。

通过 OneBot 适配器的 ``get_group_list`` 拉取机器人所在的所有群，
并补充群主、机器人在群内的角色等信息，供 WebUI 面板展示。

群信息会缓存一段时间（默认 60 秒），避免面板刷新频繁打扰协议端。

连接判定说明（重要，勿轻易简化）：
- AstrBot 的 aiocqhttp 适配器 ``get_client()`` 返回的 CQHttp 对象**恒不为 None**，
  它只表示"适配器已创建"，不代表 NapCat 已连上反向 WebSocket；
- 真正的连接状态保存在 CQHttp 的内部属性里：
  - ``_wsr_api_clients``: dict {self_id: ws}，能调 API 的连接（universal/api 角色）
  - ``_wsr_event_clients``: set，只收事件的连接（event 角色）
- 多个 QQ 账号（多个 self_id）同时连接时，不带 ``self_id`` 的 ``call_action``
  会直接抛 ``ApiNotAvailable``——因此这里对每个 self_id 显式带参调用；
- 未连接时 ``call_action`` 会空等 api_timeout_sec（AstrBot 配的是 180s），
  会把面板拖死，因此这里统一套 ``asyncio.wait_for`` 短超时。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger("panshi")

# 缓存有效期（秒）
DEFAULT_TTL = 60

# 单次 call_action 的超时（秒）。协议端未连接时 aiocqhttp 默认会等 180s，
# 面板不能跟着卡死，这里用更短的兜底超时。
CALL_TIMEOUT = 20

# 连接状态常量（供面板渲染诊断信息）
ST_CONNECTED = "connected"  # 至少一个账号的 API 通道可用
ST_NO_ADAPTER = "no_adapter"  # 一个平台适配器都没有
ST_NO_CLIENT = "no_client"  # 有适配器，但都拿不到 CQHttp 客户端
ST_EVENT_ONLY = "event_only"  # 只有事件通道，缺 API 通道
ST_NOT_CONNECTED = "not_connected"  # 适配器就绪，但协议端还没连上

MSG_NO_ADAPTER = (
    "未找到平台适配器。请在 AstrBot「平台」页确认已添加并启用 "
    "aiocqhttp 适配器（NapCat 反向 WebSocket）"
)
MSG_NOT_CONNECTED = (
    "已找到平台适配器，但尚未与协议端建立连接。"
    "请检查 NapCat 是否已连接 AstrBot（反向 WebSocket 地址/Token）"
)
MSG_EVENT_ONLY = (
    "协议端已连接，但只建立了事件通道、缺少 API 通道，无法调用接口。"
    "请把 NapCat 的「WebSocket 客户端」连接方式设为 universal（或确保包含 API 通道），"
    "并核对反向 WebSocket 地址与 Token 和 AstrBot 完全一致"
)
MSG_NO_CLIENT = "平台适配器存在，但未取得可用的 OneBot 客户端对象"


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
            error = str(e) or e.__class__.__name__
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

    def connection_status(self) -> dict:
        """诊断当前与协议端（NapCat / OneBot v11）的连接状态。

        Returns:
            {
              "state": ST_*,
              "adapters": 平台适配器总数,
              "clients": 可用的 OneBot 客户端数,
              "self_ids": 已建立 API 通道的机器人 QQ 号列表,
              "event_only": 是否只存在事件通道,
              "message": 面板可直接展示的中文说明,
            }
        """
        status = self._probe()
        message = {
            ST_CONNECTED: f"已连接 {len(status['self_ids'])} 个机器人账号",
            ST_NO_ADAPTER: MSG_NO_ADAPTER,
            ST_NO_CLIENT: MSG_NO_CLIENT,
            ST_EVENT_ONLY: MSG_EVENT_ONLY,
            ST_NOT_CONNECTED: MSG_NOT_CONNECTED,
        }.get(status["state"], "")
        status["message"] = message
        return status

    def iter_clients(self) -> list[tuple[str, Any]]:
        """列出当前可用的 (self_id, CQHttp 客户端)。

        供宵禁等需要主动调用 OneBot API 的模块使用；
        多账号时每个 self_id 各一项，调用方应显式带 self_id 调用。
        """
        candidates, _diag = self._collect_candidates()
        return candidates

    # ---------- 内部实现 ----------
    def _probe(self) -> dict:
        """遍历平台适配器，汇总连接诊断信息。"""
        platforms = self._iter_platforms()
        adapters = len(platforms)
        clients = 0
        api_ids: list[str] = []
        event_only = False

        for platform in platforms:
            client = self._resolve_client(platform)
            if client is None:
                continue
            if not callable(getattr(client, "call_action", None)):
                continue
            clients += 1
            ids = _api_client_ids(client)
            if ids:
                for sid in ids:
                    if sid not in api_ids:
                        api_ids.append(sid)
            elif _has_event_client(client):
                event_only = True

        if api_ids:
            state = ST_CONNECTED
        elif adapters == 0:
            state = ST_NO_ADAPTER
        elif clients == 0:
            state = ST_NO_CLIENT
        elif event_only:
            state = ST_EVENT_ONLY
        else:
            state = ST_NOT_CONNECTED

        return {
            "state": state,
            "adapters": adapters,
            "clients": clients,
            "self_ids": api_ids,
            "event_only": event_only,
        }

    def _collect_candidates(self) -> tuple[list[tuple[str, Any]], dict]:
        """找出可发起 API 调用的 (self_id, client) 候选列表。

        - 能读到 ``_wsr_api_clients`` 时按 self_id 逐个列出（多账号可用）；
        - 确认该实例没有任何 API 连接时直接跳过，避免空等超时；
        - 读不到内部连接信息（异常版本/测试桩）时保守地尝试调用。
        """
        candidates: list[tuple[str, Any]] = []
        status = self._probe()
        platforms = self._iter_platforms()

        seen: set[int] = set()
        for platform in platforms:
            client = self._resolve_client(platform)
            if client is None:
                continue
            if not callable(getattr(client, "call_action", None)):
                continue
            mark = id(client)
            if mark in seen:
                continue
            seen.add(mark)

            ids = _api_client_ids(client)
            if ids:
                for sid in ids:
                    candidates.append((str(sid), client))
            elif not _has_conn_info(client):
                # 读不到连接信息：无法判断是否在线，仍给它一次机会
                candidates.append(("", client))
            # 能读到连接信息且为空 → 该实例确实未连接，跳过

        return candidates, status

    async def _fetch_group_list(self) -> tuple[list[dict], str]:
        """遍历平台适配器，调用 get_group_list 拉取群列表。"""
        platforms = self._iter_platforms()
        if not platforms:
            return [], MSG_NO_ADAPTER

        candidates, status = self._collect_candidates()
        if not candidates:
            if status["state"] == ST_EVENT_ONLY:
                return [], MSG_EVENT_ONLY
            # NO_CLIENT / NOT_CONNECTED 都归结为「尚未建立连接」：
            # 对用户而言含义一致（适配器在、但 NapCat 没连上/拿不到客户端）
            return [], MSG_NOT_CONNECTED

        last_err = ""
        for sid, client in candidates:
            try:
                if sid:
                    resp = await asyncio.wait_for(
                        client.call_action("get_group_list", self_id=sid),
                        timeout=CALL_TIMEOUT,
                    )
                else:
                    resp = await asyncio.wait_for(
                        client.call_action("get_group_list"),
                        timeout=CALL_TIMEOUT,
                    )
            except asyncio.TimeoutError:
                logger.warning(f"[磐石] 协议端 {sid or '(默认)'} 响应超时({CALL_TIMEOUT}s)")
                last_err = (
                    f"协议端({sid or '默认账号'})调用超时，可能未完全连上或负载过高，"
                    "请稍后点「同步」重试"
                )
                continue
            except Exception as e:
                # ApiNotAvailable 的 str(e) 是空串，还原成可读信息
                last_err = str(e) or e.__class__.__name__
                logger.warning(f"[磐石] 协议端 {sid or '(默认)'} 调用失败: {last_err}")
                continue

            groups = _extract_list(resp)
            if groups is not None:
                return groups, ""
            last_err = last_err or "协议端返回了无法识别的数据"

        if not last_err:
            last_err = "未能从协议端获取群列表"
        return [], last_err

    def _iter_platforms(self) -> list[Any]:
        """枚举所有平台实例。

        AstrBot 的 ``PlatformManager`` 把已加载的适配器实例存放在
        ``platform_insts`` 列表中，并提供同步访问器 ``get_insts()``。
        这里按 方法 → 属性 的顺序做多层兜底，兼容不同版本。
        """
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return []

        # 1) 官方访问器 get_insts()（同步，返回 list[Platform]）
        for name in ("get_insts", "get_platform_insts", "get_instances"):
            getter = getattr(pm, name, None)
            if not callable(getter):
                continue
            try:
                got = getter()
            except Exception as e:
                logger.warning(f"[磐石] 调用 platform_manager.{name}() 失败: {e}")
                continue
            out = self._collect(got)
            if out:
                return out

        # 2) 直接读属性
        for name in ("platform_insts", "platforms", "insts"):
            got = getattr(pm, name, None)
            if got is None or callable(got):
                continue
            out = self._collect(got)
            if out:
                return out

        logger.warning(
            "[磐石] 未能从 platform_manager 枚举到平台适配器，"
            "请确认 AstrBot 已连接 NapCat（OneBot v11）"
        )
        return []

    @staticmethod
    def _resolve_client(platform: Any) -> Any | None:
        """从平台实例里解析出 OneBot 客户端（CQHttp）对象。

        不同 AstrBot 版本里客户端的位置不同：
        - 新版：``get_client()`` 返回 ``self.bot``
        - 兜底：直接读 ``.bot`` / ``.client`` / ``.cqhttp`` 属性
        注意 ``get_client()`` 可能返回 None（非 aiocqhttp 平台），
        返回非 None 也不代表协议端已连接。
        """
        getter = getattr(platform, "get_client", None)
        if callable(getter):
            try:
                got = getter()
            except Exception:
                got = None
            if got is not None:
                return got
        for attr in ("bot", "client", "cqhttp"):
            try:
                got = getattr(platform, attr, None)
            except Exception:
                got = None
            if got is not None and callable(getattr(got, "call_action", None)):
                return got
        return None

    @staticmethod
    def _collect(got: Any) -> list[Any]:
        """把各种容器形态统一成平台实例列表。"""
        if got is None:
            return []
        if isinstance(got, dict):
            # {"aiocqhttp": inst} 或 {"id": {"inst": ...}}
            out: list[Any] = []
            for val in got.values():
                if isinstance(val, dict):
                    inst = val.get("inst")
                    if inst is not None:
                        out.append(inst)
                elif val is not None:
                    out.append(val)
            return out
        if isinstance(got, (list, tuple, set)):
            return [x for x in got if x is not None]
        return []

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
#  CQHttp 连接信息读取
# ======================================================================
def _api_client_ids(client: Any) -> list[str]:
    """读出已建立 API 通道的机器人 QQ 号列表（可能为空）。

    对应 aiocqhttp 的 ``CQHttp._wsr_api_clients``：
    NapCat 以 universal / api 角色接入时才会登记到这里。
    """
    clients = getattr(client, "_wsr_api_clients", None)
    if isinstance(clients, dict):
        return [str(k) for k in clients.keys() if k is not None]
    return []


def _has_conn_info(client: Any) -> bool:
    """能否读到 aiocqhttp 的内部连接表（用于区分"确认未连接"与"未知"）。"""
    return isinstance(getattr(client, "_wsr_api_clients", None), dict)


def _has_event_client(client: Any) -> bool:
    """是否存在只收事件的角色连接（event）。"""
    clients = getattr(client, "_wsr_event_clients", None)
    return isinstance(clients, (set, frozenset)) and len(clients) > 0


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
