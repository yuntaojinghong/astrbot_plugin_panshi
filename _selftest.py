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
    assert len(ctx.routes) == 12, f"路由数量不对: {len(ctx.routes)}"
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
    test_errors()
    test_role_precheck()
    test_curfew_intent()
    test_banword_and_backup(inst)
    test_page_service(inst)
    test_local_intent()
    test_interact_layer()
    test_panel_layer()

    print("ALL_SELFTEST_PASS")


# ======================================================================
#  v1.5.0 新增能力自测
# ======================================================================
def test_local_intent():
    """本地规则意图解析：无 LLM 也能识别常用管理指令。"""
    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.local_intent import LocalIntentParser

    cfg = PluginConfig({"basic": {"default_ban_time": 60}})
    parser = LocalIntentParser(cfg)

    class _Ev:
        message_str = ""
        _msgs = []

        def get_sender_id(self):
            return "999"

        def get_group_id(self):
            return "1001"

        def get_self_id(self):
            return "777"

        def get_messages(self):
            return self._msgs

    # 显式 QQ 号
    r = parser.parse(_Ev(), "禁言 123456 10分钟")
    assert r and r["action"] == "ban" and r["target"] == "123456", r
    assert r["duration"] == 600, r

    # 小时换算
    r = parser.parse(_Ev(), "把123456禁言2小时")
    assert r and r["duration"] == 7200, r

    # 全体禁言 / 解禁（含插入字「都」）
    assert parser.parse(_Ev(), "全体禁言")["action"] == "whole_ban"
    assert parser.parse(_Ev(), "把全体都禁言了")["enable"] is True
    r = parser.parse(_Ev(), "解除全体禁言")
    assert r["action"] == "whole_ban" and r["enable"] is False, r

    # 模糊指代 -> recent_offender，而不是瞎猜
    r = parser.parse(_Ev(), "把刚才刷屏的禁言十分钟")
    assert r and r["target"] == "recent_offender", r

    # 无关闲聊不误判
    assert parser.parse(_Ev(), "今天天气不错") is None
    print("LOCAL_INTENT_OK")


def test_interact_layer():
    """互动工具：投票 / 接龙 / 自助查询 / 关键词自动回复。"""
    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.interact import InteractHandle

    class _DB:
        def get_points(self, g, u):
            return 42

        def get_message_count(self, g, u):
            return 7

        def get_warnings(self, g, u, e):
            return []

        def has_checked_in(self, g, u):
            return True

    class _Ev:
        def get_group_id(self):
            return "1001"

        def get_sender_id(self):
            return "999"

    cfg = PluginConfig(
        {"interact": {"auto_replies_text": "群规,规矩 => 群规见置顶\n签到 => 发 /签到"}}
    )
    ih = InteractHandle(cfg, _DB())

    # 自动回复
    assert ih.match_auto_reply("请问群规是啥") == "群规见置顶"
    assert ih.match_auto_reply("我要签到") == "发 /签到"
    assert ih.match_auto_reply("无关内容") is None
    print("AUTO_REPLY_OK")

    import asyncio

    loop = asyncio.new_event_loop()
    ev = _Ev()
    # 投票：发起 -> 投票 -> 结算
    text = loop.run_until_complete(ih.start_vote(ev, "周末去哪|爬山|桌游"))
    assert "1. 爬山" in text and "2. 桌游" in text, text
    loop.run_until_complete(ih.cast_vote(ev, "2"))
    res = loop.run_until_complete(ih.vote_result(ev))
    assert "桌游" in res, res
    # 接龙
    text = loop.run_until_complete(ih.start_chain(ev, "周末计划"))
    assert "周末计划" in text, text
    # 自助查询
    text = loop.run_until_complete(ih.self_query(ev, "积分"))
    assert "42" in text, text
    print("INTERACT_OK")


def test_panel_layer():
    """面板 / 自检 / 配置向导：聚合展示不抛异常。"""
    import asyncio
    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.panel import PanelHandle

    class _DB:
        def get_group_override(self, gid):
            return {}

    class _Bot:
        def set_group_ban(self, **k):
            pass

        def set_group_kick(self, **k):
            pass

        def delete_msg(self, **k):
            pass

        def set_group_whole_ban(self, **k):
            pass

    class _Ev:
        bot = _Bot()

        def get_group_id(self):
            return "1001"

        def get_sender_id(self):
            return "999"

        def get_messages(self):
            return []

        def get_self_id(self):
            return "777"

    ph = PanelHandle(PluginConfig({}), _DB())
    ev = _Ev()

    dash = ph.dashboard(ev)
    assert "磐石群管面板" in dash, dash
    assert "群号 1001" in dash, dash

    sc = asyncio.new_event_loop().run_until_complete(ph.self_check(ev))
    assert "磐石自检报告" in sc, sc
    assert "本地指令解析正常" in sc, sc

    guide = ph.config_guide("")
    for topic in ("风控", "活跃", "互动", "宵禁", "按群", "本地"):
        assert topic in guide, topic
    assert "escalation_ladder" in ph.config_guide("风控")
    print("PANEL_OK")


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
    # 分组：basic / guard / welcome / warning / smart / activity / automate / interact
    groups = cfg.schema_snapshot()
    assert len(groups) == 8, [g["key"] for g in groups]
    keys = [g["key"] for g in groups]
    assert keys[:2] == ["basic", "guard"], keys
    assert "interact" in keys, keys
    total_fields = sum(len(g["fields"]) for g in groups)
    assert total_fields >= 40, total_fields
    print(f"SCHEMA_OK (8 组 / {total_fields} 项)")

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

    # ------------------------------------------------------------------
    # 回归（用户实测：反向服务器开着，面板却一直报连接失败）：
    # 1) CQHttp 对象恒非 None，必须读 _wsr_api_clients 才能知道真实连接状态；
    # 2) 多账号同时连接时必须显式带 self_id 调用，否则 aiocqhttp 直接抛
    #    ApiNotAvailable —— 面板就会永远显示「连接失败」。
    # ------------------------------------------------------------------
    class _RealCQ:
        """模拟真实 aiocqhttp CQHttp：get_client() 恒返回自身（非 None）。"""

        def __init__(self, api_clients):
            self._wsr_api_clients = api_clients  # {self_id: ws}
            self._wsr_event_clients = set()

        async def call_action(self, action, **params):
            # 对齐 aiocqhttp 行为：不带 self_id 且在线账号 >1 时报 ApiNotAvailable
            sid = params.get("self_id")
            online = list(self._wsr_api_clients.keys())
            if not sid and len(online) != 1:
                raise RuntimeError("ApiNotAvailable")
            if sid and str(sid) not in online:
                raise RuntimeError("ApiNotAvailable")
            return [
                {"group_id": 10, "group_name": "甲群", "member_count": 30},
                {"group_id": 11, "group_name": "乙群", "member_count": 60},
            ]

    class _Adapter:
        def __init__(self, bot):
            self._bot = bot

        def get_client(self):
            return self._bot

    # 用例 1：适配器在、get_client() 非 None，但确实没有 WS 连接
    # -> 不能报「调用失败」之类，必须准确提示「尚未与协议端建立连接」
    cache5 = GroupInfoCache(_C(_PM([_Adapter(_RealCQ({}))])))
    groups = asyncio.run(cache5.list_groups(force=True))
    assert groups == []
    assert "尚未与协议端建立连接" in cache5.last_error, cache5.last_error
    st = cache5.connection_status()
    assert st["state"] == "not_connected", st
    assert st["adapters"] == 1 and st["clients"] == 1, st
    print("GROUP_CACHE_DISCONNECTED_DETECTED_OK")

    # 用例 2：两个机器人账号同时在线（此前必然一直失败）
    # -> 应对每个 self_id 显式带参调用，成功取到群列表
    cache6 = GroupInfoCache(
        _C(_PM([_Adapter(_RealCQ({"111": object(), "222": object()}))]))
    )
    groups = asyncio.run(cache6.list_groups(force=True))
    assert len(groups) == 2, groups
    assert cache6.last_error == "", cache6.last_error
    st = cache6.connection_status()
    assert st["state"] == "connected" and st["self_ids"] == ["111", "222"], st
    print("GROUP_CACHE_MULTI_ACCOUNT_OK")

    # 用例 3：只有事件通道（event 角色）没有 API 通道 -> 给出专项提示
    class _EventOnlyCQ(_RealCQ):
        def __init__(self):
            super().__init__({})
            self._wsr_event_clients = {object()}

    cache7 = GroupInfoCache(_C(_PM([_Adapter(_EventOnlyCQ())])))
    groups = asyncio.run(cache7.list_groups(force=True))
    assert groups == []
    assert "事件通道" in cache7.last_error, cache7.last_error
    st = cache7.connection_status()
    assert st["state"] == "event_only", st
    print("GROUP_CACHE_EVENT_ONLY_OK")

    # 用例 4：connection_status / iter_clients 的兜底可用性
    cache8 = GroupInfoCache(_C(_PM([_Platform()])))
    st = cache8.connection_status()
    assert isinstance(st, dict) and "state" in st and "message" in st, st
    clients = cache8.iter_clients()
    assert clients and clients[0][1] is not None, clients
    print("GROUP_CACHE_STATUS_HELPER_OK")


def test_errors():
    """协议端错误应被翻译成中文可读提示。"""
    from astrbot_plugin_panshi.core.errors import extract_retcode, hint_for, humanize

    # 用户实际遇到的这条
    raw = (
        "<ActionFailed status='failed', retcode=1200, data=None, "
        "message='cannot ban admin', wording='cannot ban admin', "
        "echo={'seq': 19}, stream='normal-action'>"
    )
    out = humanize(raw)
    assert "管理员" in out, out
    assert "ActionFailed" not in out, out
    assert "retcode" not in out, out
    assert extract_retcode(raw) == 1200
    print(f"ERROR_HUMANIZE_OK ({out})")

    # 其它常见错误
    assert "群主" in humanize("<ActionFailed message='cannot ban owner'>")
    assert "管理员" in humanize("not group admin")
    assert "2 分钟" in humanize("<ActionFailed message='msg not found'>")
    assert "30 天" in humanize("<ActionFailed message='duration invalid'>")
    # 未识别的错误至少不应残留调试外壳
    unknown = humanize("<ActionFailed status='failed', message='some weird thing'>")
    assert unknown == "some weird thing", unknown
    # 空值兜底
    assert humanize(None) == "协议端未返回具体原因"
    print("ERROR_HUMANIZE_MISC_OK")

    # 建议文案
    assert hint_for("set_group_ban")
    assert hint_for("delete_msg")
    assert hint_for("unknown_action") == ""
    print("ERROR_HINT_OK")


def test_role_precheck():
    """身份预检只用于「解释失败」，绝不提前拒绝。

    回归背景：早期版本会在调用 API 前根据角色查询提前拦下，
    但协议端（NapCat 等）对机器人自身角色的上报常不可靠（返回 member 或缺失），
    导致「机器人明明是管理员，禁言普通成员却提示权限不足」。
    """
    import asyncio

    from astrbot_plugin_panshi.core.normal import NormalHandle
    from astrbot_plugin_panshi.data import Storage
    from astrbot_plugin_panshi.config import PluginConfig

    calls = []

    class _Bot:
        async def get_group_member_info(self, group_id, user_id, no_cache=False):
            # 99 = 机器人自己，设为 admin
            role = {1: "owner", 2: "admin", 3: "member", 99: "admin"}.get(
                int(user_id), "member"
            )
            return {"role": role, "card": f"用户{user_id}", "nickname": f"用户{user_id}"}

        async def set_group_ban(self, **kw):
            calls.append(("set_group_ban", kw))
            return {"status": "ok", "retcode": 0}

        async def get_group_member_list(self, **kw):
            return []

    class _Ev:
        def __init__(self, self_id=99):
            self.bot = _Bot()
            self._self_id = self_id

        def get_group_id(self):
            return 100

        def get_sender_id(self):
            return 3

        def get_self_id(self):
            return self._self_id

        def get_messages(self):
            return []

        def get_sender_name(self):
            return "测试"

    # 用临时目录的 Storage，避免污染真实数据
    import tempfile, os

    tmp = tempfile.mkdtemp(prefix="panshi_role_")
    try:
        cfg = PluginConfig({})
        db = Storage(os.path.join(tmp, "t.json"))
        h = NormalHandle(cfg, db)

        # 普通成员 -> 正常放行
        calls.clear()
        r = asyncio.run(h.set_ban(_Ev(), 3, 300))
        assert calls and calls[0][0] == "set_group_ban", calls
        assert "已禁言" in r, r
        print(f"BAN_MEMBER_OK ({r})")

        # 目标是群主/管理员 -> 仍要尝试调用 API，由协议端决定；
        # 失败后的解释里要能看出是对方身份问题
        for tid, kw in ((1, "群主"), (2, "管理员")):
            calls.clear()

            class _BotFail(_Bot):
                async def set_group_ban(self, **kw2):
                    calls.append(("set_group_ban", kw2))
                    return {"status": "failed", "retcode": 1200, "message": "cannot ban admin"}

            class _EvFail(_Ev):
                def __init__(self):
                    super().__init__()
                    self.bot = _BotFail()

            r = asyncio.run(h.set_ban(_EvFail(), tid, 300))
            assert calls, f"应当尝试调用 API（目标 {kw}）"
            assert r.startswith("❌"), r
            assert kw in r, f"失败解释里应点明对方是{kw}: {r}"
        print("BAN_ADMIN_STILL_TRIES_OK")

        # 核心回归：协议端把机器人自己误报为 member 时，禁言普通成员必须成功
        class _BotMisreport:
            async def get_group_member_info(self, group_id, user_id, no_cache=False):
                # 无论谁一律报 member —— 模拟 NapCat 的不可靠上报
                return {"role": "member", "card": "x", "nickname": "x"}

            async def get_group_member_list(self, group_id):
                return [
                    {"user_id": 999, "role": "admin", "nickname": "机器人"},
                    {"user_id": 3, "role": "member", "nickname": "普通成员"},
                ]

            async def set_group_ban(self, **kw):
                calls.append(("set_group_ban", kw))
                return {"status": "ok", "retcode": 0}

        class _EvMisreport(_Ev):
            def __init__(self):
                super().__init__()
                self.bot = _BotMisreport()

            def get_self_id(self):
                return 999

        calls.clear()
        r = asyncio.run(h.set_ban(_EvMisreport(), 3, 300))
        assert calls, "误报 member 时也必须真的调用 API"
        assert "已禁言" in r, r
        print(f"BAN_MISREPORTED_ROLE_OK ({r})")

        # 协议端返回 status=failed 的 dict 也应被识别为失败
        class _BotDictFail(_Bot):
            async def set_group_ban(self, **kw):
                return {"status": "failed", "retcode": 1200, "message": "cannot ban admin"}

        class _EvDict(_Ev):
            def __init__(self):
                super().__init__()
                self.bot = _BotDictFail()

        r = asyncio.run(h.set_ban(_EvDict(), 3, 300))
        assert "管理员" in r, r
        print(f"PRECHECK_DICT_FAILURE_OK ({r})")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_curfew_intent():
    """宵禁自然语言设置：意图白名单、时间归一化、配置写入与即时启停。"""
    import asyncio
    import os
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.intent import IntentParser
    from astrbot_plugin_panshi.core.intent_executor import IntentExecutor
    from astrbot_plugin_panshi.data import Storage

    # 1) set_curfew 必须在意图白名单里
    parsed = IntentParser._extract_json(
        '{"action": "set_curfew", "start": "23:30", "end": "07:00"}'
    )
    assert parsed and parsed["action"] == "set_curfew", parsed

    from astrbot_plugin_panshi.core.intent import _ACTION_WORDS as _WORDS

    assert "宵禁" in _WORDS, "动作关键词应包含「宵禁」以触发智能识别"
    print("CURFEW_INTENT_WHITELIST_OK")

    # 2) 完整链路：解析参数 -> 写配置 -> 即时启停
    raw = {
        "automate": {
            "curfew_enable": False,
            "curfew_start": "23:00",
            "curfew_end": "07:00",
        }
    }
    cfg = PluginConfig(raw)
    tmp = tempfile.mkdtemp(prefix="panshi_curfew_")
    apply_calls = []

    class _Auto:
        async def apply_now(self):
            apply_calls.append(dict(cfg.dump().get("automate", {}) or {}))
            return "banned_now"

    try:
        db = Storage(os.path.join(tmp, "t.json"))
        ex = IntentExecutor(cfg, db, None, None, None, _Auto())

        # 只改时段 + 开启；"7:00" 应归一化为 "07:00"
        r = asyncio.run(
            ex.set_curfew({"action": "set_curfew", "start": "23:30", "end": "7:00", "enable": True})
        )
        assert cfg.get("automate", "curfew_start") == "23:30", cfg.get("automate", {})
        assert cfg.get("automate", "curfew_end") == "07:00", cfg.get("automate", {})
        assert cfg.get("automate", "curfew_enable") is True
        assert "23:30" in r and "07:00" in r, r
        assert "已自动开启全体禁言" in r, r
        assert apply_calls and apply_calls[-1].get("curfew_enable") is True
        print(f"CURFEW_SET_OK ({r.splitlines()[0]})")

        # 只关闭，不改时间
        r2 = asyncio.run(ex.set_curfew({"action": "set_curfew", "enable": False}))
        assert cfg.get("automate", "curfew_enable") is False
        assert cfg.get("automate", "curfew_start") == "23:30"  # 时段保持
        assert "已关闭" in r2, r2

        # 非法时间应被拒绝且不写配置
        before = dict(cfg.dump().get("automate", {}) or {})
        r3 = asyncio.run(ex.set_curfew({"action": "set_curfew", "start": "abc"}))
        assert "看不懂" in r3, r3
        assert cfg.dump().get("automate", {}) == before, "非法时间不应写入配置"

        # 没带任何参数 -> 给出用法提示
        r4 = asyncio.run(ex.set_curfew({"action": "set_curfew"}))
        assert "没听懂" in r4, r4
        print("CURFEW_EDGE_OK")

        # 时间归一化工具
        from astrbot_plugin_panshi.core.intent_executor import _norm_hhmm

        assert _norm_hhmm("23:30") == "23:30"
        assert _norm_hhmm("7:00") == "07:00"
        assert _norm_hhmm("2330") == "23:30"
        assert _norm_hhmm("700") == "07:00"
        assert _norm_hhmm("24:00") is None
        assert _norm_hhmm("abc") is None
        assert _norm_hhmm("") is None
        print("CURFEW_NORM_OK")
    finally:
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


def test_banword_and_backup(inst):
    """违禁词自然语言维护 + 面板配置导出/导入回路。"""
    import asyncio

    from astrbot_plugin_panshi.pages_service import PageService

    ex = inst.executor

    # 1) 添加违禁词
    r = ex._banword("测试违禁ABC", add=True)
    assert "已添加" in r, r
    words = inst.cfg.get("guard", "forbidden_words", []) or []
    assert "测试违禁ABC" in words, words

    # 2) 重复添加
    r2 = ex._banword("测试违禁ABC", add=True)
    assert "存在" in r2, r2

    # 3) 模糊匹配删除（输入词是已有词的子串）
    r3 = ex._banword("测试违禁", add=False)
    assert "已删除" in r3 and "测试违禁ABC" in r3, r3
    words = inst.cfg.get("guard", "forbidden_words", []) or []
    assert "测试违禁ABC" not in words, words

    # 4) 空词与不存在的词
    assert "请告诉我" in ex._banword("  ", add=True)
    assert "没有" in ex._banword("根本不存在XYZ", add=False)
    print("BANWORD_INTENT_OK")

    # 5) 导出 -> 改动 -> 导入还原
    svc = PageService(inst.cfg, inst.db, inst.group_cache)
    backup = svc.export_all()
    assert backup["plugin"] == "astrbot_plugin_panshi"
    assert "global_config" in backup and "groups" in backup

    # 导出后改点东西，再导入应恢复
    svc.update_global_config({"guard": {"forbidden_words": ["导入前临时词"]}})
    svc.update_group_config("777888", {"follow_default": False, "guard": {"spam_count": 6}})
    result = svc.import_all(backup)
    assert result["ok"] is True, result

    snap = inst.cfg.get("guard", "forbidden_words", []) or []
    assert snap == backup["global_config"]["guard"]["forbidden_words"], snap
    ov = inst.db.get_group_override("777888")
    assert ov and ov.get("guard", {}).get("spam_count") == 6, ov
    print(f"BACKUP_ROUNDTRIP_OK (恢复 {result['restored_groups']} 个群覆盖)")

    # 6) 导入非对象应报错
    try:
        svc.import_all(["not", "a", "dict"])
        raise AssertionError("非法导入竟然通过了")
    except ValueError:
        pass
    # 清理测试群覆盖
    inst.db.reset_group("777888")
    print("BACKUP_VALIDATION_OK")


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

    # 回归：前端漏传 follow_default 时必须保持独立配置，不能退回全局
    gc2b = svc.update_group_config("123456", {"guard": {"spam_count": 12}})
    assert gc2b["follow_default"] is False, f"漏传 follow_default 竟退回了全局: {gc2b}"
    assert gc2b["effective"]["guard"]["spam_count"] == 12, gc2b["effective"]["guard"]
    print("GROUP_OVERRIDE_NO_FLAG_OK")

    # 显式传 true 才应清空覆盖
    gc2c = svc.update_group_config("123456", {"follow_default": True})
    assert gc2c["follow_default"] is True
    assert gc2c["effective"]["guard"]["spam_count"] == 7
    print("GROUP_FOLLOW_EXPLICIT_OK")

    # 回归（用户实测：独立配置保存后又变回全局）：重新读盘必须仍是独立配置
    svc.update_group_config("123456", {"follow_default": False, "guard": {"spam_count": 15}})
    reread = svc.get_group_config("123456")
    assert reread["follow_default"] is False, f"重读后又变回全局了: {reread}"
    assert reread["effective"]["guard"]["spam_count"] == 15, reread["effective"]["guard"]
    # 换一个新 service 实例读同一份存储，模拟插件重载
    svc2 = PageService(inst.cfg, inst.db, inst.group_cache)
    reread2 = svc2.get_group_config("123456")
    assert reread2["follow_default"] is False, f"重载插件后又变回全局了: {reread2}"
    assert reread2["effective"]["guard"]["spam_count"] == 15, reread2["effective"]["guard"]
    print("GROUP_OVERRIDE_PERSIST_OK")

    # 恢复默认
    gc3 = svc.reset_group_config("123456")
    assert gc3["follow_default"] is True
    assert gc3["effective"]["guard"]["spam_count"] == 7
    print("GROUP_RESET_OK")

    # 回归（用户实测：独立配置保存后像被写死、改全局对该群不生效）：
    # override 必须只记录「与全局不同的字段」，不能把整组生效值固化下来。
    svc.reset_group_config("123456")
    svc.update_global_config({"guard": {"spam_count": 5}})
    svc.update_group_config("123456", {"follow_default": False})
    # 模拟最坏情况：前端把整组生效值都提交回来
    full = svc.get_group_config("123456")
    payload = {"follow_default": False, "guard": dict(full["effective"]["guard"])}
    payload["guard"]["spam_count"] = 11
    after = svc.update_group_config("123456", payload)
    ov = (after["override"] or {}).get("guard", {})
    assert list(ov.keys()) == ["spam_count"], f"只应固化改动项，实际 {ov}"
    assert ov["spam_count"] == 11, ov

    # 改全局后：显式改过的保持，没改过的跟随
    svc.update_global_config({"guard": {"spam_count": 3, "spam_window": 9}})
    reread = svc.get_group_config("123456")
    assert reread["effective"]["guard"]["spam_count"] == 11, reread["effective"]["guard"]
    assert reread["effective"]["guard"]["spam_window"] == 9, reread["effective"]["guard"]
    print("GROUP_OVERRIDE_DIFF_ONLY_OK")

    # 改回与全局一致时，该分组的覆盖应自动消失
    same = svc.get_group_config("123456")
    p2 = {"follow_default": False, "guard": dict(same["effective"]["guard"])}
    p2["guard"]["spam_count"] = 3
    after2 = svc.update_group_config("123456", p2)
    assert (after2["override"] or {}).get("guard", {}) == {}, after2["override"]
    assert after2["follow_default"] is False, "清掉覆盖后仍应是独立配置"
    print("GROUP_OVERRIDE_SELF_CLEAN_OK")
    svc.reset_group_config("123456")

    # 非法群号
    try:
        svc.get_group_config("abc")
        raise AssertionError("非法群号竟然通过了")
    except ValueError:
        pass
    print("GROUP_ID_VALIDATION_OK")


if __name__ == "__main__":
    main()
