"""面板业务层：把配置读写、群列表、运行状态组装成前端直接可用的数据结构。

这一层不关心 HTTP，只负责"取数据 / 存数据 / 校验"，
由 ``web.py`` 包装成 Web API。
"""

from __future__ import annotations

import copy
import os
from typing import Any

from astrbot.api import logger

DEFAULT_GROUP_ID = "__default__"
FOLLOW_DEFAULT_KEY = "follow_default"
PLUGIN_NAME = "astrbot_plugin_panshi"

# 与 metadata.yaml 保持一致的插件版本（读取失败时的兜底值）
FALLBACK_VERSION = "v1.15.0"

# 按群可覆盖的配置分组（与 _conf_schema.json 的分组保持一致）
OVERRIDABLE_GROUPS = [
    "guard",
    "welcome",
    "warning",
    "smart",
    "activity",
    "automate",
    "interact",
    "basic",
]

# 分组内**允许**按群覆盖的字段白名单。
#
# ``basic`` 是混合分组：其中 anon_protect / anon_nicknames 这类豁免规则天生
# 与群相关（运营号只在某个群需要豁免），但 super_admins / enable_groups /
# max_ban_time 属于插件级全局开关，被按群覆盖会造成「权限与生效范围两套真相」，
# 因此这里逐字段放行，而不是整组放行。
OVERRIDABLE_FIELDS = {
    "basic": {
        "anon_protect",
        "anon_nicknames",
        "default_ban_time",
        "operation_notice",
    },
    # activity 里也有一个插件级全局开关：`points_shared`（积分跨群共用）。
    # 允许按群覆盖会变成「这个群共用、那个群不共用」——同一份积分两种归属，
    # 没法解释也没法排查，所以逐字段放行：**除了 points_shared，其余都能按群覆盖**。
    "activity": {
        "checkin_enable",
        "checkin_points",
        "checkin_random_bonus",
        "checkin_streak_bonus",
        "checkin_streak_bonus_cap",
        "checkin_first_bonus",
        "chat_points_enable",
        "chat_points_value",
        "chat_points_cooldown",
        "chat_points_daily_cap",
        "chat_points_min_len",
        "chat_first_bonus",
        "newbie_bonus",
    },
}


def field_overridable(group: str, key: str) -> bool:
    """判断某个分组的某个字段是否允许按群覆盖。"""
    if group not in OVERRIDABLE_GROUPS:
        return False
    allowed = OVERRIDABLE_FIELDS.get(group)
    return allowed is None or key in allowed


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
                # 字段级白名单：前端据此把不能按群覆盖的字段显示为只读，
                # 避免「面板能改、后端静默丢弃」的假开关。
                "overridable_fields": {
                    g: sorted(OVERRIDABLE_FIELDS[g]) for g in OVERRIDABLE_FIELDS
                },
                "group_cache_error": self.group_cache.last_error,
                "connection": self.connection(),
            },
        }

    # ==================================================================
    #  配置备份：导出 / 导入
    # ==================================================================
    def export_all(self) -> dict:
        """导出全部配置（全局默认 + 各群独立配置）为可备份的 JSON 结构。"""
        import time as _time

        data = getattr(self.db, "_data", {}) or {}
        groups = data.get("groups", {}) if isinstance(data, dict) else {}
        return {
            "plugin": PLUGIN_NAME,
            "version": self.plugin_version(),
            "exported_at": int(_time.time()),
            "global_config": self.cfg.config_snapshot(),
            "groups": copy.deepcopy(groups) if isinstance(groups, dict) else {},
        }

    def import_all(self, payload: dict) -> dict:
        """导入备份：覆盖全局配置与各群独立配置。

        Args:
            payload: export_all() 产出的结构

        Returns:
            {"ok": True, "groups": 恢复的群覆盖数}
        """
        import time as _time

        if not isinstance(payload, dict):
            raise ValueError("导入内容必须是 JSON 对象")
        if payload.get("plugin") not in (None, PLUGIN_NAME):
            raise ValueError("这不是磐石的配置备份文件")

        global_cfg = payload.get("global_config")
        if not isinstance(global_cfg, dict):
            raise ValueError("备份缺少 global_config 字段")
        groups = payload.get("groups", {})
        if not isinstance(groups, dict):
            raise ValueError("备份的 groups 字段必须是对象")

        # 1) 全局配置：走类型校验后写回并持久化
        self.cfg.apply_payload(global_cfg)

        # 2) 各群独立配置：整表替换（公开 API 逐项写入）
        restored = 0
        for gid, override in groups.items():
            if not isinstance(override, dict):
                continue
            try:
                self.db.reset_group(str(gid))
            except Exception:
                pass
            # 关键：必须显式补上 follow_default=False。
            # 面板把「缺 follow_default」读作「跟随全局」，而运行时的 for_group()
            # 把「缺该字段」读作「应用覆盖」——两边默认值相反，一个老备份导入后
            # 会呈现「面板说跟随全局、实际按独立配置执行」的分裂状态。
            override = dict(override)
            override.setdefault(FOLLOW_DEFAULT_KEY, False)
            for key, val in override.items():
                if key != FOLLOW_DEFAULT_KEY and key not in OVERRIDABLE_GROUPS:
                    continue
                try:
                    self.db.set_group_override(str(gid), key, val)
                except Exception as e:
                    logger.warning(f"[磐石] 导入群 {gid} 覆盖项 {key} 失败: {e}")
            restored += 1
        try:
            self.db.save()
        except Exception as e:
            logger.warning(f"[磐石] 导入后保存存储失败: {e}")

        return {
            "ok": True,
            "restored_groups": restored,
            "imported_at": int(_time.time()),
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
        """面板上显示的版本号。

        **来源是正在运行的代码，不是磁盘上的文件。**

        之前这里读 `metadata.yaml`，线上出现过「装的 1.9.9、面板显示 1.9.5」。
        读文件的问题是：文件可能是旧的、可能不在预期位置、也可能被别的东西
        覆盖——显示出来的数字和你实际跑的那份代码没有任何必然关系，
        排查问题时先被它带偏。

        现在改成从包里 `__init__.py` 的 `__version__` 取——那是随模块一起
        被导入的代码常量，跑的是哪份代码就显示哪个版本，改不了假。

        仍然保留读 metadata.yaml 的能力，但只在「代码里的版本号读不到」时
        才用它兜底；两个值不一致时在日志里说明，便于发现打包问题。
        """
        cached = getattr(self, "_version", None)
        if cached:
            return cached

        from . import __version__ as code_version  # 正在运行的那份代码

        meta_version = self._version_from_metadata()
        version = code_version or FALLBACK_VERSION

        if meta_version and meta_version.lstrip("v") != str(version).lstrip("v"):
            logger.warning(
                "[磐石] 版本号不一致：代码里是 %s，metadata.yaml 是 %s。"
                "面板显示的是代码版本（%s）——说明装的包有问题，"
                "可能解压时只覆盖了部分文件。",
                version, meta_version, version,
            )
        self._version = str(version)
        return self._version

    def _version_from_metadata(self) -> str:
        """从 metadata.yaml 读版本号；读不到返回空串。

        只用它做交叉核对——面板显示的版本号来自代码（见
        :meth:`plugin_version`）。这里读不到不算错误，返回空串即可。
        """
        try:
            # metadata.yaml 与本文件同级（都在插件根目录）
            base = os.path.dirname(os.path.abspath(__file__))
            path = os.path.join(base, "metadata.yaml")
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip().startswith("version:"):
                            return line.split(":", 1)[1].strip().strip("'\"")
        except Exception:
            pass
        return ""

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
        # 与 PluginConfig.for_group() 保持**同一个默认值**（缺字段 = 跟随全局）。
        # 两边默认值相反时，面板会显示「跟随全局」而运行时却按覆盖执行。
        follow = (not bool(override)) or _follow_default(override)

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
            # 机器人在本群的身份（群主/管理员/普通成员/未知）。
            # 面板要把它显示出来——不然用户只能靠"点了没反应"才发现机器人不是管理员。
            "bot_role": (info or {}).get("bot_role", "unknown"),
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

        Note:
            保存采用**整组替换**语义：payload 里没出现的可覆盖分组会被清空。
            只按 payload 里出现的分组写入是不够的——前端不会发「与全局相同」
            的字段，于是把某个值改回全局值时 diff 为空、分组整体缺失，旧覆盖
            永远留不下来也清不掉，用户以为改回去了而运行时仍用旧值。
        """
        gid = self._normalize_gid(group_id)
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")

        if FOLLOW_DEFAULT_KEY in payload:
            follow = _as_bool(payload.get(FOLLOW_DEFAULT_KEY))
        else:
            # 未显式指定：保持该群既有状态（缺字段/无覆盖都算「跟随全局」）
            current = self.db.get_group_override(gid) or {}
            follow = _follow_default(current)

        if follow:
            # 跟随默认 = 清掉该群的所有覆盖
            self.db.reset_group(gid)
            return self.get_group_config(gid)

        # 校验通过后写入。注意：override 存的是「与全局默认的差异」，
        # 因此这里会剔除掉与全局相同的字段——即使前端把整组生效值都发了回来，
        # 也不会把当前全局值固化成该群专属值（表现为「配置被写死、改全局不生效」）。
        cleaned = self.cfg.validate_payload(payload)
        self.db.set_group_override(gid, FOLLOW_DEFAULT_KEY, False)
        for gkey in OVERRIDABLE_GROUPS:
            gval = cleaned.get(gkey)
            if not isinstance(gval, dict):
                # payload 里没带这一组 → 该群不再覆盖这一组（整组替换语义）
                self.db.clear_group_override(gid, gkey)
                continue
            allowed = {k: v for k, v in gval.items() if field_overridable(gkey, k)}
            if allowed != gval:
                skipped = sorted(set(gval) - set(allowed))
                logger.info(
                    f"[磐石] 群 {gid} 的 {gkey}.{'/'.join(skipped)} 属于插件级全局配置，"
                    "已忽略按群覆盖"
                )
            diff = self._diff_from_global(gkey, allowed)
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
            ks = str(key)
            if "_" not in ks:
                continue
            gid = ks.split("_", 1)[0]
            # 共用积分模式用的是保留键（``__shared___QQ``），它不是真实群号，
            # 不能算进「纳管群聊」——否则这个数字会平白多 1。
            if not gid or gid.startswith("_"):
                continue
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

        # 待人工发放的订单：面板顶栏据此提示管理员有货要发
        orders = data.get("orders") or [] if isinstance(data, dict) else []
        pending_orders = sum(
            1 for o in orders
            if isinstance(o, dict) and str(o.get("status")) == "pending"
        )

        return {
            "tracked_groups": len(group_ids),
            "tracked_users": len(users),
            "active_users": active_users,
            "total_warnings": warn_total,
            "blocked_users": blocked,
            "pending_orders": pending_orders,
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


def _follow_default(override: dict) -> bool:
    """该群是否「跟随全局」。

    **单一事实来源**：缺 ``follow_default`` 字段一律视为「跟随全局」。
    面板与 ``PluginConfig.for_group()`` 必须用同一个默认值，否则会出现
    「面板显示跟随全局、运行时按独立配置执行」的分裂状态。
    """
    if not isinstance(override, dict):
        return True
    if FOLLOW_DEFAULT_KEY not in override:
        return True
    return _as_bool(override.get(FOLLOW_DEFAULT_KEY))


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
