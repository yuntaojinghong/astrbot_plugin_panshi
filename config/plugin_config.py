"""配置封装：把 AstrBotConfig 包装成好用的属性对象。

除了原有只读访问器，本模块还提供供 WebUI 面板使用的三组能力：
1. ``schema_snapshot()``  —— 从 _conf_schema.json 生成可供前端渲染的字段描述
2. ``config_snapshot()``  —— 导出当前所有配置值的结构化快照
3. ``apply_payload()``    —— 把前端提交的数据做类型安全校验后写回配置并持久化
"""

from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

try:
    from astrbot.api import logger
except Exception:  # 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger("panshi")

# 配置分组的展示顺序（与 _conf_schema.json 的 key 一致）
GROUP_ORDER = ["basic", "guard", "welcome", "warning", "smart", "activity", "automate", "interact"]

GROUP_ICONS = {
    "basic": "⚙️",
    "guard": "🛡️",
    "welcome": "👋",
    "warning": "⚠️",
    "smart": "🧠",
    "activity": "📊",
    "automate": "🌙",
    "interact": "🎮",
}


class PluginConfig:
    """插件配置访问器。

    通过分组属性（basic / guard / welcome / warning / smart / activity / automate）
    访问 _conf_schema.json 中定义的配置项。
    """

    def __init__(self, raw: dict | None = None, context: Any = None):
        self.raw = raw if isinstance(raw, dict) else {}
        # AstrBot 的 Context，用于定位插件目录、调用保存
        self.context = context
        self._schema: dict | None = None
        self._plugin_dir: str | None = None
        # 存储层引用（由 main.py 注入），用于按群配置视图
        self._storage = None

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
        return _to_int(self.get("basic", "default_ban_time", 60), 60)

    @property
    def max_ban_time(self) -> int:
        return _to_int(self.get("basic", "max_ban_time", 2592000), 2592000)

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

    # ---------- 匿名保护 ----------
    @property
    def anon_protect(self) -> bool:
        """是否豁免匿名转述内容（避免误禁匿名身份）。默认开启。"""
        return bool(self.get("basic", "anon_protect", True))

    @property
    def anon_nicknames(self) -> list[str]:
        """匿名昵称池：命中这些昵称的消息视为匿名转述，予以豁免。"""
        return [str(x) for x in self.get("basic", "anon_nicknames", []) or []]

    # ---------- 功能分组 ----------
    @property
    def basic(self) -> dict:
        """基础配置分组（供面板/自检做整体展示）。"""
        return self._group("basic")

    @property
    def version(self) -> str:
        """从 metadata.yaml 读取插件版本，失败时回退为 unknown。"""
        try:
            path = os.path.join(self.resolve_plugin_dir(), "metadata.yaml")
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"\s*version\s*:\s*(.+?)\s*$", line)
                    if m:
                        return m.group(1).strip().strip("\"'")
        except Exception:
            pass
        return "unknown"

    @property
    def guard(self) -> dict:
        return self._group("guard")

    @property
    def welcome(self) -> dict:
        return self._group("welcome")

    @property
    def warning(self) -> dict:
        return self._group("warning")

    @property
    def smart(self) -> dict:
        return self._group("smart")

    @property
    def activity(self) -> dict:
        return self._group("activity")

    @property
    def automate(self) -> dict:
        return self._group("automate")

    @property
    def interact(self) -> dict:
        """互动工具配置（投票 / 接龙 / 自动回复）。"""
        return self._group("interact")

    @property
    def auto_replies(self) -> list[dict]:
        """关键词自动回复规则列表。"""
        raw = self.get("interact", "auto_replies", []) or []
        return [r for r in raw if isinstance(r, dict)]

    @property
    def auto_replies_text(self) -> str:
        """关键词自动回复的文本配置（每行 k1,k2 => reply）。"""
        return str(self.get("interact", "auto_replies_text", "") or "")

    def parsed_auto_replies(self) -> list[dict]:
        """把文本形式的自动回复规则解析成 [{keywords, reply}]。"""
        rules = []
        for line in self.auto_replies_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" in line:
                left, _, right = line.partition("=>")
            elif "＝>" in line:
                left, _, right = line.partition("＝>")
            else:
                continue
            reply = right.strip()
            if not reply:
                continue
            rules.append({"keywords": left.strip(), "reply": reply})
        return rules

    # ---------- 风控增强 ----------
    @property
    def whitelist(self) -> list[str]:
        """风控白名单：这些用户豁免防护处罚（如官方客服号）。"""
        return [str(x) for x in self.get("guard", "whitelist", []) or []]

    @property
    def escalation_ladder(self) -> list[dict]:
        """渐进式处罚阶梯。解析 warning.escalation_ladder 文本。

        每行格式：``次数|动作|时长秒``，例如 ``3|ban|600``。
        动作可选 warn / ban / kick。
        """
        raw = self.get("warning", "escalation_ladder", "")
        ladder = []
        if isinstance(raw, str):
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [x.strip() for x in line.split("|")]
                if len(parts) < 2:
                    continue
                try:
                    cnt = int(parts[0])
                except ValueError:
                    continue
                action = parts[1].lower()
                if action not in ("warn", "ban", "kick"):
                    continue
                dur = 0
                if len(parts) >= 3:
                    try:
                        dur = int(parts[2])
                    except ValueError:
                        dur = 0
                ladder.append({"count": cnt, "action": action, "duration": dur})
        elif isinstance(raw, list):
            ladder = [r for r in raw if isinstance(r, dict)]
        ladder.sort(key=lambda r: r.get("count", 0))
        return ladder

    def dump(self) -> dict:
        """返回原始配置字典。"""
        return dict(self.raw)

    # ---------- 按群配置视图 ----------
    def for_group(self, group_id):
        """返回某群视角的配置对象（全局 + 该群覆盖深合并）。

        面板里的「按群独立配置」此前只写存储、运行时不读取，导致静默失效。
        各 handle 应通过本方法取配置，例如::

            cfg = self.cfg.for_group(group_id)
            guard = cfg.guard           # 该群视角的 guard 配置

        Args:
            group_id: 群号；为空或该群跟随全局时返回自身。

        Returns:
            一个 :class:`PluginConfig` 视图（加载该群的 override 覆盖全局值）。
        """
        gid = str(group_id).strip() if group_id not in (None, "") else ""
        if not gid:
            return self

        # 通过 storage 读取该群的覆盖；无 storage 引用时退化为全局。
        storage = getattr(self, "_storage", None)
        if storage is None:
            return self
        try:
            override = storage.get_group_override(gid)
        except Exception:
            return self
        if not override:
            return self

        # 该群标记为「跟随全局」时，忽略一切覆盖。
        if override.get("follow_default", False):
            return self

        # override 结构为「与全局的差异」的扁平字典：
        # {"follow_default": False, "guard": {...}, "automate": {...}}
        merged_raw = _deep_merge(copy.deepcopy(self.raw), override)
        view = PluginConfig(merged_raw, self.context)
        view._schema = self._schema
        view._plugin_dir = self._plugin_dir
        return view

    def bind_storage(self, storage) -> None:
        """注入存储层，使 ``for_group`` 能读取按群覆盖。"""
        self._storage = storage

    # ==================================================================
    #  WebUI 面板支持
    # ==================================================================
    def resolve_plugin_dir(self) -> str:
        """定位插件自身目录（用于读取 _conf_schema.json）。"""
        if self._plugin_dir:
            return self._plugin_dir
        candidates = []

        # 1) 从 AstrBot 插件管理器查询
        try:
            mgr = getattr(self.context, "plugin_manager", None)
            for meta in getattr(mgr, "context", None).get_all_stars() if mgr else []:
                name = getattr(meta, "name", "")
                if name == "astrbot_plugin_panshi":
                    mod = getattr(meta, "module", None)
                    path = getattr(mod, "__file__", None)
                    if path:
                        candidates.append(os.path.dirname(os.path.abspath(path)))
        except Exception:
            pass

        # 2) 回退：本文件位于 <plugin>/config/plugin_config.py
        candidates.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # 3) 回退：AstrBot 默认插件目录
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_path

            candidates.append(
                os.path.join(get_astrbot_plugin_path(), "astrbot_plugin_panshi")
            )
        except Exception:
            pass

        for c in candidates:
            if c and os.path.exists(os.path.join(c, "_conf_schema.json")):
                self._plugin_dir = c
                return c
        self._plugin_dir = candidates[0] if candidates else os.getcwd()
        return self._plugin_dir

    def load_schema(self) -> dict:
        """读取并缓存 _conf_schema.json。"""
        if self._schema is not None:
            return self._schema
        path = os.path.join(self.resolve_plugin_dir(), "_conf_schema.json")
        try:
            with open(path, encoding="utf-8") as f:
                self._schema = json.load(f)
        except Exception as e:
            logger.warning(f"[磐石] 读取 _conf_schema.json 失败: {e}")
            self._schema = {}
        return self._schema

    def schema_snapshot(self) -> list[dict]:
        """把 schema 转成前端好渲染的分组列表。

        返回::

            [{"key": "basic", "title": "基础设置", "icon": "⚙️",
              "fields": [{"key": "super_admins", "type": "list",
                          "label": "…", "hint": "…", "default": [] , ...}]}, ...]
        """
        schema = self.load_schema()
        groups: list[dict] = []
        ordered = GROUP_ORDER + [k for k in schema if k not in GROUP_ORDER]
        for gkey in ordered:
            node = schema.get(gkey)
            if not isinstance(node, dict) or node.get("type") != "object":
                continue
            items = node.get("items", {})
            if not isinstance(items, dict):
                continue
            groups.append(
                {
                    "key": gkey,
                    "title": node.get("description", gkey),
                    "hint": node.get("hint", ""),
                    "icon": GROUP_ICONS.get(gkey, "🔧"),
                    "fields": [
                        _field_snapshot(fkey, fnode)
                        for fkey, fnode in items.items()
                        if isinstance(fnode, dict)
                    ],
                }
            )
        return groups

    def config_snapshot(self) -> dict:
        """导出当前所有配置值（按分组），缺失项用 schema 默认值补齐。"""
        schema = self.load_schema()
        out: dict[str, Any] = {}
        for gkey, gnode in schema.items():
            if not isinstance(gnode, dict) or gnode.get("type") != "object":
                # 展开型字段（非 object 的顶层项）暂不单独处理
                continue
            items = gnode.get("items", {})
            grp = self._group(gkey)
            bucket: dict[str, Any] = {}
            for fkey, fnode in items.items():
                if not isinstance(fnode, dict):
                    continue
                default = fnode.get("default")
                bucket[fkey] = copy.deepcopy(grp.get(fkey, default))
            out[gkey] = bucket

        # 兼容：如果 schema 读取失败，直接按常见分组回退
        if not out:
            for gkey in GROUP_ORDER:
                out[gkey] = copy.deepcopy(self._group(gkey))
        return out

    def _field_index(self) -> dict[tuple[str, str], dict]:
        """建立 (分组, 字段) -> schema 节点 的索引。"""
        index: dict[tuple[str, str], dict] = {}
        for gkey, gnode in self.load_schema().items():
            if isinstance(gnode, dict) and gnode.get("type") == "object":
                for fkey, fnode in (gnode.get("items") or {}).items():
                    if isinstance(fnode, dict):
                        index[(gkey, fkey)] = fnode
        return index

    def validate_payload(self, payload: dict) -> dict:
        """只做校验与归一化，不写入配置。

        Returns:
            清洗后的 ``{分组: {字段: 值}}``，只包含 schema 中存在的项。

        Raises:
            ValueError: 校验失败（附带具体字段名）
        """
        if not isinstance(payload, dict):
            raise ValueError("payload 必须是对象")

        schema = self.load_schema()
        index = self._field_index()
        cleaned: dict[str, dict] = {}

        for gkey, gval in payload.items():
            if gkey not in schema:
                continue  # 忽略未知分组
            if not isinstance(gval, dict):
                raise ValueError(f"配置分组「{gkey}」必须是对象")
            bucket: dict[str, Any] = {}
            for fkey, fval in gval.items():
                fnode = index.get((gkey, fkey))
                if fnode is None:
                    continue  # 忽略未知字段
                bucket[fkey] = _coerce(fval, fnode, gkey, fkey)
            if bucket:
                cleaned[gkey] = bucket
        return cleaned

    def apply_payload(self, payload: dict) -> dict:
        """把前端提交的配置做类型校验后写回并持久化。

        Args:
            payload: ``{"basic": {...}, "guard": {...}}`` 形式的字典

        Returns:
            归一化后的完整配置快照

        Raises:
            ValueError: 校验失败（附带具体字段名）
        """
        cleaned = self.validate_payload(payload)

        for gkey, gval in cleaned.items():
            target = self.raw.get(gkey)
            if not isinstance(target, dict):
                target = {}
                self.raw[gkey] = target
            target.update(gval)

        self.save_config()
        return self.config_snapshot()

    def save_config(self) -> bool:
        """把配置持久化到 AstrBot。

        AstrBot 的 AstrBotConfig 本身是 dict 子类且带 ``save_config()``；
        直接改 self.raw 即可生效。此处优先调用其自带保存，
        若不可用则回退为写 JSON 文件。
        """
        # 路径 1：AstrBotConfig 自带保存
        save = getattr(self.raw, "save_config", None)
        if callable(save):
            try:
                save()
                return True
            except Exception as e:
                logger.warning(f"[磐石] AstrBotConfig.save_config 失败: {e}")

        # 路径 2：通过 context 上的配置管理器保存
        try:
            for attr in ("config", "_config"):
                cfgmgr = getattr(self.context, attr, None)
                save2 = getattr(cfgmgr, "save_config", None)
                if callable(save2):
                    save2()
                    return True
        except Exception as e:
            logger.warning(f"[磐石] context 配置保存失败: {e}")

        # 路径 3：回退为直接写文件（尽量不走到这里）
        try:
            path = os.path.join(
                self.resolve_plugin_dir(), "..", "..", "config",
                "astrbot_plugin_panshi_config.json",
            )
            path = os.path.abspath(path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(dict(self.raw), f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"[磐石] 配置保存失败: {e}")
            return False

    def reload_from(self, raw: dict) -> None:
        """用新字典替换内部配置（供外部刷新使用）。"""
        if isinstance(raw, dict):
            self.raw = raw


# ======================================================================
#  校验与归一化工具
# ======================================================================
def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _deep_merge(base: dict, override: dict) -> dict:
    """把 override 深合并进 base（override 优先），返回 base。

    只对 dict 做递归合并；list / 标量直接覆盖。
    """
    if not isinstance(override, dict):
        return base
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(base.get(key), dict):
            base[key] = _deep_merge(base[key], val)
        else:
            base[key] = val
    return base


def _field_snapshot(key: str, node: dict) -> dict:
    """把单个 schema 字段转成前端描述对象。"""
    out = {
        "key": key,
        "type": node.get("type", "string"),
        "label": node.get("description", key),
        "hint": node.get("hint", "") or "",
        "default": node.get("default"),
    }
    if node.get("options"):
        out["options"] = list(node["options"])
    if node.get("slider"):
        out["slider"] = dict(node["slider"])
    return out


def _coerce(value: Any, node: dict, group: str, key: str) -> Any:
    """按 schema 类型校验并归一化一个值，失败抛 ValueError。"""
    ftype = node.get("type", "string")
    label = node.get("description", key)

    if ftype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "on", "yes", "开", "是"):
                return True
            if low in ("false", "0", "off", "no", "关", "否"):
                return False
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError(f"「{label}」需要是布尔值，收到 {value!r}")

    if ftype == "int":
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"「{label}」需要是整数，收到 {value!r}") from None
        slider = node.get("slider") or {}
        lo, hi = slider.get("min"), slider.get("max")
        if lo is not None:
            num = max(int(lo), num)
        if hi is not None:
            num = min(int(hi), num)
        return num

    if ftype == "list":
        if value is None:
            return []
        if isinstance(value, str):
            parts = re.split(r"[\n,，;；]+", value)
        elif isinstance(value, (list, tuple)):
            parts = list(value)
        else:
            raise ValueError(f"「{label}」需要是列表，收到 {value!r}")
        result = []
        for item in parts:
            text = str(item).strip()
            if text and text not in result:
                result.append(text)
        return result

    if ftype == "float":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError(f"「{label}」需要是数字，收到 {value!r}") from None

    # string 及其他
    text = "" if value is None else str(value)
    options = node.get("options")
    if options and text not in options:
        # 非法选项回退到默认值，避免脏数据
        default = node.get("default", "")
        return str(default if default is not None else "")
    return text
