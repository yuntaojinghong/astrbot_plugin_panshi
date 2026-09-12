"""面板业务层：把配置读写、群列表、运行状态组装成前端直接可用的数据结构。

这一层不关心 HTTP，只负责"取数据 / 存数据 / 校验"，
由 ``web.py`` 包装成 Web API。
"""

from __future__ import annotations

import os
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger("panshi")

DEFAULT_GROUP_ID = "__default__"
FOLLOW_DEFAULT_KEY = "follow_default"
PLUGIN_NAME = "astrbot_plugin_panshi"

# 与 metadata.yaml 保持一致的插件版本（读取失败时的兜底值）
FALLBACK_VERSION = "v1.3.0"

# 按群可覆盖的配置分组（与 _conf_schema.json 的分组保持一致）
OVERRIDABLE_GROUPS = [
    "guard",
    "welcome",
    "warning",
    "smart",
    "activity",
    "automate",
]


class PageService:
    """面板数据服务。

    Args:
        cfg: :class:`PluginConfig` 实例
        db: :class:`Storage` 实例
        group_cache: :class:`GroupInfoCache` 实例
    """

    def __init__(self, cfg: Any, db: Any, group_cache: Any):
        self.cfg = cfg
        self.db = db
        self.group_cache = group_cache

    # ==================================================================
    #  初始化负载
    # ==================================================================
    async def bootstrap(self) -> dict:
        """面板首次加载所需的全部数据。"""
        groups = await self.list_groups()
        return {
            "schema": self.cfg.schema_snapshot(),
            "groups": groups,
            "global": self.get_global_config(),
            "meta": {
                "plugin_name": PLUGIN_NAME,
                "version": self.plugin_version(),
                "follow_default_key": FOLLOW_DEFAULT_KEY,
                "default_group_id": DEFAULT_GROUP_ID,
                "overridable_groups": OVERRIDABLE_GROUPS,
                "group_cache_error": self.group_cache.last_error,
                "connection": self.connection(),
            },
        }

    # ==================================================================
    #  连接诊断
    # ==================================================================
    def connection(self) -> dict:
        """当前与协议端（NapCat / OneBot v11）的连接诊断信息。"""
        status = self.group_cache.connection_status()
        status["last_error"] = self.group_cache.last_error
        status["groups_cached"] = len(self.group_cache.snapshot())
        status["updated_at"] = self.group_cache.updated_at
        return status

    def plugin_version(self) -> str:
        """从 metadata.yaml 读取插件版本号，失败时用兜底值。"""
        version = getattr(self, "_version", None)
        if version:
            return version
        version = FALLBACK_VERSION
        try:
            base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            path = os.path.join(base, "metadata.yaml")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("version:"):
                            raw = line.split(":", 1)[1].strip().strip("'\"")
                            if raw:
                                version = raw
                            break
        except Exception:
            pass
        self._version = version
        return version

    # ==================================================================
    #  群列表
    # ==================================================================
    async def list_groups(self, force: bool = False) -> list[dict]:
        """返回群列表，并在每项上附加"是否启用/是否跟随默认"状态。"""
        raw = await self.group_cache.list_groups(force=force)
        enabled_list = self.cfg.enable_groups
        all_enabled = (not enabled_list) or ("all" in [g.lower() for g in enabled_list])

        out: list[dict] = []
        for g in raw:
            gid = str(g.get("group_id", ""))
            if not gid:
                continue
            override = self.db.get_group_override(gid)
            out.append(
                {
                    "group_id": gid,
                    "group_name": g.get("group_name", f"群 {gid}"),
                    "member_count": g.get("member_count", 0),
                    "max_member_count": g.get("max_member_count", 0),
                    "owner_id": g.get("owner_id", ""),
                    "bot_role": g.get("bot_role", "unknown"),
                    "enabled": all_enabled or (gid in enabled_list),
                    # 是否为该群保存了独立配置
                    "has_override": bool(override),
                    "override_keys": sorted(override.keys()),
                }
            )
        return out

    # ==================================================================
    #  全局默认配置
    # ==================================================================
    def get_global_config(self) -> dict:
        return {
            "group_id": DEFAULT_GROUP_ID,
            "group_name": "全局默认",
            "is_default": True,
            "config": self.cfg.config_snapshot(),
            "overridden_by": self._count_overridden_groups(),
        }

    def update_global_config(self, payload: dict) -> dict:
        """保存全局默认配置。"""
        self.cfg.apply_payload(payload)
        return self.get_global_config()

    # ==================================================================
    #  按群配置
    # ==================================================================
    def get_group_config(self, group_id: str) -> dict:
        gid = self._normalize_gid(group_id)
        override = self.db.get_group_override(gid)
        follow = not bool(override) or bool(override.get(FOLLOW_DEFAULT_KEY, True))

        # 该群实际生效的值 = 全局默认 叠加 群覆盖
        effective = self.cfg.config_snapshot()
        for gkey, gval in override.items():
            if gkey in (FOLLOW_DEFAULT_KEY,):
                continue
            if isinstance(gval, dict) and isinstance(effective.get(gkey), dict):
                effective[gkey].update(gval)
            else:
                effective[gkey] = gval

        # 群信息（从缓存里找，找不到就用最小结构）
        info = self._find_group_info(gid)
        return {
            "group_id": gid,
            "group_name": (info or {}).get("group_name", f"群 {gid}"),
            "member_count": (info or {}).get("member_count", 0),
            "is_default": False,
            "follow_default": follow,
            "override": override,
            "effective": effective,
            "schema": self.cfg.schema_snapshot(),
        }

    def update_group_config(self, group_id: str, payload: dict) -> dict:
        """保存某个群的独立配置。

        Args:
            payload: 两种形态
                - ``{"follow_default": true}`` —— 恢复继承全局默认（清空覆盖）
                - ``{"follow_default": false, "guard": {...}, ...}`` —— 保存独立配置

        Note:
            ``follow_default`` 缺省时**沿用该群当前状态**，而不是一律当作 true。
            否则前端漏传该字段会把用户刚存的独立配置清掉（历史 bug）。
        """
        gid = self._normalize_gid(group_id)
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")

        if FOLLOW_DEFAULT_KEY in payload:
            follow = _as_bool(payload.get(FOLLOW_DEFAULT_KEY))
        else:
            # 未显式指定：保持该群既有状态
            current = self.db.get_group_override(gid) or {}
            follow = _as_bool(current.get(FOLLOW_DEFAULT_KEY, True))

        if follow:
            # 跟随默认 = 清掉该群的所有覆盖
            self.db.reset_group(gid)
            return self.get_group_config(gid)

        # 校验通过后写入。注意：override 存的是「与全局默认的差异」，
        # 因此这里会剔除掉与全局相同的字段——即使前端把整组生效值都发了回来，
        # 也不会把当前全局值固化成该群专属值（表现为「配置被写死、改全局不生效」）。
        cleaned = self.cfg.validate_payload(payload)
        self.db.set_group_override(gid, FOLLOW_DEFAULT_KEY, False)
        for gkey, gval in cleaned.items():
            if gkey not in OVERRIDABLE_GROUPS:
                continue
            diff = self._diff_from_global(gkey, gval)
            if diff:
                self.db.set_group_override(gid, gkey, diff)
            else:
                # 该分组与全局完全一致 → 没必要保留覆盖
                self.db.clear_group_override(gid, gkey)
        return self.get_group_config(gid)

    def _diff_from_global(self, gkey: str, gval: dict) -> dict:
        """挑出与全局默认不同的字段。"""
        if not isinstance(gval, dict):
            return {}
        glob = (self.cfg.config_snapshot() or {}).get(gkey)
        if not isinstance(glob, dict):
            return dict(gval)
        return {k: v for k, v in gval.items() if not _same_value(v, glob.get(k))}

    def reset_group_config(self, group_id: str) -> dict:
        """清除某群的独立配置，恢复跟随全局默认。"""
        gid = self._normalize_gid(group_id)
        self.db.reset_group(gid)
        return self.get_group_config(gid)

    # ==================================================================
    #  运行状态概览（面板顶部卡片）
    # ==================================================================
    def overview(self) -> dict:
        """汇总运行状态，供面板展示。"""
        data = getattr(self.db, "_data", {}) or {}
        users = data.get("users", {}) if isinstance(data, dict) else {}
        blacklist = data.get("blacklist", {}) if isinstance(data, dict) else {}

        group_ids = set()
        warn_total = 0
        active_users = 0
        for key in users:
            if "_" not in str(key):
                continue
            gid = str(key).split("_", 1)[0]
            group_ids.add(gid)
        for value in users.values():
            if not isinstance(value, dict):
                continue
            warns = value.get("warnings") or []
            if isinstance(warns, list):
                warn_total += len(warns)
            if _to_int(value.get("points")) > 0 or _to_int(value.get("messages")) > 0:
                active_users += 1

        blocked = 0
        for lst in blacklist.values():
            if isinstance(lst, list):
                blocked += len(lst)

        return {
            "tracked_groups": len(group_ids),
            "tracked_users": len(users),
            "active_users": active_users,
            "total_warnings": warn_total,
            "blocked_users": blocked,
            "quick_status": self._quick_status(),
        }

    # ==================================================================
    #  内部工具
    # ==================================================================
    def _quick_status(self) -> list[dict]:
        """把关键开关汇总成一排状态点，方便一眼看清。"""
        cfg = self.cfg
        flips = [
            ("违禁词检测", cfg.get("guard", "forbidden_enable", True)),
            ("刷屏检测", cfg.get("guard", "spam_enable", True)),
            ("广告拦截", cfg.get("guard", "ad_enable", True)),
            ("入群欢迎", cfg.get("welcome", "welcome_enable", True)),
            ("算术验证", cfg.get("welcome", "verify_enable", False)),
            ("警告系统", cfg.get("warning", "warning_enable", True)),
            ("智能识别", cfg.get("smart", "smart_enable", True)),
            ("签到", cfg.get("activity", "checkin_enable", True)),
            ("宵禁", cfg.get("automate", "curfew_enable", False)),
        ]
        return [{"label": label, "on": _as_bool(val)} for label, val in flips]

    def _count_overridden_groups(self) -> int:
        data = getattr(self.db, "_data", {}) or {}
        groups = data.get("groups", {}) if isinstance(data, dict) else {}
        count = 0
        for gid, override in groups.items():
            if str(gid) == DEFAULT_GROUP_ID:
                continue
            if isinstance(override, dict) and not _as_bool(
                override.get(FOLLOW_DEFAULT_KEY, True)
            ):
                count += 1
        return count

    def _find_group_info(self, group_id: str) -> dict | None:
        for g in self.group_cache.snapshot():
            if str(g.get("group_id")) == str(group_id):
                return g
        return None

    @staticmethod
    def _normalize_gid(group_id: Any) -> str:
        gid = str(group_id or "").strip()
        if not gid:
            raise ValueError("缺少群号 group_id")
        if not gid.isdigit():
            raise ValueError(f"群号必须是纯数字，收到 {gid!r}")
        return gid


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "on", "yes", "开", "是")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _same_value(a: Any, b: Any) -> bool:
    """判断两个配置值是否等价（用于剔除与全局相同的字段）。

    列表按「元素逐个字符串比较」处理，避免 [1,2] 与 ["1","2"] 被误判为不同。
    """
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            str(x) == str(y) for x, y in zip(a, b)
        )
    if isinstance(a, bool) or isinstance(b, bool):
        return _as_bool(a) is _as_bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b
