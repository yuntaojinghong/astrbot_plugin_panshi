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
    test_curfew_lift_reporting()
    test_per_group_runtime()
    test_issue_regressions(inst)
    test_banword_and_backup(inst)
    test_page_service(inst)
    test_local_intent()
    test_interact_layer()
    test_panel_layer()
    test_natural_language()
    test_intent_gate()
    test_command_vs_question()
    test_never_target_self()
    test_notice_dispatch()

    print("ALL_SELFTEST_PASS")


def test_notice_dispatch():
    """入群通知必须能从 on_notice 一路走到欢迎语。

    线上反馈「入群没欢迎」。on_notice 原本挂着
    `@filter.event_message_type(GROUP_MESSAGE)`，而 AstrBot 的 aiocqhttp 适配器在
    `_convert_handle_notice_event()` 里是按「有没有 group_id」把通知事件标成
    GROUP_MESSAGE 或 OTHER_MESSAGE 的 —— 被标成后者的通知会被该过滤器静默丢弃，
    日志里一个字都不会有，非常难查。

    本用例直接驱动 on_notice，断言：
      · group_increase 通知能产出欢迎语
      · 非通知事件（post_type 不是 notice）被忽略，不会误发
      · 关闭欢迎时不再产出内容
    """
    import asyncio

    from astrbot_plugin_panshi.main import PanshiPlugin

    GID = 1077250302
    NEWBIE = "226067490"

    class _Raw:
        """模拟 event.message_obj（AstrBot 把原始事件挂在 raw_message 上）。"""

        def __init__(self, post_type="notice", notice_type="group_increase",
                     user_id=NEWBIE, sub_type="approve"):
            self.post_type = post_type
            self.notice_type = notice_type
            self.user_id = user_id
            self.sub_type = sub_type

    class _Bot:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            async def _m(**kw):
                self.calls.append((name, kw))
                if name == "get_group_member_info":
                    return {"card": "", "nickname": f"新人{NEWBIE}"}
                return {"status": "ok", "retcode": 0}
            return _m

    class _Ev:
        def __init__(self, robot, raw):
            self.bot = _Bot()
            self.message_obj = raw
            self._group = GID
            self._self = robot
            self._stopped = False

        def get_group_id(self):
            return self._group

        def get_self_id(self):
            return self._self

        def get_sender_id(self):
            return NEWBIE

        def get_sender_name(self):
            return "新人"

        def get_messages(self):
            return []

        def is_stopped(self):
            return self._stopped

        def stop_event(self):
            self._stopped = True

        def plain_result(self, text):
            return {"text": text}

    def run_notice(cfg_extra=None, raw=None):
        from astrbot_plugin_panshi.config import PluginConfig

        cfg = {"basic": {"default_ban_time": 60}, "welcome": {"welcome_enable": True}}
        if cfg_extra:
            for key, val in cfg_extra.items():
                cfg.setdefault(key, {}).update(val)

        class _Ctx:
            def register_web_api(self, *a, **k):
                pass

        inst = PanshiPlugin(_Ctx(), cfg)
        ev = _Ev(inst.cfg.get("basic", "self_id", "") or "3823105457",
                 raw or _Raw())

        async def collect():
            out = []
            async for item in inst.on_notice(ev):
                out.append(item)
            return out

        return asyncio.run(collect())

    # 1) 入群通知 → 应产出欢迎语
    got = run_notice()
    assert got, "入群通知没有产出欢迎语 —— on_notice 分派断了"
    text = got[0].get("text") if isinstance(got[0], dict) else str(got[0])
    assert text and len(text.strip()) > 0, f"欢迎语是空的: {got!r}"
    print(f"NOTICE_WELCOME_OK ({text[:26]}…)")

    # 2) 非通知事件 → 不该产出任何东西（避免误发）
    got2 = run_notice(raw=_Raw(post_type="message", notice_type="group_increase"))
    assert not got2, f"非通知事件竟然产出了内容: {got2!r}"

    # 3) 关闭欢迎 → 不产出
    got3 = run_notice(cfg_extra={"welcome": {"welcome_enable": False}})
    assert not got3, f"已关闭欢迎却仍然产出: {got3!r}"

    # 4) 退群通知 → 有会员退群时不该崩（内容可有可无）
    run_notice(raw=_Raw(notice_type="group_decrease"))
    print("NOTICE_OTHER_TYPES_OK")


# ======================================================================
#  v1.7.3：权限分流与自我目标保护
# ======================================================================
def test_command_vs_question():
    """区分「管理指令」与「普通提问」。

    线上现象：无权限的群友 @机器人 问「总结一下今天群里聊了什么」，
    磐石日志说「无权限已忽略」，可机器人还是回答了 —— 因为磐石只是 return，
    没有消费事件，消息继续流向 AstrBot。

    修法要在两者之间分流：
      · 普通提问 → 放给 AstrBot 回答（拦下来群友就问不了问题了）
      · 管理指令 → 明确回权限不足并消费事件（否则又是"日志说忽略、它却回话"）
    """
    from astrbot_plugin_panshi.core.intent_gate import looks_like_command

    # 应当判为管理指令
    for text in (
        "有本事你禁言我", "禁言 5m", "把张三踢了", "撤回他刚才那条",
        "开全体禁言", "关闭全体禁言", "给他一个警告", "改名片 阿伟",
        "设置头衔 学霸", "发个群公告", "开启宵禁", "签到",
        "加入违禁词 广告", "上管 @张三", "清屏",
    ):
        assert looks_like_command(text), f"这明明是指令，却被当成提问: {text!r}"

    # 应当判为普通提问（拦下来会让群友问不了问题）
    for text in (
        "总结一下今天群里聊了什么",
        "你是什么样的气候",
        "介绍一下你自己",
        "今天天气怎么样",
        "我爸妈会对我说一些气话",
        "？", "蠢鱼", "你好冷淡", "能不能对我好一点",
        "大智若鱼～",
    ):
        assert not looks_like_command(text), f"这是普通提问，却被当成指令: {text!r}"

    print("COMMAND_VS_QUESTION_OK")


def test_never_target_self():
    """禁止把机器人自己当成操作对象。

    线上现象：用户说「有本事你禁言我」，模型把目标解析成了机器人自己，
    协议端回 cannot ban admin，回执却是
        ❌ 对 deepseek-v4.1-flash 的禁言失败：...
    那个名字是机器人自己的昵称，用户完全看不懂发生了什么。

    现在遇到「目标 == 自己」直接跳过并说明，不发那趟注定失败的 API。
    """
    import asyncio

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.normal import NormalHandle

    BOT = "3823105457"
    OTHER = "226067490"

    class _Cli:
        def __init__(self):
            self.calls = []

        async def __call__(self, action, **params):
            self.calls.append((action, params))
            return {"status": "ok", "retcode": 0}

    class _Bot:
        def __init__(self, cli):
            self._cli = cli

        def __getattr__(self, name):
            async def _m(**kw):
                return await self._cli(name, **kw)
            return _m

    class _Ev:
        def __init__(self, cli):
            self.bot = _Bot(cli)

        def get_group_id(self):
            return 1001

        def get_self_id(self):
            return BOT

        def get_sender_id(self):
            return "999"

        def get_sender_name(self):
            return "群友"

        def get_messages(self):
            return []

        async def get_group_member_info(self, **kw):
            return {"card": "", "nickname": f"u{kw.get('user_id')}"}

    for method, label in (("set_ban", "禁言"), ("kick", "踢出")):
        cli = _Cli()
        handle = NormalHandle(PluginConfig({"basic": {"default_ban_time": 60}}), None)
        if method == "set_ban":
            reply = asyncio.run(handle.set_ban(_Ev(cli), BOT, 300))
        else:
            reply = asyncio.run(handle.kick(_Ev(cli), BOT))
        assert "机器人自己" in reply, f"{label}：目标是自己却没被拦住 -> {reply!r}"
        # 只关心「有没有真的下发处置动作」——查昵称那步不算
        acted = [c for c in cli.calls
                 if c[0] in ("set_group_ban", "set_group_kick")]
        assert not acted, f"{label}：目标是自己却仍然下发了 API 调用 -> {acted}"

    # 正常目标仍要照常执行（别把功能一起修死）
    cli = _Cli()
    handle = NormalHandle(PluginConfig({"basic": {"default_ban_time": 60}}), None)
    reply = asyncio.run(handle.set_ban(_Ev(cli), OTHER, 300))
    bans = [c for c in cli.calls if c[0] == "set_group_ban"]
    assert len(bans) == 1, f"正常禁言没下发: {cli.calls}"
    assert bans[0][1].get("user_id") == int(OTHER), bans
    assert "已禁言" in reply, reply

    print("NEVER_TARGET_SELF_OK")


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


def test_natural_language():
    """v1.5.1 核心：听得懂人话（LLM 为主 + 本地快通道）。

    覆盖三件事：
    1. 口语化表达能触发智能识别（不再局限于指令词）；
    2. 闲聊不被误触发（省 token）；
    3. 供应商解析默认走 AstrBot 已配置的模型（三级兜底）。
    """
    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.intent import IntentParser

    cfg = PluginConfig({})
    parser = IntentParser(cfg, None, None)

    class _Ev:
        message_str = ""
        unified_msg_origin = "aiocqhttp:GroupMessage:1001"

        def __init__(self, msgs=None):
            self._msgs = msgs or []

        def get_group_id(self):
            return "1001"

        def get_sender_id(self):
            return "999"

        def get_self_id(self):
            return "777"

        def get_sender_name(self):
            return "测试员"

        def get_messages(self):
            return self._msgs

    # ---- 1. 人话触发：这些句子没有「禁言/踢」等标准指令词，但明显在指挥 ----
    human_lines = [
        "群里太吵了，收拾一下",
        "帮我看看谁在刷广告",
        "有人一直复读，能不能管管",
        "这人怎么老发广告啊，处理一下",
    ]
    for line in human_lines:
        ev = _Ev()
        assert parser.should_trigger(ev, line), f"人话未触发: {line}"

    # ---- 2. 标准指令词依旧触发 ----
    for line in ["禁言张三", "@某人 踢了", "全体禁言"]:
        assert parser.should_trigger(_Ev(), line), f"指令未触发: {line}"

    # ---- 3. 闲聊不触发（省 token），且不以「@机器人」为借口漏掉 ----
    for line in ["哈哈哈笑死我了", "晚安各位", "早上好"]:
        assert not parser.should_trigger(_Ev(), line), f"闲聊误触发: {line}"

    # ---- 4. 已是指令前缀的不走智能通道 ----
    assert not parser.should_trigger(_Ev(), "/禁言 123 10m")

    # ---- 5. 开关可关：trigger_on_intent=False 时软信号不再触发 ----
    cfg_off = PluginConfig({"smart": {"trigger_on_intent": False}})
    p_off = IntentParser(cfg_off, None, None)
    assert not p_off.should_trigger(_Ev(), "群里太吵了，收拾一下")

    # ---- 6. 供应商解析：默认落到 AstrBot 已配置的模型 ----
    class _Prov:
        def __init__(self, pid):
            self.pid = pid

    class _Ctx:
        def __init__(self, allp, by_id=None, session=None):
            self._all = allp
            self._by_id = by_id or {}
            self._session = session

        def get_all_providers(self):
            return self._all

        def get_provider_by_id(self, provider_id=None):
            return self._by_id.get(provider_id)

        async def get_using_provider_async(self, umo=None):
            return self._session

    class _EvCtx(_Ev):
        """带 AstrBot 上下文的假事件（_get_provider 从 event 取 ctx）。"""

        def __init__(self, ctx):
            super().__init__()
            self._ctx = ctx

        def get_context(self):
            return self._ctx

    import asyncio

    loop = asyncio.new_event_loop()

    # 6a. 面板未指定 -> 用会话默认模型
    ctx = _Ctx([_Prov("global")], session=_Prov("session"))
    p = IntentParser(PluginConfig({}), None, None)
    got = loop.run_until_complete(p._get_provider(_EvCtx(ctx)))
    assert got is not None and got.pid == "session", got

    # 6b. 会话也没有 -> 用全局第一个模型（这就是「默认用 AstrBot 里配好的」）
    ctx2 = _Ctx([_Prov("global")], session=None)
    p2 = IntentParser(PluginConfig({}), None, None)
    got2 = loop.run_until_complete(p2._get_provider(_EvCtx(ctx2)))
    assert got2 is not None and got2.pid == "global", got2

    # 6c. 一个模型都没有 -> 返回 None 并给出可读原因
    ctx3 = _Ctx([], session=None)
    p3 = IntentParser(PluginConfig({}), None, None)
    got3 = loop.run_until_complete(p3._get_provider(_EvCtx(ctx3)))
    assert got3 is None, got3
    assert p3.last_error and "模型" in p3.last_error, p3.last_error

    print("NATURAL_LANGUAGE_OK")


def test_intent_gate():
    """v1.6.0 核心：意图闸门 —— 用零成本本地判定把废话挡在模型之外。

    验证四道闸门与信用额度都能正确工作，且不误伤真正的人话指挥。
    """
    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.context import ContextCollector
    from astrbot_plugin_panshi.core.intent_gate import IntentGate

    cfg = PluginConfig({})
    ctx = ContextCollector()
    gate = IntentGate(cfg, ctx)

    # ---------- 闸门③：闲聊 / 否定直接否决 ----------
    for line in ["哈哈哈笑死", "晚安", "吃饭去了"]:
        ok, why = gate.allow("g1", line)
        assert not ok, f"闲聊未拦截: {line} -> {why}"

    for line in ["别管他", "不用处理了", "算了随他吧", "开玩笑的"]:
        ok, why = gate.allow("g1", line)
        assert not ok, f"否定语境未拦截: {line} -> {why}"

    # ---------- 闸门①：无上下文佐证时，纯陈述不该放行 ----------
    ok, why = gate.allow("g2", "张三是广告")
    assert not ok, f"纯陈述未拦截: {why}"

    # ---------- 闸门①②：有异常迹象 + 效果动词 + 目标指代 -> 放行 ----------
    ctx.record(_make_ctx_event("g3", "111", "小明", "刷屏刷屏刷屏"))
    ctx.record(_make_ctx_event("g3", "111", "小明", "AAAAAAAAAA"))
    ok, why = gate.allow("g3", "这个人一直在刷屏，帮我处理一下")
    assert ok, f"有效人话被误拦: {why}"

    # ---------- 额度扣减 ----------
    left_before = gate.budget_left("g3")
    assert left_before <= 19, left_before  # 上面用掉 1 点

    # ---------- 闸门④：冷却生效 ----------
    ok2, why2 = gate.allow("g3", "楼上刷屏了，管一下")
    assert not ok2 and why2 == "冷却中", (ok2, why2)

    # 关掉冷却后，额度继续扣
    cfg2 = PluginConfig({"smart": {"intent_cooldown": 0, "intent_budget": 3}})
    gate2 = IntentGate(cfg2, ContextCollector())
    gate2.ctx.record(_make_ctx_event("g4", "222", "小红", "发广告"))

    allowed = 0
    for i in range(6):
        ok3, why3 = gate2.allow("g4", f"有人发广告，处理一下 {i}")
        if ok3:
            allowed += 1
    assert allowed == 3, f"额度应放行 3 次，实际 {allowed}"

    # 额度耗尽后仍被拦截，且原因可读
    ok4, why4 = gate2.allow("g4", "又有人发广告了，管管")
    assert not ok4 and why4 == "额度已用尽", (ok4, why4)

    # ---------- 缓存 ----------
    g3 = IntentGate(PluginConfig({}), ContextCollector())
    assert g3.cached("g9", "禁言张三") is None
    g3.remember("g9", "禁言张三", {"action": "ban"})
    got = g3.cached("g9", "禁言张三")
    assert got and got["action"] == "ban", got
    # 不同群不共享缓存
    assert g3.cached("g10", "禁言张三") is None

    # ---------- 统计可读 ----------
    desc = g3.describe()
    assert "拦截" in desc and "模型调用" in desc, desc

    # ---------- 额度为 0：完全不调用模型 ----------
    g0 = IntentGate(PluginConfig({"smart": {"intent_budget": 0}}), ContextCollector())
    g0.ctx.record(_make_ctx_event("g5", "333", "小刚", "刷屏了"))
    ok5, _ = g0.allow("g5", "有人在刷屏，处理下")
    assert not ok5, "额度为 0 时不应放行"

    # ---------- 与 should_trigger 串联 ----------
    from astrbot_plugin_panshi.core.intent import IntentParser

    class _Ev:
        message_str = ""

        def get_group_id(self):
            return "g7"

        def get_messages(self):
            return []

        def get_self_id(self):
            return "777"

    p = IntentParser(PluginConfig({}), None, ctx, gate=gate)
    # 明确动作词直接放行（不经过闸门）
    assert p.should_trigger(_Ev(), "禁言张三")
    # 闲聊被闸门拦住
    assert not p.should_trigger(_Ev(), "哈哈哈")

    print("INTENT_GATE_OK")


def _make_ctx_event(group_id, user_id, name, text):
    """构造一个可供 ContextCollector 记录的最小事件。"""

    class _E:
        message_str = text

        def get_group_id(self):
            return group_id

        def get_sender_id(self):
            return user_id

        def get_sender_name(self):
            return name

    return _E()


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

    # 意图闸门的两个新配置项必须存在（v1.6.0）
    smart_fields = {
        f["key"] if isinstance(f, dict) else f
        for f in next(g for g in groups if g["key"] == "smart")["fields"]
    }
    for need in ("trigger_on_intent", "intent_budget", "intent_cooldown", "local_fast_path"):
        assert need in smart_fields, f"缺少配置项 {need}（现有：{sorted(smart_fields)}）"

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


def test_per_group_runtime():
    """回归 Issue #1：面板「按群独立配置」必须在运行期真正生效。

    v1.6.0 只把 guard/panel 切到了 cfg_for()，welcome / join / activity /
    warning 仍在读全局配置，导致这几组「独立配置」在群里静默失效。
    """
    import asyncio
    import os
    import shutil
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.activity import ActivityHandle
    from astrbot_plugin_panshi.core.join import JoinHandle
    from astrbot_plugin_panshi.core.warning import WarningHandle
    from astrbot_plugin_panshi.core.welcome import WelcomeHandle
    from astrbot_plugin_panshi.data import Storage

    GID, UID = "123456", "789"
    global_cfg = {
        "basic": {"anon_protect": True, "super_admins": [], "enable_groups": []},
        "guard": {"forbidden_enable": False, "whitelist": [], "forbidden_words": []},
        "warning": {
            "warning_enable": True,
            "warn_expire_days": 30,
            "warn_ban_threshold": 3,
            "warn_kick_threshold": 5,
            "warn_ban_time": 3600,
            "escalation_ladder": "",
        },
        "welcome": {
            "welcome_enable": True,
            "verify_enable": False,
            "join_review_enable": False,
            "join_accept_words": [],
            "join_reject_words": [],
            "join_no_match_reject": False,
            "leave_block": False,
            "leave_notify": True,
        },
        "activity": {"checkin_enable": True, "checkin_points": 10, "checkin_random_bonus": 0},
    }

    class _Bot:
        def __init__(self):
            self.calls = []

        def _mk(self, name):
            async def _call(**kw):
                self.calls.append((name, kw))
                return {"status": "ok", "retcode": 0}

            return _call

        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            return self._mk(name)

    class _Ev:
        message_str = ""

        def __init__(self, text=""):
            self.message_str = text
            self.bot = _Bot()

        def get_group_id(self):
            return int(GID)

        def get_sender_id(self):
            return int(UID)

        def get_self_id(self):
            return 999

        def get_sender_name(self):
            return "用户3"

        def get_messages(self):
            return []

    def stack():
        tmp = tempfile.mkdtemp(prefix="panshi_pg_")
        db = Storage(os.path.join(tmp, "d.json"))
        cfg = PluginConfig(global_cfg)
        cfg.bind_storage(db)
        return tmp, cfg, db

    tmp, cfg, db = stack()
    try:
        # --- 回归：raw 是 AstrBotConfig 那种「带线程锁的 dict 子类」时，
        #     for_group 不能炸。
        #
        #     线上日志：
        #       [磐石] 读取按群配置失败，已退化为全局配置: cannot pickle '_thread.lock' object
        #     根因：AstrBotConfig 继承 dict，但实例上挂了 _save_state_lock /
        #     _save_commit_lock（threading.Lock）。copy.deepcopy 对 dict 子类会去
        #     pickle 它的 __dict__，撞上锁直接抛 TypeError，异常冒到 cfg_for()
        #     被兜底 catch 成「退化为全局配置」——**按群配置静默失效**。
        #
        #     为什么以前没测出来：这里的 cfg 用的是普通 dict，deepcopy 不走 __dict__。
        import threading

        class _LockedConfig(dict):
            """模拟真实 AstrBotConfig：dict 子类 + 实例上挂锁。"""

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._save_state_lock = threading.Lock()
                self._save_commit_lock = threading.Lock()
                self._save_revision = 0

            def save_config(self, replace_config=None, indent=None):
                return True

        locked_cfg = PluginConfig(_LockedConfig(global_cfg))
        locked_cfg.bind_storage(db)
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(GID, "guard", {"spam_count": 9})
        view = locked_cfg.for_group(GID)
        assert view.guard.get("spam_count") == 9, (
            f"带锁配置对象下按群覆盖没生效（又退化全局了）: {view.guard.get('spam_count')}"
        )
        print("FOR_GROUP_LOCKED_CONFIG_OK")

        # --- welcome.welcome_enable 按群关闭 ---
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(GID, "welcome", {"welcome_enable": False})
        wh = WelcomeHandle(cfg, db)
        assert asyncio.run(wh.on_member_increase(_Ev(), "555", "approve")) is None, (
            "按群关闭欢迎后仍然发了欢迎语"
        )

        # --- welcome.leave_notify 按群关闭 ---
        db.reset_group(GID)
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(GID, "welcome", {"leave_notify": False})
        assert asyncio.run(WelcomeHandle(cfg, db).on_member_decrease(_Ev(), "555")) is None, (
            "按群关闭退群播报后仍然播报"
        )

        # --- welcome.join_review_enable 按群开启 ---
        db.reset_group(GID)
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(
            GID, "welcome", {"join_review_enable": True, "join_accept_words": ["暗号"]}
        )
        r = asyncio.run(JoinHandle(cfg, db).on_request(_Ev(), "flag1", "555", "暗号", "add"))
        assert r and "批准" in r, f"按群开启入群审核后没有批准: {r!r}"

        # --- activity.checkin_points 按群生效 ---
        db.reset_group(GID)
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(GID, "activity", {"checkin_points": 100})
        asyncio.run(ActivityHandle(cfg, db).checkin(_Ev()))
        assert db.get_points(GID, UID) == 100, (
            f"按群签到积分未生效: {db.get_points(GID, UID)}"
        )

        # --- warning.warn_kick_threshold 按群生效 ---
        db.reset_group(GID)
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(
            GID, "warning", {"warn_kick_threshold": 1, "warn_ban_threshold": 0}
        )
        ev = _Ev()
        asyncio.run(WarningHandle(cfg, db).add_warning(ev, UID, "测试"))
        assert any(c[0] == "set_group_kick" for c in ev.bot.calls), (
            f"按群警告阈值未触发踢出: {[c[0] for c in ev.bot.calls]}"
        )

        # --- guard.whitelist 按群生效（guard 早已切 cfg_for，防回归） ---
        from astrbot_plugin_panshi.core.guard import GuardHandle

        db.reset_group(GID)
        db.set_group_override(GID, "follow_default", False)
        db.set_group_override(GID, "guard", {"whitelist": [UID], "forbidden_enable": True})
        ev2 = _Ev("加微信 免费领取")
        assert asyncio.run(GuardHandle(cfg, db).inspect(ev2, "1")) is None, (
            "按群白名单没有豁免处罚"
        )
        print("PER_GROUP_RUNTIME_OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_curfew_lift_reporting():
    """回归 Issue #2：解禁失败时绝不能回报「已解除」。

    v1.6.0 补上了「关闭宵禁时下发解禁」，但无论下发成功与否都回 lifted_now，
    且未校验协议端返回值 / 未连接的情况，仍会出现「提示与事实相反」。
    """
    import asyncio
    import os
    import shutil
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.automate import AutomateHandle
    from astrbot_plugin_panshi.data import Storage

    GID = "123456"
    tmp = tempfile.mkdtemp(prefix="panshi_curfew_")
    _seq = {"n": 0}

    def fresh_db():
        """每个场景独立存储：宵禁禁言记录是持久化的，共用会互相污染。"""
        _seq["n"] += 1
        return Storage(os.path.join(tmp, f"d{_seq['n']}.json"))

    class _Cli:
        def __init__(self, fail=False, retcode=0):
            self.calls = []
            self.fail = fail
            self.retcode = retcode

        async def call_action(self, action, **kw):
            self.calls.append((action, kw))
            if self.fail:
                raise RuntimeError("协议端未连接")
            return {"status": "ok" if self.retcode == 0 else "failed", "retcode": self.retcode}

    def make(cli, storage=None, group=GID):
        cfg = PluginConfig(
            {
                "automate": {
                    "curfew_enable": True,
                    "curfew_start": "00:00",
                    "curfew_end": "23:59",
                    "announce_enable": False,
                }
            }
        )
        auto = AutomateHandle(cfg, storage if storage is not None else fresh_db())

        async def sender(gid, enable):
            result = await cli.call_action(
                "set_group_whole_ban", group_id=int(gid), enable=enable
            )
            return not (result.get("status") == "failed" or result.get("retcode") not in (None, 0))

        auto.bind_sender(sender, [group])

        async def provider():
            return [group]

        auto.bind_groups_provider(provider)
        return cfg, auto

    # 1) 正常：关闭宵禁 -> 下发解禁 -> lifted_now
    cli = _Cli()
    cfg, auto = make(cli)
    asyncio.run(auto.apply_now())
    cfg.raw["automate"]["curfew_enable"] = False
    assert asyncio.run(auto.apply_now()) == "lifted_now"
    assert cli.calls[-1][1].get("enable") is False

    # 2) 禁言下发失败（协议端不可用）：禁言没确认成功，但「可能已生效」，
    #    因此关闭宵禁时必须尝试解禁，且解禁同样失败 -> 只能回报 lift_failed，
    #    绝不能回报「已解除」（那正是 Issue #2 里「提示与事实相反」的形态）。
    cli_fail = _Cli(fail=True)
    cfg2, auto2 = make(cli_fail)
    asyncio.run(auto2.apply_now())
    cfg2.raw["automate"]["curfew_enable"] = False
    state = asyncio.run(auto2.apply_now())
    assert state == "lift_failed", f"禁言/解禁都失败却回报: {state}"
    assert auto2.has_pending_lift() is True, "解禁失败却不再重试"
    assert any(
        c[0] == "set_group_whole_ban" and c[1].get("enable") is False for c in cli_fail.calls
    ), f"没有尝试下发解禁: {cli_fail.calls}"

    # 3) 协议端返回 retcode != 0：同样不能算成功
    cli_rc = _Cli(retcode=1400)
    cfg3, auto3 = make(cli_rc)
    asyncio.run(auto3.apply_now())
    cfg3.raw["automate"]["curfew_enable"] = False
    assert asyncio.run(auto3.apply_now()) == "lift_failed", "retcode 非 0 却回报已解除"

    # 4) 重启后仍能解禁（Issue #2 的残留形态）：
    #    上一个进程把群置为全体禁言后崩了/被重启，新进程内存标记为 False，
    #    若只看内存标记，这个群将永远没人解除。
    try:
        db4 = Storage(os.path.join(tmp, "restart.json"))
        cli4 = _Cli()
        cfg4, auto4 = make(cli4, storage=db4)
        asyncio.run(auto4.apply_now())  # 进入宵禁 -> 全体禁言
        assert db4.get_curfew_banned() == [GID], db4.get_curfew_banned()
        cli4.calls.clear()

        # —— 模拟重启：全新的 handle + 内存标记为 False，但存储里记着这个群
        cfg5 = PluginConfig(
            {
                "automate": {
                    "curfew_enable": False,  # 重启后宵禁是关闭状态
                    "curfew_start": "00:00",
                    "curfew_end": "23:59",
                    "announce_enable": False,
                }
            }
        )
        auto5 = AutomateHandle(cfg5, db4)

        async def sender5(gid, enable):
            result = await cli4.call_action(
                "set_group_whole_ban", group_id=int(gid), enable=enable
            )
            return not (result.get("status") == "failed" or result.get("retcode") not in (None, 0))

        auto5.bind_sender(sender5, [GID])

        async def provider5():
            return [GID]

        auto5.bind_groups_provider(provider5)

        state5 = asyncio.run(auto5.apply_now())
        assert state5 == "lifted_now", f"重启后没有补解禁: {state5}"
        assert any(
            c[0] == "set_group_whole_ban" and c[1].get("enable") is False for c in cli4.calls
        ), f"重启后未下发解禁: {cli4.calls}"
        assert db4.get_curfew_banned() == [], "解禁成功后记录应清空"
        print("CURFEW_RESTART_LIFT_OK")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("CURFEW_LIFT_REPORTING_OK")


def test_issue_regressions(inst):
    """本轮修复的回归测试：否定误执行、时长被 QQ 号劫持、面板配置语义。

    这些问题的共同点是「CI 全绿但线上行为是错的」——单测只覆盖了解析函数的
    正常输入，没有覆盖真实指令参数与否定语境。
    """
    import asyncio
    import re

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.intent_executor import _as_bool, _as_int
    from astrbot_plugin_panshi.core.local_intent import LocalIntentParser
    from astrbot_plugin_panshi.utils import parse_duration
    from astrbot_plugin_panshi.utils.parser import _DURATION_TOKEN_RE

    # 1) 时长不能被 QQ 号劫持（/禁言 @某人 10m）
    def split_duration(arg: str) -> str:
        cleaned = re.sub(r"\[CQ:at,qq=\d+\]", " ", arg)
        cleaned = re.sub(r"@\S+", " ", cleaned)
        for token in cleaned.split():
            if _DURATION_TOKEN_RE.fullmatch(token) or token.startswith(("无限", "永久")):
                return token
        return ""

    cases = {
        "[CQ:at,qq=123456789] 10m": 600,
        "[CQ:at,qq=123456789] 2h": 7200,
        "123456789 10m": 600,
        "[CQ:at,qq=123456789] 1d": 86400,
        "[CQ:at,qq=123456789] 600": 600,
    }
    for arg, want in cases.items():
        tok = split_duration(arg)
        got = parse_duration(tok, 60) if tok else 60
        assert got == want, f"arg={arg!r} token={tok!r} 得到 {got}s，期望 {want}s"
    # 5-12 位 QQ 号本身绝不能被当成秒数
    assert parse_duration("123456789", 60) == 60
    assert parse_duration("[CQ:at,qq=123456789] 10m", 60) == 600
    assert parse_duration("1.5h", 60) == 5400, "小数时长仍应正确"
    print("DURATION_NO_HIJACK_OK")

    # 2) 否定指令绝不能被解析成执行动作
    cfg = PluginConfig({"basic": {"default_ban_time": 60}})
    parser = LocalIntentParser(cfg)

    class _At:
        def __init__(self, qq):
            self.qq = qq

    class _Ev:
        message_str = ""

        def __init__(self, text, ats=()):
            self.message_str = text
            self._ats = [_At(q) for q in ats]

        def get_group_id(self):
            return 123456

        def get_sender_id(self):
            return 999

        def get_self_id(self):
            return 888

        def get_messages(self):
            return self._ats

    for text, ats in (
        ("别禁言张三", ()),
        ("不用禁言@张三", ("10001",)),
        ("别禁言他", ()),
        ("算了别踢了", ()),
        ("不要拉黑", ()),
    ):
        got = parser.parse(_Ev(text, ats), text)
        assert got is None, f"否定指令 {text!r} 竟被解析成 {got!r}"
    print("NEGATION_GUARD_OK")

    # 2b) 全体禁言的「关闭」语序（用户实测报障）
    #     日志：管理员说「关闭全体禁言」，机器人回「🔇 已开启全体禁言」，
    #     连说三次三次都被开启。
    #     根因：_UNBAN_HINTS 里没有「关闭」，句子落到兜底分支命中「全体禁言」
    #     → enable=True。修法是按「否定动词紧邻关键词」判定，
    #     而不是往全局词表里塞「关闭」——后者会把「关闭宵禁」也误判成解除全体禁言。
    for text, want in (
        ("关闭全体禁言", False),
        ("关闭全体禁言。", False),
        ("关掉全体禁言", False),
        ("把全体禁言关掉", False),
        ("全体禁言关掉", False),
        ("停止全体禁言", False),
        ("停用全体禁言", False),
        ("取消全体禁言", False),
        ("全体禁言取消", False),
        ("解除全体禁言", False),
        ("关闭全员禁言", False),
        ("关闭全禁", False),
        # 开启侧不能回归
        ("全体禁言", True),
        ("开启全体禁言", True),
        ("打开全体禁言", True),
        ("全员禁言", True),
        ("全禁", True),
        ("把全体都禁言了", True),
    ):
        got = parser.parse(_Ev(text, ()), text)
        assert got is not None, f"{text!r} 竟解析不出意图"
        assert got.get("action") == "whole_ban", f"{text!r} -> {got!r}"
        assert got.get("enable") is want, f"{text!r} 期望 enable={want}，实际 {got!r}"
    print("WHOLE_BAN_NEGATION_OK")

    # 2b-2) 端到端：不只解析对，**下发参数与回复文案**也要对。
    #       用户看到的是「🔇 已开启全体禁言」，所以必须断言回复文案——
    #       只断言 enable 会漏掉「解析对了但回复写反」这类问题。
    class _WholeBanNormal:
        def __init__(self, cfg):
            from astrbot_plugin_panshi.core.normal import NormalHandle

            self._h = NormalHandle(cfg, None)

        async def whole_ban(self, event, enable):
            return await self._h.whole_ban(event, enable)

    class _Cli:
        def __init__(self):
            self.calls = []

        async def __call__(self, action, **params):
            self.calls.append((action, params))
            return {"status": "ok", "retcode": 0}

    class _BanBot:
        def __init__(self, cli):
            self._cli = cli

        def __getattr__(self, name):
            async def _m(**kw):
                return await self._cli(name, **kw)

            return _m

    class _BanEv:
        def __init__(self, text, cli):
            self.message_str = text
            self.bot = _BanBot(cli)

        def get_group_id(self):
            return 1077250302

        def get_sender_id(self):
            return 2226175932

        def get_self_id(self):
            return 3823105457

        def get_messages(self):
            return []

        def stop_event(self):
            pass

    for text, want_enable, want_word in (
        ("关闭全体禁言", False, "已解除"),
        ("把全体禁言关掉", False, "已解除"),
        ("全体禁言", True, "已开启"),
    ):
        from astrbot_plugin_panshi.core.intent_executor import IntentExecutor

        cfg2 = PluginConfig({"basic": {"default_ban_time": 60}})
        p2 = LocalIntentParser(cfg2)
        cli = _Cli()
        ev = _BanEv(text, cli)
        it = p2.parse(ev, text)
        assert it is not None, f"{text!r} 解析失败"
        ex = IntentExecutor(cfg2, None, None, _WholeBanNormal(cfg2), None)
        reply = asyncio.run(ex.execute(ev, it))
        api_enable = next(
            (pr.get("enable") for a, pr in cli.calls if a == "set_group_whole_ban"), None
        )
        assert it.get("enable") is want_enable, f"{text!r} 解析 enable 错: {it!r}"
        assert api_enable is want_enable, f"{text!r} 下发 enable 错: {api_enable}"
        assert want_word in (reply or ""), f"{text!r} 回复文案错: {reply!r}"
    print("WHOLE_BAN_E2E_OK")

    # 2c) 宵禁不能被「全体禁言」抢走（「关闭」同时能修饰两者，必须分开路由）
    for text, want_enable in (
        ("关闭宵禁", False),
        ("关闭夜间禁言", False),
        ("开启宵禁", True),
    ):
        got = parser.parse(_Ev(text, ()), text)
        assert got is not None, f"{text!r} 竟解析不出意图"
        assert got.get("action") == "set_curfew", f"{text!r} 被误判成 {got!r}"
        assert got.get("enable") is want_enable, f"{text!r} -> {got!r}"
    print("CURFEW_ROUTING_OK")

    # 2d) 开关方向总检：任何「关闭/取消/停止/别开 X」都**不得**产生 enable=True。
    #     这类方向性错误在群里表现为"我让它关，它给我开"，观感极差，
    #     所以在自测里做一次穷举兜底。
    for text in (
        "关闭全体禁言", "关闭宵禁", "取消全体禁言", "停止全体禁言",
        "关掉全体禁言", "停用全体禁言", "别开全体禁言", "别开启全体禁言",
        "把全体禁言关掉", "全体禁言关掉",
    ):
        got = parser.parse(_Ev(text, ()), text) or {}
        assert got.get("enable") is not True, (
            f"方向性错误：{text!r} 被判成开启 -> {got!r}"
        )
    # 开启侧仍必须为 True（别把功能一起修死）
    for text in ("全体禁言", "开启全体禁言", "打开全体禁言", "开启宵禁"):
        got = parser.parse(_Ev(text, ()), text) or {}
        assert got.get("enable") is True, f"{text!r} 开启侧失效 -> {got!r}"
    print("TOGGLE_DIRECTION_OK")

    # 3) 正常指令仍要能解析（否定守卫不能误伤）
    got = parser.parse(_Ev("禁言@10001 10分钟", ("10001",)), "禁言@10001 10分钟")
    assert got and got.get("action") == "ban", got
    assert str(got.get("target")) == "10001", f"目标解析错误: {got}"
    assert got.get("duration") == 600, f"@ 后面的时长被吃掉了: {got}"
    # 没有引用的「@昵称 10分钟」找不到目标时应放弃，但时长解析本身要正确
    assert parser._extract_duration_text("禁言@张三 10分钟".replace("@张三", " ")) != ""
    assert parse_duration(parser._extract_duration_text("禁言 10分钟"), 60) == 600
    got2 = parser.parse(_Ev("全体禁言"), "全体禁言")
    assert got2 and got2.get("action") == "whole_ban" and got2.get("enable") is True, got2
    got3 = parser.parse(_Ev("解除全体禁言"), "解除全体禁言")
    assert got3 and got3.get("action") == "whole_ban" and got3.get("enable") is False, got3
    # 「永久禁言」不应退化成默认 60 秒
    got4 = parser.parse(_Ev("永久禁言@10001", ("10001",)), "永久禁言@10001")
    assert got4 and got4.get("duration") == 2592000, f"永久禁言时长错误: {got4}"
    print("LOCAL_PARSE_STILL_OK")

    # 4) 「别人在刷屏」不能被否定词误伤（裸「别」不再是否定）
    from astrbot_plugin_panshi.core.intent_gate import IntentGate

    gate = IntentGate(cfg, None)
    allowed, reason = gate.allow("123456", "别人一直在刷屏，管一下")
    assert reason != "否定语境", f"「别人」被误判成否定语境: {reason}"
    assert allowed, f"真实的举报被拦截: {reason}"
    blocked, reason2 = gate.allow("123456", "别禁言张三")
    assert not blocked and reason2 == "否定语境", f"否定指令未被拦截: {reason2}"
    print("NEGATION_FALSE_POSITIVE_OK")

    # 5) 模型给的 "false" 不能变成 True（否则「关闭全体禁言」会执行成开启）
    assert _as_bool("false", True) is False
    assert _as_bool("关闭", True) is False
    assert _as_bool(False, True) is False
    assert _as_bool("true", False) is True
    assert _as_int("3条", 1) == 3, "「3条」应能取出 3"
    assert _as_int(None, 30) == 30
    print("LLM_SCALAR_COERCION_OK")

    # 6) danger: 确认「关闭宵禁」在真实 IntentExecutor 上不会写成开启
    from astrbot_plugin_panshi.core.intent_executor import IntentExecutor

    class _Normal:
        async def whole_ban(self, event, enable):
            return f"whole_ban={enable}"

    ex = IntentExecutor(cfg, inst.db, None, _Normal(), None, None)
    out = asyncio.run(ex._dispatch(_Ev("x"), {"action": "whole_ban", "enable": "false"}, None))
    assert out == "whole_ban=False", f"字符串 false 被当成 True: {out}"
    print("WHOLE_BAN_STRING_FALSE_OK")


def test_banword_and_backup(inst):
    """违禁词自然语言维护 + 面板配置导出/导入回路。"""
    import asyncio

    from astrbot_plugin_panshi.pages_service import PageService

    ex = inst.executor

    # _banword 现在需要事件（用于按群视角读词表）；给一个最小事件即可。
    class _Ev:
        def get_group_id(self):
            return 123456

    ev = _Ev()

    # 1) 添加违禁词
    r = ex._banword(ev, "测试违禁ABC", add=True)
    assert "已添加" in r, r
    words = inst.cfg.get("guard", "forbidden_words", []) or []
    assert "测试违禁ABC" in words, words

    # 2) 重复添加
    r2 = ex._banword(ev, "测试违禁ABC", add=True)
    assert "存在" in r2, r2

    # 3) 模糊匹配删除（输入词是已有词的子串）
    r3 = ex._banword(ev, "测试违禁", add=False)
    assert "已删除" in r3 and "测试违禁ABC" in r3, r3
    words = inst.cfg.get("guard", "forbidden_words", []) or []
    assert "测试违禁ABC" not in words, words

    # 4) 空词与不存在的词
    assert "请告诉我" in ex._banword(ev, "  ", add=True)
    assert "没有" in ex._banword(ev, "根本不存在XYZ", add=False)
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

    # 回归：把某个字段「改回全局值」必须真的改回去。
    # 前端不会提交与全局相同的字段，旧实现只遍历 payload 里出现的分组，
    # 于是 diff 为空 → 分组整体缺失 → 旧覆盖永远清不掉，用户以为改回去了，
    # 运行时却还在用旧值（面板提示「已保存」但行为没变）。
    svc.update_global_config({"guard": {"spam_count": 5}})
    svc.update_group_config("123456", {"follow_default": False, "guard": {"spam_count": 9}})
    assert svc.get_group_config("123456")["effective"]["guard"]["spam_count"] == 9
    # 用户把 9 改回全局的 5 -> 前端只发 follow_default（没有 guard 分组）
    after_revert = svc.update_group_config("123456", {"follow_default": False})
    assert (after_revert["override"] or {}).get("guard", {}) == {}, (
        f"改回全局值后仍残留覆盖: {after_revert['override']}"
    )
    assert after_revert["effective"]["guard"]["spam_count"] == 5, (
        f"改回全局值后实际仍用旧值: {after_revert['effective']['guard']}"
    )
    # 运行时视图也必须跟着回到全局值
    assert inst.cfg.for_group("123456").guard.get("spam_count") == 5, (
        "运行时 for_group 仍在用已删除的覆盖值"
    )
    print("GROUP_REVERT_TO_GLOBAL_OK")

    # 回归：老备份里缺 follow_default 时，面板与运行时的判断必须一致。
    # panel 的 get_group_config 读作「跟随全局」，而 for_group() 读作「应用覆盖」，
    # 两边默认值相反时会出现「面板显示跟随全局、实际按独立配置执行」的分裂。
    svc.reset_group_config("123456")
    legacy = {
        "plugin": "astrbot_plugin_panshi",
        "global_config": {"guard": {"spam_count": 5}},
        "groups": {"123456": {"guard": {"spam_count": 9}}},  # 老备份：没有 follow_default
    }
    svc.import_all(legacy)
    ov_after = inst.db.get_group_override("123456")
    assert ov_after.get("follow_default") is False, (
        f"导入老备份后应显式标记为独立配置: {ov_after}"
    )
    panel_view = svc.get_group_config("123456")["follow_default"]
    runtime_view = inst.cfg.for_group("123456").guard.get("spam_count")
    assert panel_view is False, f"面板显示「跟随全局」而运行时按覆盖执行: {panel_view}"
    assert runtime_view == 9, runtime_view
    print("FOLLOW_DEFAULT_CONSISTENT_OK")
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
