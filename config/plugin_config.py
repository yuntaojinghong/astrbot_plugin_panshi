"""配置封装：把 AstrBotConfig 包装成好用的属性对象。"""

from __future__ import annotations


class PluginConfig:
    """插件配置访问器。

    通过分组属性（basic / guard / welcome / warning / smart / activity / automate）
    访问 _conf_schema.json 中定义的配置项。
    """

    def __init__(self, raw: dict):
        self.raw = raw or {}

    # ---------- 通用取值 ----------
    def _group(self, name: str) -> dict:
        grp = self.raw.get(name, {})
        return grp if isinstance(grp, dict) else {}

    def get(self, group: str, key: str, default=None):
        return self._group(group).get(key, default)

    # ---------- basic ----------
    @property
    def super_admins(self) -> list[str]:
        return [str(x) for x in self.get("basic", "super_admins", []) or []]

    @property
    def default_ban_time(self) -> int:
        return int(self.get("basic", "default_ban_time", 60) or 60)

    @property
    def max_ban_time(self) -> int:
        return int(self.get("basic", "max_ban_time", 2592000) or 2592000)

    @property
    def enable_groups(self) -> list[str]:
        return [str(x) for x in self.get("basic", "enable_groups", []) or []]

    @property
    def operation_notice(self) -> bool:
        return bool(self.get("basic", "operation_notice", True))

    def group_enabled(self, group_id: str) -> bool:
        """判断某群是否启用插件。"""
        groups = self.enable_groups
        if not groups or "all" in [g.lower() for g in groups]:
            return True
        return str(group_id) in groups

    def clamp_ban_time(self, seconds: int) -> int:
        """把禁言时长限制在合理范围内。"""
        if seconds <= 0:
            return 0
        return min(seconds, self.max_ban_time)

    # ---------- guard ----------
    @property
    def guard(self) -> dict:
        return self._group("guard")

    # ---------- welcome ----------
    @property
    def welcome(self) -> dict:
        return self._group("welcome")

    # ---------- warning ----------
    @property
    def warning(self) -> dict:
        return self._group("warning")

    # ---------- smart ----------
    @property
    def smart(self) -> dict:
        return self._group("smart")

    # ---------- activity ----------
    @property
    def activity(self) -> dict:
        return self._group("activity")

    # ---------- automate ----------
    @property
    def automate(self) -> dict:
        return self._group("automate")

    def dump(self) -> dict:
        """返回原始配置字典。"""
        return dict(self.raw)
