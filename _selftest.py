"""开发期自测脚本：用最小 astrbot 桩验证插件可加载、可实例化。

运行：python _selftest.py
"""

import importlib
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def mk(name, ispkg=False):
    m = types.ModuleType(name)
    if ispkg:
        m.__path__ = []
    sys.modules[name] = m
    return m


# ---- astrbot 桩 ----
astrbot = mk("astrbot", True)
api = mk("astrbot.api", True)


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


api.logger = _Logger()


class FunctionTool:
    pass


api.FunctionTool = FunctionTool


class AstrBotConfig(dict):
    pass


api.AstrBotConfig = AstrBotConfig

# ---- event ----
event_mod = mk("astrbot.api.event")


class AstrMessageEvent:
    pass


event_mod.AstrMessageEvent = AstrMessageEvent


class _Filter:
    class EventMessageType:
        ALL = "all"
        GROUP_MESSAGE = "group"
        PRIVATE_MESSAGE = "private"

    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"
        ALL = "all"

    def _d(self, *a, **k):
        def deco(f):
            return f

        return deco

    command = _d
    event_message_type = _d
    platform_adapter_type = _d
    llm_tool = _d
    on_astrbot_loaded = _d
    permission_type = _d


event_mod.filter = _Filter()
event_mod.EventMessageType = _Filter.EventMessageType

# ---- star ----
star_mod = mk("astrbot.api.star")


class Context:
    pass


class Star:
    def __init__(self, context=None):
        self.context = context


star_mod.Context = Context
star_mod.Star = Star
api.star = star_mod

# ---- message_components ----
mc = mk("astrbot.api.message_components")


class At:
    def __init__(self, qq=""):
        self.qq = qq


class Reply:
    def __init__(self, id="", sender_id=""):
        self.id = id
        self.sender_id = sender_id


class Image:
    def __init__(self, url="", file=""):
        self.url = url
        self.file = file


mc.At = At
mc.Reply = Reply
mc.Image = Image
api.message_components = mc

# ---- astrbot.api.web（供面板后端使用）----
web_mod = mk("astrbot.api.web")


class _Query:
    def get(self, key, default=None, type=None):  # noqa: A002
        return default

    def getlist(self, key):
        return []


class _Req:
    query = _Query()

    @staticmethod
    async def json(default=None):
        return default or {}

    @staticmethod
    async def body():
        return b""


web_mod.request = _Req()
web_mod.json_response = lambda payload: payload
web_mod.error_response = lambda msg, status_code=400: (
    {"status": "error", "message": msg},
    status_code,
)
api.web = web_mod


class _Ctx:
    """带 register_web_api 的 Context 桩，用于验证面板路由注册。"""

    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, desc):
        self.routes.append((route, handler, tuple(methods), desc))

    @property
    def platform_manager(self):
        class _PM:
            platform_insts = []

            @staticmethod
            def get_insts():
                # 对齐 AstrBot PlatformManager.get_insts()（同步，返回 list）
                return []

        return _PM()


star_mod.Context = _Ctx


def main():
    main_mod = importlib.import_module("astrbot_plugin_panshi.main")
    print("MAIN_IMPORTED_OK")
    ctx = _Ctx()
    inst = main_mod.PanshiPlugin(ctx, {})
    print("PLUGIN_INSTANTIATED_OK")

    cmds = sorted(n for n in dir(inst) if n.startswith("cmd_"))
    print(f"COMMANDS: {len(cmds)}")

    tools = sorted(
        getattr(getattr(inst, n), "__name__", n)
        for n in dir(type(inst))
        if n.startswith("llm_")
    )
    print(f"LLM_TOOLS: {len(tools)}")

    # 校验核心模块可用
    assert inst.normal and inst.guard and inst.intent and inst.executor
    print("HANDLES_OK")

    # 校验 WebUI 面板路由注册
    assert inst.web is not None, "面板控制器未创建"
    assert len(ctx.routes) == 9, f"路由数量不对: {len(ctx.routes)}"
    for route, _h, _m, _d in ctx.routes:
        assert route.startswith("/astrbot_plugin_panshi/"), route
    print(f"WEB_ROUTES_OK ({len(ctx.routes)})")
    for route, _h, methods, _d in ctx.routes:
        print("   ", ",".join(methods).ljust(4), route)

    # 校验时长解析
    from astrbot_plugin_panshi.utils import parse_duration, format_duration

    assert parse_duration("10m") == 600
    assert parse_duration("1h30m") == 5400
    assert format_duration(5400) == "1小时30分钟"
    print("UTILS_OK")

    # 校验意图 JSON 提取
    from astrbot_plugin_panshi.core.intent import IntentParser

    parsed = IntentParser._extract_json('```json\n{"action":"ban","target":"reply","duration":600}\n```')
    assert parsed and parsed["action"] == "ban", parsed
    bad = IntentParser._extract_json("not json")
    assert bad is None
    print("INTENT_PARSE_OK")

    test_config_layer()
    test_group_cache()
    test_page_service(inst)

    print("ALL_SELFTEST_PASS")


# ======================================================================
#  面板相关模块自测
# ======================================================================
def test_config_layer():
    """配置读写：schema 快照、类型校验、apply_payload。"""
    from astrbot_plugin_panshi.config import PluginConfig

    raw = {
        "basic": {"default_ban_time": 60, "super_admins": ["10001"]},
        "guard": {"forbidden_enable": True},
    }
    cfg = PluginConfig(raw)

    # schema 快照应读到真实的 _conf_schema.json
    groups = cfg.schema_snapshot()
    assert len(groups) == 7, [g["key"] for g in groups]
    keys = [g["key"] for g in groups]
    assert keys[:2] == ["basic", "guard"], keys
    total_fields = sum(len(g["fields"]) for g in groups)
    assert total_fields >= 40, total_fields
    print(f"SCHEMA_OK (7 组 / {total_fields} 项)")

    # 配置快照应包含全部字段
    snap = cfg.config_snapshot()
    assert set(snap.keys()) == set(keys), snap.keys()
    assert snap["basic"]["default_ban_time"] == 60
    print("CONFIG_SNAPSHOT_OK")

    # validate_payload 应拒绝非法类型
    try:
        cfg.validate_payload({"guard": {"forbidden_enable": "maybe"}})
        raise AssertionError("非法布尔值竟然通过了校验")
    except ValueError:
        pass

    # 非法整数
    try:
        cfg.validate_payload({"basic": {"default_ban_time": "abc"}})
        raise AssertionError("非法整数竟然通过了校验")
    except ValueError:
        pass

    # slider 裁剪
    cleaned = cfg.validate_payload({"guard": {"spam_count": 99999}})
    assert cleaned["guard"]["spam_count"] == 20, cleaned["guard"]["spam_count"]

    # 列表：字符串 -> 列表，去空去重
    cleaned = cfg.validate_payload({"guard": {"forbidden_words": "a, b\nc,,a"}})
    assert cleaned["guard"]["forbidden_words"] == ["a", "b", "c"], cleaned

    # 未知字段应被忽略
    cleaned = cfg.validate_payload({"guard": {"__nope__": 1}})
    assert "guard" not in cleaned or "__nope__" not in cleaned.get("guard", {})

    # apply_payload 写回
    cfg.apply_payload({"guard": {"spam_count": 8}})
    assert cfg.get("guard", "spam_count") == 8
    print("VALIDATE_OK")


def test_group_cache():
    """群缓存：归一化、排序、列表提取。"""
    import asyncio
    from astrbot_plugin_panshi.data import GroupInfoCache
    from astrbot_plugin_panshi.data.group_cache import _extract_list

    # 各种返回结构
    assert _extract_list([{"group_id": 1}]) == [{"group_id": 1}]
    assert _extract_list({"data": [{"group_id": 2}]}) == [{"group_id": 2}]
    assert _extract_list({"retcode": 100, "status": "failed"}) is None
    assert _extract_list("garbage") is None

    class _Client:
        async def call_action(self, action):
            assert action == "get_group_list"
            return [
                {"group_id": 1, "group_name": "小群", "member_count": 3},
                {"group_id": 2, "group_name": "大群", "member_count": 500},
            ]

    class _Platform:
        @staticmethod
        def get_client():
            return _Client()

    class _PM:
        def __init__(self, insts):
            self.platform_insts = insts

        def get_insts(self):
            return self.platform_insts

    class _C:
        def __init__(self, pm):
            self.platform_manager = pm

    # 正常路径：get_insts() 返回 list[Platform]
    cache = GroupInfoCache(_C(_PM([_Platform()])))
    groups = asyncio.run(cache.list_groups(force=True))
    assert len(groups) == 2
    # 应按人数降序
    assert groups[0]["group_id"] == "2", groups
    assert groups[0]["group_name"] == "大群"
    assert groups[1]["member_count"] == 3
    assert cache.last_error == "", cache.last_error
    print("GROUP_CACHE_OK")

    # 兜底路径：没有 get_insts()，只有 platform_insts 属性
    class _PM2:
        def __init__(self, insts):
            self.platform_insts = insts

    cache2 = GroupInfoCache(_C(_PM2([_Platform()])))
    groups = asyncio.run(cache2.list_groups(force=True))
    assert len(groups) == 2, groups
    print("GROUP_CACHE_FALLBACK_OK")

    # 适配器存在但未连接协议端 -> 应给出「未连接」而非「未找到适配器」
    class _Offline:
        @staticmethod
        def get_client():
            return None

    cache3 = GroupInfoCache(_C(_PM([_Offline()])))
    groups = asyncio.run(cache3.list_groups(force=True))
    assert groups == []
    assert "尚未与协议端建立连接" in cache3.last_error, cache3.last_error
    print("GROUP_CACHE_OFFLINE_MSG_OK")

    # 完全没有适配器 -> 应提示去启用 aiocqhttp
    cache4 = GroupInfoCache(_C(_PM([])))
    groups = asyncio.run(cache4.list_groups(force=True))
    assert groups == []
    assert "未找到平台适配器" in cache4.last_error, cache4.last_error
    print("GROUP_CACHE_EMPTY_MSG_OK")


def test_page_service(inst):
    """面板业务层：群列表、全局配置、按群覆盖。"""
    import asyncio
    from astrbot_plugin_panshi.pages_service import PageService

    svc = PageService(inst.cfg, inst.db, inst.group_cache)

    # 概览
    ov = svc.overview()
    assert "tracked_groups" in ov and "quick_status" in ov
    assert isinstance(ov["quick_status"], list) and len(ov["quick_status"]) == 9
    print(f"OVERVIEW_OK (开关 {len(ov['quick_status'])} 项)")

    # 全局配置
    g = svc.get_global_config()
    assert g["is_default"] is True
    assert "guard" in g["config"]
    print("GLOBAL_CONFIG_OK")

    # 保存全局
    g2 = svc.update_global_config({"guard": {"spam_count": 7}})
    assert g2["config"]["guard"]["spam_count"] == 7
    print("GLOBAL_SAVE_OK")

    # 群列表（无适配器时应优雅返回空 + 错误信息）
    groups = asyncio.run(svc.list_groups(force=True))
    assert isinstance(groups, list)
    print(f"GROUP_LIST_OK (返回 {len(groups)} 个群，缓存错误: {inst.group_cache.last_error!r})")

    # 按群配置：默认跟随
    gc = svc.get_group_config("123456")
    assert gc["follow_default"] is True, gc
    assert gc["effective"]["guard"]["spam_count"] == 7

    # 改为独立配置
    gc2 = svc.update_group_config("123456", {"follow_default": False, "guard": {"spam_count": 99}})
    assert gc2["follow_default"] is False
    assert gc2["effective"]["guard"]["spam_count"] == 20  # 被 slider 裁到上限
    assert gc2["effective"]["warning"]["warning_enable"] is True  # 未覆盖的仍取全局
    print("GROUP_OVERRIDE_OK")

    # 恢复默认
    gc3 = svc.reset_group_config("123456")
    assert gc3["follow_default"] is True
    assert gc3["effective"]["guard"]["spam_count"] == 7
    print("GROUP_RESET_OK")

    # 非法群号
    try:
        svc.get_group_config("abc")
        raise AssertionError("非法群号竟然通过了")
    except ValueError:
        pass
    print("GROUP_ID_VALIDATION_OK")


if __name__ == "__main__":
    main()
