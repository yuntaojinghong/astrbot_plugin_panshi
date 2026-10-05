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
    #
    # 注册的是**通配路径**，靠 URL 后缀认端点。
    #
    # 这一处我来回错过三次，每次都是面板全部接口失效（「未找到该路由」）：
    #   1. 只注册 /{插件名}/{子路径}
    #   2. 只注册 /{子路径}
    #   3. 两种都注册
    # 都不对，因为真实 URL 里插件名可能出现 0~2 次 —— 用户实测的那条是：
    #   /api/v1/plugins/extensions/astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items
    #                             └────────── plugin_path 参数（整条）──────────┘
    # 前缀层数不可控，所以改成 /<path:rest> 匹配一切，用后缀定位端点。
    raw_paths = [r[0] for r in ctx.routes]
    assert raw_paths, "一条路由都没注册"
    for path in raw_paths:
        assert path.startswith("/"), f"路由应以 / 开头: {path}"
    wildcard = [p for p in raw_paths if "<path:" in p]
    assert wildcard, f"应当注册通配路由，实际 {raw_paths}"
    print(f"WEB_ROUTES_OK ({len(raw_paths)} 条通配路由，按后缀识别端点)")

    # 端点表与真实处理函数必须对得上——写错名字会在运行时才炸
    from astrbot_plugin_panshi.pages_api import PanshiWebController
    table = inst.web._endpoint_table()
    for ep, handlers in table.items():
        assert handlers, f"端点 {ep} 没有登记任何方法"
        for method, fname in handlers.items():
            assert method in ("GET", "POST", "DELETE", "PUT", "PATCH"), (ep, method)
            assert hasattr(PanshiWebController, fname), \
                f"端点 {ep} 的 {method} 指向不存在的处理函数 {fname}"
    print(f"WEB_ENDPOINTS_OK ({len(table)} 个端点都指向真实处理函数)")

    # 前端真正会调的「端点 + 方法」，必须都在端点表里登记过。
    #
    # 回归背景（v1.9.13 线上）：端点表早期写成「一个处理函数 + 允许的方法
    # 集合」，`global` / `group` 只登记了 GET。而面板「保存配置」走的是
    # POST /global，于是被自己的路由判断挡下，界面弹：
    #     「global 不接受 POST（允许 GET）」
    # 页面能打开、配置读得出来，就是**存不进去**——不点保存根本发现不了。
    #
    # 修法是把表改成「端点 -> {方法: 处理函数}」。但光修不够：这种"前端加了
    # 调用、后端忘了登记"的走散必须能自动抓到，所以这里直接拿前端的调用当
    # 需求清单来核对，前后端再也别想悄悄走散。
    import os as _os
    import re as _re

    _pages_dir = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "pages", "settings")
    _calls: set[tuple[str, str]] = set()
    for _fn in ("app.js", "shop.js"):
        _p = _os.path.join(_pages_dir, _fn)
        if not _os.path.exists(_p):
            continue
        with open(_p, encoding="utf-8") as _f:
            _text = _f.read()
        for _m in _re.finditer(r"api(Get|Post)\(\s*[\"']([^\"']+)[\"']", _text):
            _calls.add((_m.group(1).upper(), _m.group(2).split("?")[0]))
    assert _calls, "没从面板前端解析到任何接口调用，正则或路径可能变了"
    _missing = sorted(
        (ep, method) for method, ep in _calls
        if method not in (table.get(ep) or {})
    )
    assert not _missing, (
        "面板前端调用的「端点+方法」后端没登记，运行时会报 405："
        f"{_missing}")
    print(f"WEB_FRONTEND_CONTRACT_OK "
          f"(前端 {len(_calls)} 处调用全部有对应处理函数)")

    # 后缀识别：真实 URL 的三种前缀层数都要能定位到同一个端点
    class _PathReq:
        def __init__(self, path):
            self.path = path

    cases = [
        ("/api/v1/plugins/extensions/astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items",
         "shop/items"),
        ("/api/v1/plugins/extensions/astrbot_plugin_panshi/shop/items", "shop/items"),
        ("/plugins/extensions/shop/items", "shop/items"),
        ("/x/y/astrbot_plugin_panshi/global", "global"),
        ("/x/y/astrbot_plugin_panshi/shop/save-items", "shop/save-items"),
        ("/x/y/astrbot_plugin_panshi/groups/refresh", "groups/refresh"),
    ]
    for url, want in cases:
        got = inst.web._tail_of(_PathReq(url))
        assert got == want, f"{url} 应当识别为 {want}，实际 {got}"
    print("WEB_TAIL_MATCH_OK (URL 前缀层数不同也能定位到同一端点)")

    # 通配参数是**关键字参数**传给处理函数的，签名必须收得下。
    #
    # 回归背景：线上真实报错是
    #   TypeError: PanshiWebController.api_dispatch() got an unexpected
    #              keyword argument 'rest'
    # 因为 AstrBot 用 /<path:rest> 注册时，会以 kwarg 形式把解出的通配内容
    # 传给处理函数。这个调用约定本地看不到，只能靠签名兜住。
    import inspect as _inspect

    sig = _inspect.signature(inst.web.api_dispatch)
    params = sig.parameters
    accepts_kwargs = any(p.kind is _inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
    assert "rest" in params or accepts_kwargs, (
        f"api_dispatch 必须收得下通配参数（rest），当前签名 {sig}")
    print("WEB_DISPATCH_SIGNATURE_OK (能接住框架传来的通配参数)")

    # 真的按框架的方式调一次：只传关键字参数
    async def _call_dispatch_as_framework_does():
        controller = inst.web
        # 用一个最小的假请求，避免依赖真实 HTTP 环境
        class _Req:
            path = "/api/v1/plugins/extensions/" \
                   "astrbot_plugin_panshi/astrbot_plugin_panshi/shop/items"
            method = "GET"
            path_params = {}
            _request = None

            async def json(self, default=None):
                return {}

            def get(self, key, default=None):
                return default

        import astrbot_plugin_panshi.pages_api as _pa
        saved = _pa.request
        _pa.request = _Req()          # type: ignore[assignment]
        try:
            # 关键：rest 只以**关键字**传，和框架行为一致
            return await controller.api_dispatch(
                rest="astrbot_plugin_panshi/shop/items")
        finally:
            _pa.request = saved       # type: ignore[assignment]

    import asyncio as _asyncio
    try:
        res = _asyncio.run(_call_dispatch_as_framework_does())
        # 只要不是「未知接口」就说明后缀被正确识别成 shop/items
        assert not (isinstance(res, dict) and res.get("status") == "error"), res
        print("WEB_DISPATCH_KWARG_OK (按框架的方式用关键字传参能正常分派)")
    except TypeError as e:
        raise AssertionError(
            f"按框架的调用方式会失败：{e}\n"
            f"说明 api_dispatch 的签名接不住通配参数——线上就是这个报错。")

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
    test_group_role_and_evict()
    test_errors()
    test_role_precheck()
    test_guard_punish_reports_failure()
    test_bare_word_shortcuts()
    test_checkin_respects_switch()
    test_points_shared_across_groups()
    test_games()
    test_points_earning_ways()
    test_points_natural_language()
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
    test_group_role_permission()
    test_autonomous_enforcement()
    test_self_defense()
    test_welcome_self_and_at()
    test_shop_logic()
    test_shop_probability()
    test_points_switch()
    test_simple_points_commands()
    test_schema_loadable()
    test_schema_renders_in_ui()
    test_shop_editing()

    print("ALL_SELFTEST_PASS")


def test_schema_renders_in_ui():
    """配置页里不能出现 [object Object]。

    **回归背景（v1.9.1 线上）**：`shop.items` / `lottery.prizes` /
    `shop.penalties` 的默认值我写成了**对象数组**：

        "default": [{"id": "tea", "name": "奶茶", "cost": 50}, ...]

    AstrBot 原生配置页把 list 的 default 逐项转成字符串显示，
    每一项就变成字面量 ``[object Object]``；几个字段叠起来，
    用户看到的是一屏看不懂的方块。

    另外 ``type: object`` 且没有 ``items`` 子结构的字段也会被直接字符串化 ——
    `lottery` 就是这种情况，同样显示成 ``[object Object]``。

    这两点和「合法 JSON」「合规 schema」都不冲突，所以之前那两条校验
    都放行了。这条专门盯**渲染结果**：

    * default 不得是 dict，也不得是含 dict/list 的 list
    * type=object 必须有 items（否则会被字符串化）
    """
    import json as _json
    import os as _os

    repo = _os.path.dirname(_os.path.abspath(__file__))
    with open(_os.path.join(repo, "_conf_schema.json"), encoding="utf-8") as f:
        schema = _json.load(f)

    problems: list[str] = []

    def walk(node, path: str) -> None:
        if not isinstance(node, dict):
            return
        t = node.get("type")
        d = node.get("default")

        if isinstance(d, dict):
            problems.append(
                f"{path}: default 是对象 —— 配置页会显示成 [object Object]")
        elif isinstance(d, list) and any(isinstance(x, (dict, list)) for x in d):
            problems.append(
                f"{path}: default 是对象数组 —— 配置页会逐项显示成 "
                f"[object Object]（共 {len(d)} 项）")

        if t == "object":
            if "items" not in node:
                problems.append(
                    f"{path}: type=object 没有 items —— 会被整个字符串化成 "
                    f"[object Object]")
            else:
                for k, v in node["items"].items():
                    walk(v, f"{path}.{k}" if path else k)
        else:
            for k, v in node.items():
                if k in ("items", "default", "slider", "options", "templates"):
                    continue
                if isinstance(v, dict) and ("type" in v or "default" in v):
                    walk(v, f"{path}.{k}" if path else k)

    walk(schema, "")

    assert not problems, (
        "这些配置项在 AstrBot 配置页里会显示成 [object Object]：\n  "
        + "\n  ".join(problems)
        + "\n\n内容型字段请把 default 留空（string），示例 JSON 写进 hint；"
          "复杂结构改由插件自己的面板页或指令填写。")
    print("SCHEMA_RENDERS_OK (没有 default 是对象/对象数组的配置项)")

    # 抽奖字段原先被摊平在 shop 组里。现在整组删掉了（设置搬到了面板的
    # 两个页面），所以这里只断言"没有再冒出对象嵌套"——
    # 渲染层的那类问题由上面的 SCHEMA_RENDERS_OK 覆盖全 schema。
    assert "shop" not in schema, "shop 组应当已删除"
    print("SCHEMA_FLAT_LOTTERY_OK (shop 组已整体移除，不再有 object 嵌套)")

    # 摊平后的键名必须真的被读取——只改 schema 不改代码的话，
    # 用户在配置页填了抽奖设置却完全不生效，而且不会有任何报错。
    from astrbot_plugin_panshi.core.shop import parse_config

    flat, _ = parse_config({
        "enable": True,
        "lottery_enable": True,
        "lottery_cost": 25,
        "lottery_daily_limit": 7,
        "lottery_pity": 33,
        "lottery_prizes": [{"name": "甲", "weight": 0.5}],
    })
    assert flat.lottery.enable is True, flat.lottery
    assert flat.lottery.cost == 25, flat.lottery
    assert flat.lottery.daily_limit == 7, flat.lottery
    assert flat.lottery.pity == 33, flat.lottery
    assert [p.name for p in flat.lottery.prizes] == ["甲"], flat.lottery.prizes
    print("SCHEMA_FLAT_KEYS_OK (摊平后的 lottery_* 键真的生效)")

    # 旧嵌套写法仍要认（已写过这种配置的用户不能被弄坏）
    nested, _ = parse_config({
        "enable": True,
        "lottery": {"enable": True, "cost": 25, "daily_limit": 7,
                    "pity": 33, "prizes": [{"name": "甲", "weight": 0.5}]},
    })
    assert nested.lottery.enable is True, nested.lottery
    assert nested.lottery.cost == 25, nested.lottery
    assert nested.lottery.daily_limit == 7, nested.lottery
    assert nested.lottery.pity == 33, nested.lottery
    assert [p.name for p in nested.lottery.prizes] == ["甲"]
    print("SCHEMA_NESTED_KEYS_OK (旧的 lottery:{...} 嵌套写法仍兼容)")

    # 空字符串（新的默认值）不能被当成坏数据
    empty, _ = parse_config({"items": "", "lottery_prizes": "",
                            "penalties": ""})
    assert empty.items == [], empty.items
    assert empty.lottery.prizes == [], empty.lottery.prizes
    assert empty.lottery.cost == 10, empty.lottery
    print("SCHEMA_EMPTY_STR_OK (默认的空字符串按「未配置」处理，不报错)")

    # hint 里不能有 HTML —— 配置页不渲染标签，用户会看到满屏 <code> <br>。
    # 这个坑我踩过：上一版在 hint 里写了标签，线上就是这样显示的。
    raw = _json.dumps(schema, ensure_ascii=False)
    tags = [t for t in ("<br>", "<code>", "</code>", "<b>", "</b>",
                        "<i>", "</i>", "<p>", "</p>", "<a ") if t in raw]
    assert not tags, f"schema 里有 HTML 标签，配置页只会原样显示：{tags}"
    print("SCHEMA_NO_HTML_OK (说明文字是纯文本，不会显示成标签原文)")

    # 说明要短——用户明确说过「不要留者还写一大段话」。
    # shop 组已经删了，这里检查剩下来的所有分组。
    _long = []
    for _gkey, _gnode in schema.items():
        for _fkey, _fnode in (_gnode.get("items") or {}).items():
            _hint = (_fnode or {}).get("hint") or ""
            if len(_hint) > 120:
                _long.append(f"{_gkey}.{_fkey}（{len(_hint)} 字）")
    assert not _long, f"这些说明太长，配置页显示不下：{_long}"
    print("SCHEMA_HINT_SHORT_OK (所有字段说明都在 120 字以内)")

    # 升级后，用户配置里遗留的对象数组要被清掉。
    # 不清的话，schema 改了也没用——配置页读的是配置里的**实际值**，
    # 照旧显示成一串 [object Object]（截图里就是这样）。
    import types as _types
    from astrbot_plugin_panshi.main import PanshiPlugin

    class _Cfg(dict):
        saved = 0

        def save_config(self, *a, **k):
            type(self).saved += 1

    fake = PanshiPlugin.__new__(PanshiPlugin)     # 不走 __init__，只测这个方法
    fake.cfg = _Cfg({
        "basic": {"default_ban_time": 60},
        "shop": {
            "enable": True,
            "items": [{"name": "旧商品", "cost": 10}],      # 遗留对象数组
            "penalties": [{"reason": "刷屏", "points": 20}],
            "lottery": {"prizes": [{"name": "旧奖品"}]},
            "lottery_cost": 30,
        },
    })
    fake._clean_legacy_shop_config()
    sh = fake.cfg["shop"]
    assert sh["items"] == "", sh["items"]
    assert sh["penalties"] == "", sh["penalties"]
    assert sh["lottery"]["prizes"] == "", sh["lottery"]
    # 不相关的配置一个字都不能动
    assert sh["lottery_cost"] == 30, sh["lottery_cost"]
    assert sh["enable"] is True, sh["enable"]
    assert fake.cfg["basic"]["default_ban_time"] == 60
    assert _Cfg.saved >= 1, "清理后应当写盘，否则重启又出现"
    print("SCHEMA_LEGACY_CLEANUP_OK (遗留对象数组被清空；其它配置不动)")

    # 已经是空字符串时不该反复写盘
    before = _Cfg.saved
    fake._clean_legacy_shop_config()
    assert _Cfg.saved == before, "没有需要清理的内容时不该写盘"
    print("SCHEMA_LEGACY_IDEMPOTENT_OK (无遗留内容时不写盘)")


def test_shop_editing():
    """面板里可视化增删商品/奖品（对应「加号 + 三个框」那套界面）。

    验证三件事：
      1. 编辑后**以编辑结果为准**（不是被配置里的默认值覆盖回去）
      2. 保存时的校验（名字必填、概率总和不得超 1）
      3. 奖品设了「可中次数」后，抽完就不再出
    """
    import asyncio
    import os
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.shop_handle import ShopHandle
    from astrbot_plugin_panshi.data import Storage

    GID, UID = "1077250302", "2226175932"

    class _Ev:
        def __init__(self):
            self.message_obj = types.SimpleNamespace(raw_message={})
            self.message_str = ""

        def get_group_id(self):
            return GID

        def get_sender_id(self):
            return UID

        def get_self_id(self):
            return "3823105457"

        def get_sender_name(self):
            return "小明"

        def get_messages(self):
            return []

        def is_message_str(self):
            return ""

    tmp = tempfile.mkdtemp(prefix="panshi_edit_")
    cfg = PluginConfig({
        "basic": {"default_ban_time": 60},
        "shop": {
            "enable": True,
            "items": [{"id": "a", "name": "配置里的商品", "cost": 10}],
            "lottery": {
                "enable": True, "cost": 0, "daily_limit": 0, "pity": 0,
                "prizes": [{"id": "p1", "name": "配置里的奖品", "weight": 0.5,
                            "reward": "points", "value": "1"}],
            },
        },
    })
    db = Storage(os.path.join(tmp, "d.json"))
    shop = ShopHandle(cfg, db)
    ev = _Ev()

    # ---------- 1. 默认来自配置 ----------
    names = [i["name"] for i in shop.editable_items()]
    assert names == ["配置里的商品"], names
    assert [p["name"] for p in shop.editable_prizes()] == ["配置里的奖品"]
    print("SHOP_EDIT_DEFAULT_OK (未编辑时用配置里的默认值)")

    # ---------- 2. 编辑后以编辑结果为准 ----------
    r = shop.save_items([
        {"name": "奶茶", "cost": 50, "stock": 10},
        {"name": "表情包", "cost": 30, "stock": None},
    ])
    assert r["ok"], r
    items = shop.editable_items()
    assert [i["name"] for i in items] == ["奶茶", "表情包"], items
    assert items[0]["cost"] == 50 and items[0]["stock"] == 10, items[0]
    assert items[1]["stock"] is None, "留空库存应表示不限量"
    assert "配置里的商品" not in [i["name"] for i in items], "编辑后不该再出现配置默认值"
    print("SHOP_EDIT_ITEMS_OK (增/改商品，且以编辑结果为准)")

    # 落盘后仍然生效（换一个实例读同一份数据）
    shop2 = ShopHandle(cfg, Storage(os.path.join(tmp, "d.json")))
    assert [i["name"] for i in shop2.editable_items()] == ["奶茶", "表情包"], \
        shop2.editable_items()
    print("SHOP_EDIT_PERSIST_OK (重启后仍是编辑过的内容)")

    # ---------- 3. 删除 ----------
    r = shop.save_items([{"name": "奶茶", "cost": 50}])
    assert r["ok"] and [i["name"] for i in shop.editable_items()] == ["奶茶"]
    print("SHOP_EDIT_DELETE_OK (删除生效)")

    # ---------- 4. 校验：名字必填 / 价格非负 ----------
    bad = shop.save_items([{"name": "", "cost": 10}])
    assert not bad["ok"] and any("缺少商品名" in p for p in bad["problems"]), bad
    before_names = [i["name"] for i in shop.editable_items()]
    assert before_names == ["奶茶"], "校验失败时不该改动已有数据"

    bad2 = shop.save_items([{"name": "负价格", "cost": -5}])
    assert not bad2["ok"] and any("不能为负" in p for p in bad2["problems"]), bad2
    print("SHOP_EDIT_VALIDATE_OK (名字必填 / 价格非负，且失败不改数据)")

    # ---------- 5. 概率总和不得超过 1 ----------
    ok = shop.save_prizes([
        {"name": "小奖", "chance": 0.3},
        {"name": "大奖", "chance": 0.1, "rare": True},
    ])
    assert ok["ok"], ok
    assert abs(ok["total_chance"] - 0.4) < 1e-9, ok

    over = shop.save_prizes([
        {"name": "A", "chance": 0.7},
        {"name": "B", "chance": 0.5},
    ])
    assert not over["ok"], over
    assert any("超过了 1" in p for p in over["problems"]), over
    # 被拒绝后原奖池不变
    assert [p["name"] for p in shop.editable_prizes()] == ["小奖", "大奖"], \
        shop.editable_prizes()

    single = shop.save_prizes([{"name": "超大", "chance": 1.5}])
    assert not single["ok"] and any("不能大于 1" in p for p in single["problems"])
    print("SHOP_EDIT_CHANCE_OK (合计 >1 被拒；单项 >1 被拒；拒绝不改数据)")

    # ---------- 6. 奖品库存：抽完不再出 ----------
    shop.save_prizes([{"name": "限量奖", "chance": 1.0, "stock": 2,
                       "reward": "points", "value": "10"}])
    db.add_points(GID, UID, 1000)

    async def go():
        got = 0
        for _ in range(5):
            out = await shop.draw(ev)
            if "限量奖" in out:
                got += 1
            elif "没有可抽的奖品" in out or "已抽完" in out:
                pass
        return got

    got = asyncio.run(go())
    assert got == 2, f"库存 2 应当只中 2 次，实际 {got}"
    # 抽完之后再抽应当提示没有可抽的奖品（并退还消耗）
    tail = asyncio.run(shop.draw(ev))
    assert "没有可抽的奖品" in tail or "已抽完" in tail, tail
    print(f"SHOP_EDIT_PRIZE_STOCK_OK (库存 2 只中 2 次；抽完提示无奖品)")

    # ---------- 7. 恢复默认 ----------
    shop.reset_shop_data()
    print("  [dbg] storage.get_prizes() =", db.get_prizes())
    print("  [dbg] storage settings     =", db.get_shop_settings())
    print("  [dbg] cfg shop keys        =", sorted((cfg.shop or {}).keys()))
    print("  [dbg] cfg lottery keys     =", sorted(((cfg.shop or {}).get("lottery") or {}).keys()))
    assert [i["name"] for i in shop.editable_items()] == ["配置里的商品"], \
        shop.editable_items()
    assert [p["name"] for p in shop.editable_prizes()] == ["配置里的奖品"], \
        [p["name"] for p in shop.editable_prizes()]
    print("SHOP_EDIT_RESET_OK (恢复为配置默认值)")

    # ---------- 8. 命令版增删 ----------
    msg = asyncio.run(shop.add_item_from_text("咖啡 60 5"))
    assert "已上架" in msg and "咖啡" in msg, msg
    assert any(i["name"] == "咖啡" for i in shop.editable_items())
    msg2 = asyncio.run(shop.remove_item_by_name("咖啡"))
    assert "已下架" in msg2, msg2
    assert not any(i["name"] == "咖啡" for i in shop.editable_items())

    pmsg = asyncio.run(shop.add_prize_from_text("参与奖 0.2"))
    assert "已加奖品" in pmsg, pmsg
    assert any(p["name"] == "参与奖" for p in shop.editable_prizes())
    dmsg = asyncio.run(shop.remove_prize_by_name("参与奖"))
    assert "已删除" in dmsg, dmsg
    # 参数不对时给用法，而不是静默失败
    assert "用法" in asyncio.run(shop.add_item_from_text("只有名字"))
    assert "用法" in asyncio.run(shop.add_prize_from_text("只有名字"))
    print("SHOP_EDIT_COMMANDS_OK (/上架 /下架 /奖池 /删奖品)")

    # ---------- 9. 指令与入口 ----------
    from astrbot_plugin_panshi.main import PanshiPlugin
    for name in ("cmd_add_item", "cmd_del_item", "cmd_prize",
                 "cmd_del_prize", "cmd_shop", "cmd_buy"):
        assert hasattr(PanshiPlugin, name), f"缺少指令 {name}"
    print("SHOP_EDIT_ENTRYPOINTS_OK (指令就位)")

    # ---------- 10. 开关与参数也能在面板里改 ----------
    #
    # 用户要求：把商城/抽奖的设置从 AstrBot 原生配置页搬进插件面板。
    # 存法与商品/奖池一致——配置值作默认，面板改过以 storage 为准。
    tmp2 = tempfile.mkdtemp(prefix="panshi_set_")
    cfg2 = PluginConfig({
        "basic": {"default_ban_time": 60},
        "shop": {
            "enable": False,                    # 配置里是关的
            "lottery_enable": False,
            "lottery_cost": 10,
            "lottery_daily_limit": 3,
            "lottery_pity": 10,
        },
    })
    db2 = Storage(os.path.join(tmp2, "d.json"))
    sh2 = ShopHandle(cfg2, db2)

    got = sh2.editable_settings()
    assert got == {"enable": False, "lottery_enable": False, "lottery_cost": 10,
                   "lottery_daily_limit": 3, "lottery_pity": 10}, got
    print("SHOP_SETTINGS_DEFAULT_OK (未改时用配置里的值)")

    r = sh2.save_settings({"enable": True, "lottery_enable": True,
                           "lottery_cost": 25, "lottery_daily_limit": 5,
                           "lottery_pity": 20})
    assert r["ok"], r
    assert sh2.editable_settings() == {
        "enable": True, "lottery_enable": True, "lottery_cost": 25,
        "lottery_daily_limit": 5, "lottery_pity": 20}, sh2.editable_settings()
    # 换一个实例读同一份数据，确认落盘了
    sh3 = ShopHandle(cfg2, Storage(os.path.join(tmp2, "d.json")))
    assert sh3.editable_settings()["lottery_cost"] == 25, sh3.editable_settings()
    print("SHOP_SETTINGS_SAVE_OK (开关与参数能改、能落盘)")

    # 非法值要被挡下，且不破坏已有设置
    bad = sh2.save_settings({"lottery_cost": "abc"})
    assert not bad["ok"] and any("整数" in p for p in bad["problems"]), bad
    bad2 = sh2.save_settings({"lottery_pity": -1})
    assert not bad2["ok"], bad2
    bad3 = sh2.save_settings({"lottery_cost": 10 ** 9})
    assert not bad3["ok"], bad3
    assert sh2.editable_settings()["lottery_cost"] == 25, sh2.editable_settings()
    print("SHOP_SETTINGS_VALIDATE_OK (非法值被拒；被拒后原设置不变)")

    # 开关真的影响行为
    class _Ev2:
        def get_group_id(self):
            return "1077250302"

    ev2 = _Ev2()
    assert sh2.points_enabled(ev2) is True, "面板打开后本群应启用积分系统"
    sh2.save_settings({"enable": False})
    assert sh2.points_enabled(ev2) is False, "面板关闭后本群应停用积分系统"
    print("SHOP_SETTINGS_EFFECT_OK (面板开关真的改变行为)")

    # 恢复默认要连设置一起清掉
    sh2.save_settings({"enable": True, "lottery_cost": 99})
    sh2.reset_shop_data()
    assert sh2.editable_settings()["lottery_cost"] == 10, sh2.editable_settings()
    print("SHOP_SETTINGS_RESET_OK (恢复默认会连设置一起回退)")

    # ---------- 11. 首次读取把配置灌进 storage ----------
    #
    # 不灌的话会出现「配置里有 3 件 + 面板加 1 件 → 看到 4 件；
    # 删掉面板那件后配置的 3 件又冒回来」这种自相矛盾的行为。
    tmp3 = tempfile.mkdtemp(prefix="panshi_seed_")
    cfg3 = PluginConfig({"shop": {
        "enable": True,
        "items": [{"name": "配置商品", "cost": 5}],
        "lottery_prizes": [{"name": "配置奖品", "weight": 0.5}],
    }})
    db3 = Storage(os.path.join(tmp3, "d.json"))
    sh4 = ShopHandle(cfg3, db3)
    assert db3.get_shop_items() is None, "读之前不该有数据"
    names = [i["name"] for i in sh4.editable_items()]
    assert names == ["配置商品"], names
    assert db3.get_shop_items() is not None, "首次读取后应已灌入 storage"
    assert [p["name"] for p in sh4.editable_prizes()] == ["配置奖品"]

    # 灌过之后再删掉，配置里的不该冒回来
    sh4.save_items([])
    assert [i["name"] for i in sh4.editable_items()] == [], \
        "删空后配置里的商品又冒回来了"
    print("SHOP_SEED_ONCE_OK (配置只灌一次；删空后不会复活)")

    # ---------- 12. 落盘往返：写进文件、换实例还能读出来 ----------
    #
    # 用户反馈「保存后刷新又回默认值」。本地复现不出来，所以把
    # 「写文件 → 新实例读文件」这条链路钉住：只要它绿，
    # 就说明存储层没问题，问题在别处（路由、版本没生效等）。
    tmp4 = tempfile.mkdtemp(prefix="panshi_disk_")
    db4 = Storage(os.path.join(tmp4, "d.json"))
    db4.set_shop_items([{"name": "奶茶", "cost": 50, "stock": 3}])
    db4.set_shop_settings({"enable": True, "lottery_cost": 25})
    db4.set_prizes([{"name": "谢谢参与", "weight": 0.7}])

    import json as _json3
    assert os.path.exists(db4.file), f"写完之后文件不存在: {db4.file}"
    with open(db4.file, encoding="utf-8") as _f:
        _raw = _json3.load(_f)
    assert _raw["shop"]["items"], _raw.get("shop")
    assert _raw["shop"]["settings"]["lottery_cost"] == 25, _raw["shop"]

    # 换一个实例读同一份文件（等同于插件重载后再读）
    db5 = Storage(os.path.join(tmp4, "d.json"))
    assert [i["name"] for i in (db5.get_shop_items() or [])] == ["奶茶"], \
        db5.get_shop_items()
    assert db5.get_shop_settings().get("lottery_cost") == 25, \
        db5.get_shop_settings()
    assert [p["name"] for p in (db5.get_prizes() or [])] == ["谢谢参与"], \
        db5.get_prizes()

    # 用 ShopHandle 再读一遍，确认面板看到的就是存下的
    sh6 = ShopHandle(cfg3, db5)
    assert [i["name"] for i in sh6.editable_items()] == ["奶茶"], \
        sh6.editable_items()
    assert sh6.editable_settings()["lottery_cost"] == 25, sh6.editable_settings()
    assert sh6.editable_settings()["enable"] is True, sh6.editable_settings()
    print("SHOP_DISK_ROUNDTRIP_OK (写文件 → 新实例读出，值都还在)")

    # 页面上的「保存商品」走的是 save_items -> set_shop_items -> save()，
    # 这里确认面板读出来的和刚存的一致（用户看到的就是这个）
    r = sh6.save_items([{"name": "头像框", "cost": 88, "stock": 1}])
    assert r["ok"], r
    db6 = Storage(os.path.join(tmp4, "d.json"))
    sh7 = ShopHandle(cfg3, db6)
    assert [i["name"] for i in sh7.editable_items()] == ["头像框"], \
        sh7.editable_items()
    print("SHOP_SAVE_THEN_REOPEN_OK (保存后重新打开，商品没有复位)")




def test_schema_loadable():
    """_conf_schema.json 必须能被 AstrBot 的配置解析器吃下去。

    **回归背景（v1.8.3 线上事故）**：`shop.penalties` 被写成
    ``type: object`` 却没给 ``items`` 子结构。AstrBot 的
    ``_parse_schema`` 遇到 object 就递归去找 ``v["items"]``，
    于是 ``KeyError: 'items'``，**整个插件加载失败**：

        Exception: 加载插件「磐石 · 智能群管」时出现问题，原因：'items'

    而当时我的测试**一个都没发现**——因为我只验证了「这是合法 JSON」，
    没验证「AstrBot 能解析它」。合法 JSON 和合规 schema 是两回事。

    AstrBot 的解析规则（读自 astrbot_config._parse_schema 的字节码常量）：

    * ``type`` 不在支持列表里 → TypeError
    * ``type == "object"`` → **必须**有 ``items``，并递归解析每一项
    * ``type == "template_list"`` → 需要 ``templates``
    * 其余类型 → 取 ``default``（缺省用 DEFAULT_VALUE_MAP 的零值）
    * 没有 ``type`` → 不递归，按普通值处理
    """
    import json as _json
    import os as _os

    repo = _os.path.dirname(_os.path.abspath(__file__))
    path = _os.path.join(repo, "_conf_schema.json")
    with open(path, encoding="utf-8") as f:
        schema = _json.load(f)

    # AstrBot 支持的类型（取自 DEFAULT_VALUE_MAP 的键 + 两个结构类型）
    SIMPLE = {"int", "float", "bool", "string", "text", "list", "dict"}
    STRUCT = {"object", "template_list"}
    problems: list[str] = []

    def walk(node, path: str) -> None:
        if not isinstance(node, dict):
            return
        t = node.get("type")

        if t is not None and t not in SIMPLE | STRUCT:
            problems.append(f"{path}: 不支持的 type={t!r}（AstrBot 会抛 TypeError）")

        if t == "object":
            # 这就是线上那个 KeyError
            if "items" not in node:
                problems.append(
                    f"{path}: type=object 但缺少 items —— "
                    f"AstrBot 解析时会 KeyError: 'items'，导致插件加载失败")
            elif not isinstance(node["items"], dict):
                problems.append(f"{path}: items 必须是对象")
            else:
                for k, v in node["items"].items():
                    walk(v, f"{path}.{k}" if path else k)

        if t == "template_list" and "templates" not in node:
            problems.append(f"{path}: type=template_list 但缺少 templates")

        # 递归只沿 items 走：object 之外的类型 AstrBot 不会往下钻
        if t != "object":
            for k, v in node.items():
                if k in ("items", "default", "slider", "options", "templates"):
                    continue
                if isinstance(v, dict) and ("type" in v or "default" in v):
                    walk(v, f"{path}.{k}" if path else k)

    walk(schema, "")

    # 顶层每个分组都应当是 object 且带 items
    for k, v in schema.items():
        if not isinstance(v, dict):
            problems.append(f"顶层 {k} 不是对象")
            continue
        if v.get("type") != "object":
            problems.append(f"顶层 {k} 的 type 应为 object，实际 {v.get('type')!r}")
        elif "items" not in v:
            problems.append(f"顶层 {k} 缺少 items")

    assert not problems, "schema 有问题：\n  " + "\n  ".join(problems)
    print(f"SCHEMA_LOADABLE_OK ({len(schema)} 个顶层分组，无 object 缺 items)")

    # 每个配置项都要有 description，否则面板上是一串看不懂的键名
    no_desc = []

    def check_desc(node, path=""):
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            for k, v in (node.get("items") or {}).items():
                if isinstance(v, dict) and not str(v.get("description") or "").strip():
                    no_desc.append(f"{path}.{k}" if path else k)
                check_desc(v, f"{path}.{k}" if path else k)

    check_desc(schema)
    assert not no_desc, f"这些配置项缺少 description（面板上会显示成裸键名）：{no_desc}"
    print("SCHEMA_DESC_OK (每项都有说明文字)")


def test_simple_points_commands():
    """简化版积分指令：/加分、/扣分、/积分。

    用户要求「简答一些，单发积分、显示我的积分、管理员@人加10就是加10积分」。

    这套指令最大的坑是**把 QQ 号当成积分数量**：
    消息里同时有 @目标的 QQ 号和数量，取错就会「给某人加 22 亿分」。
    所以这里密集覆盖各种写法。
    """
    import asyncio
    import os
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.activity import ActivityHandle
    from astrbot_plugin_panshi.data import Storage
    from astrbot_plugin_panshi.main import PanshiPlugin
    from astrbot_plugin_panshi.utils import parse_amount, strip_amount

    # ---------- 1. 数量解析 ----------
    AMOUNTS = [
        ("@小明 10", 10),
        ("@小明 10 表现好", 10),
        ("2226175932 10", 10),            # QQ 号 + 数量
        ("@小明 2226175932 10", 10),       # 两个数字：QQ 和数量
        ("10", 10),
        ("+50", 50),
        ("-30", -30),
        ("20分", 20),
        ("15积分", 15),
        ("数量=25", 25),
        ("@小明 999999999", None),         # 只有像 QQ 号的数字
    ]
    bad = []
    for text, want in AMOUNTS:
        got, _why = parse_amount(text)
        if got != want:
            bad.append((text, want, got))
    assert not bad, f"数量解析错误: {bad}"
    print(f"AMOUNT_PARSE_OK ({len(AMOUNTS)} 种写法，含 QQ 号干扰)")

    # 默认值：/@加分 @某人 不写数量时按 10
    got, why = parse_amount("@小明", default=10)
    assert got == 10, (got, why)
    # 不传 default 时要求明确给出，不能瞎猜
    got2, _ = parse_amount("@小明")
    assert got2 is None, got2
    # 空文本
    assert parse_amount("", default=10)[0] == 10
    assert parse_amount("")[0] is None
    print("AMOUNT_DEFAULT_OK (默认 10；不传 default 时要求明确)")

    # ---------- 2. 理由剥离：不能吃掉理由里的数字 ----------
    REASONS = [
        ("@小明 10 表现好", "表现好"),
        ("@小明 10 刷屏", "刷屏"),
        ("@某人 5 连刷 3 条", "连刷 3 条"),   # 理由里的 3 要保住
        ("@小明 8 广告刷屏", "广告刷屏"),
        ("@小明 10", ""),
        ("2226175932 10", ""),               # QQ 号不该进理由
    ]
    bad2 = []
    for text, want in REASONS:
        amt, _ = parse_amount(text)
        got = strip_amount(text, amt or 0)
        if got != want:
            bad2.append((text, want, got))
    assert not bad2, f"理由剥离错误: {bad2}"
    print(f"AMOUNT_REASON_OK ({len(REASONS)} 种写法)")

    # ---------- 3. 加减积分的行为 ----------
    GID, ADMIN, TARGET = "1077250302", "3823105457", "2226175932"

    class _Ev:
        def __init__(self):
            self.message_obj = types.SimpleNamespace(raw_message={})
            self.message_str = ""
            self.bot = types.SimpleNamespace()

        def get_group_id(self):
            return GID

        def get_self_id(self):
            return ADMIN

        def get_sender_id(self):
            return TARGET

        def get_sender_name(self):
            return "小明"

        def get_messages(self):
            return []

        def is_stopped(self):
            return False

    tmp = tempfile.mkdtemp(prefix="panshi_pts_")
    cfg = PluginConfig({"basic": {"default_ban_time": 60}})
    db = Storage(os.path.join(tmp, "d.json"))
    act = ActivityHandle(cfg, db)
    ev = _Ev()

    async def go():
        # 加 10
        r = await act.adjust_points(ev, TARGET, 10)
        assert "加 10" in r, r
        assert db.get_points(GID, TARGET) == 10, db.get_points(GID, TARGET)
        # 回执要同时给出变动前后，管理员才能确认改对了
        assert "0 → 10" in r, r

        # 再加 5，带理由
        r2 = await act.adjust_points(ev, TARGET, 5, "表现好")
        assert "15" in r2 and "表现好" in r2, r2
        assert db.get_points(GID, TARGET) == 15

        # 扣 5
        r3 = await act.adjust_points(ev, TARGET, -5)
        assert db.get_points(GID, TARGET) == 10, db.get_points(GID, TARGET)
        assert "10" in r3, r3

        # 扣超：不扣成负数
        r4 = await act.adjust_points(ev, TARGET, -999)
        assert db.get_points(GID, TARGET) == 0, db.get_points(GID, TARGET)
        assert "0" in r4, r4

        # 已经没有积分时再扣
        r5 = await act.adjust_points(ev, TARGET, -10)
        assert "没有可扣" in r5 or "0" in r5, r5
        assert db.get_points(GID, TARGET) == 0

        # 没给目标
        r6 = await act.adjust_points(ev, None, 10)
        assert "不知道要给谁" in r6, r6

        # 数量 0
        r7 = await act.adjust_points(ev, TARGET, 0)
        assert "0" in r7 and "没有变化" in r7, r7

    asyncio.run(go())
    print("ADJUST_POINTS_OK (加/扣/扣不穿/无目标/数量 0)")

    # ---------- 4. 指令存在且别名好记 ----------
    for name, aliases in (
        ("cmd_add_points", ("加分", "加积分", "给分", "奖励")),
        ("cmd_sub_points", ("扣分", "减积分", "罚分")),
        ("cmd_points", ("积分", "查积分")),
    ):
        fn = getattr(PanshiPlugin, name, None)
        assert fn is not None, f"缺少指令 {name}"
        doc = (fn.__doc__ or "")
        assert doc.strip(), f"{name} 缺少说明"
    print("SIMPLE_CMD_OK (/加分 /扣分 /积分 三个指令就位)")

    # ---------- 5. 入口检查：新增能力必须能被指令到达 ----------
    import inspect as _inspect
    main_src = _inspect.getsource(PanshiPlugin)
    for meth in ("adjust_points", "query_points"):
        assert f".{meth}(" in main_src, (
            f"ActivityHandle.{meth} 没有指令入口，用户用不到")
    print("SIMPLE_CMD_ENTRYPOINTS_OK")


def test_shop_probability():
    """奖池概率：总和不得超过 1，不足 1 的部分是「未中奖」。

    用户要求「概率可以自己设置，但总概率不超过 1」。
    这里固定三件事：恰好 1 / 不足 1 / 超过 1 的处理方式。
    """
    from astrbot_plugin_panshi.core.shop import (
        LotteryConfig, can_draw, chance_summary, draw_prize, miss_placeholder,
        parse_prizes,
    )

    # ---------- 1. 恰好合计 1：全部铺满，没有未中奖余量 ----------
    prizes, notes = parse_prizes([
        {"name": "A", "weight": 0.5, "reward": "points", "value": "1"},
        {"name": "B", "weight": 0.3, "reward": "points", "value": "2"},
        {"name": "C", "weight": 0.2, "reward": "points", "value": "3"},
    ])
    assert abs(sum(p.chance for p in prizes) - 1.0) < 1e-9, [p.chance for p in prizes]
    assert not any("未中奖" in n for n in notes), notes
    print("PROB_SUM_ONE_OK (合计 1 → 无未中奖余量)")

    # ---------- 2. 不足 1：余量成为未中奖 ----------
    prizes2, notes2 = parse_prizes([
        {"name": "小奖", "weight": 0.03, "reward": "points", "value": "5"},
        {"name": "大奖", "weight": 0.01, "rare": True, "reward": "points", "value": "100"},
    ])
    assert abs(sum(p.chance for p in prizes2) - 0.04) < 1e-9
    assert any("未中奖" in n for n in notes2), notes2
    print(f"PROB_REMAINDER_OK ({notes2[-1]})")

    # ---------- 3. 超过 1：剔除越界项，保留能用的 ----------
    prizes3, notes3 = parse_prizes([
        {"name": "甲", "weight": 0.6, "reward": "points", "value": "1"},
        {"name": "乙", "weight": 0.6, "reward": "points", "value": "2"},
        {"name": "丙", "weight": 0.1, "reward": "points", "value": "3"},
    ])
    kept = [p.name for p in prizes3 if p.enabled]
    # 累加式保留：甲 0.6 收下；乙会让总和到 1.2 → 剔除；
    # 丙 0.6+0.1=0.7 仍在上限内 → 保留。所以是甲、丙。
    assert kept == ["甲", "丙"], kept
    assert abs(sum(p.chance for p in prizes3) - 0.7) < 1e-9, \
        sum(p.chance for p in prizes3)
    assert any("超过 1" in n for n in notes3), notes3
    print(f"PROB_OVER_ONE_OK ({notes3[-1]})")

    # 负数/非法概率跳过
    prizes4, notes4 = parse_prizes([
        {"name": "正常", "weight": 0.2},
        {"name": "负数", "weight": -0.5},
        {"name": "乱写", "weight": "abc"},
    ])
    assert [p.name for p in prizes4] == ["正常"], [p.name for p in prizes4]
    rejected = [n for n in notes4 if "概率无效" in n]
    assert len(rejected) == 2, notes4

    # 兼容旧写法：都 > 1 时按权重归一化
    prizes5, notes5 = parse_prizes([
        {"name": "常见", "weight": 60},
        {"name": "稀有", "weight": 1},
    ])
    assert abs(sum(p.chance for p in prizes5) - 1.0) < 1e-9, [p.chance for p in prizes5]
    assert prizes5[0].chance > prizes5[1].chance
    assert any("归一化" in n for n in notes5), notes5
    print("PROB_WEIGHT_COMPAT_OK (整数权重自动归一化)")

    # ---------- 4. 概率真的影响结果 ----------
    import random
    lot = LotteryConfig(enable=True, cost=0, pity=0, prizes=prizes2)
    rnd = random.Random(7)
    hits = {"小奖": 0, "大奖": 0}
    misses = 0
    for _ in range(4000):
        p, _ = draw_prize(lot, rng=rnd)
        if p is None:
            continue
        if p.name in hits:
            hits[p.name] += 1
        elif p.reward == "none":
            misses += 1
    # 合计 4%，4000 次期望 ~160 次中奖、~3840 次空手
    total_hits = hits["小奖"] + hits["大奖"]
    assert 80 < total_hits < 260, (hits, misses)
    assert misses > 3500, misses
    assert hits["小奖"] > hits["大奖"], hits
    print(f"PROB_EFFECT_OK (4000 次中 {total_hits} 次中奖 / {misses} 次空手，比例合理)")

    # ---------- 5. 「空手」与「奖池为空」必须区分 ----------
    # 空手 = 概率余量，是正常结果；奖池为空 = 配置错误，要退还消耗
    p_none, _ = draw_prize(LotteryConfig(enable=True, prizes=[]), rng=rnd)
    assert p_none is None, "奖池为空应返回 None（调用方据此退还消耗）"
    assert miss_placeholder().reward == "none"
    assert miss_placeholder().name == "未中奖"
    print("PROB_EMPTY_VS_MISS_OK (奖池为空返回 None；余量空手返回占位奖品)")

    # ---------- 6. 概率全为 0 时视为没配奖品 ----------
    zero = LotteryConfig(enable=True, cost=10, prizes=parse_prizes([
        {"name": "零概率", "weight": 0.0},
    ])[0])
    assert "概率都是 0" in can_draw(zero, points=999, drawn_today=0).reason
    print("PROB_ALL_ZERO_OK (概率全 0 视为未配置，拒绝抽奖)")

    # ---------- 7. 概率清单可读 ----------
    lines = chance_summary(lot)
    assert any("小奖" in x for x in lines), lines
    assert any("合计中奖概率" in x for x in lines), lines
    print(f"PROB_SUMMARY_OK ({lines[-1]})")


def test_points_switch():
    """按群总开关：关掉这个群就不启用积分系统。

    用户要求「积分系统也有一个总开关，关闭此群就不启用」。
    这里验证：默认取全局、按群覆盖生效、关掉后商城/抽奖/扣分全部停。
    """
    import asyncio
    import os
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.guard import GuardHandle
    from astrbot_plugin_panshi.core.shop_handle import ShopHandle
    from astrbot_plugin_panshi.data import Storage
    from astrbot_plugin_panshi.main import PanshiPlugin

    GID, UID = "1077250302", "2226175932"

    class _Ev:
        def __init__(self):
            self.message_obj = types.SimpleNamespace(raw_message={})
            self.message_str = ""

        def get_group_id(self):
            return GID

        def get_sender_id(self):
            return UID

        def get_self_id(self):
            return "3823105457"

        def get_sender_name(self):
            return "小明"

        def get_messages(self):
            return []

        def get_message_str(self):
            return ""

        def is_admin(self):
            return True

        def plain_result(self, t):
            return {"text": t}

    tmp = tempfile.mkdtemp(prefix="panshi_switch_")
    cfg = PluginConfig({
        "basic": {"default_ban_time": 60},
        "shop": {"enable": True, "items": [
            {"id": "x", "name": "测试商品", "cost": 10},
        ], "lottery": {"enable": True, "cost": 10, "daily_limit": 0,
                       "prizes": [{"name": "奖", "weight": 1.0,
                                   "reward": "points", "value": "5"}]},
                 "penalties": {"刷屏": {"points": 20}}},
    })
    db = Storage(os.path.join(tmp, "d.json"))
    # 必须绑定存储：按群覆盖（for_group）靠它读取，不绑定就会静默退化成全局值，
    # 表现就是"按群开关点了没反应"。主程序里是在 __init__ 调 bind_storage。
    cfg.bind_storage(db)
    shop = ShopHandle(cfg, db)
    ev = _Ev()

    # 全局开着 → 本群也是开
    assert shop.points_enabled(ev) is True
    print("SWITCH_GLOBAL_ON_OK")

    async def go():
        # 关掉本群
        msg = await shop.toggle(ev, "off")
        assert "关闭" in msg, msg

        # 现在本群应当关着，但全局仍是开的（不影响别的群）
        assert shop.points_enabled(ev) is False, "按群覆盖没生效"
        other = cfg.for_group("999999")
        assert other.shop.get("enable") is True, "不该改到全局"

        # 商城 / 抽奖 / 记录 都应当拒绝
        for fn, args in ((shop.show_shop, ()), (shop.draw, ()),
                         (shop.my_records, ())):
            r = await fn(ev, *args)
            assert "已关闭" in r, f"{fn.__name__} 未受总开关约束: {r!r}"
        r_buy = await shop.buy(ev, "测试商品")
        assert "已关闭" in r_buy, r_buy

        # 违规扣分也应当停
        guard = GuardHandle(cfg, db)
        db.add_points(GID, UID, 100)
        out = guard._apply_points_penalty(ev, "刷屏", cfg)
        assert out == "", f"关了积分系统却还在扣分: {out!r}"
        assert db.get_points(GID, UID) == 100, db.get_points(GID, UID)

        # 不带参数 = 查询，不应改动状态
        q = await shop.toggle(ev, "")
        assert "关闭" in q and "用法" in q, q
        assert shop.points_enabled(ev) is False, "查询不该改动状态"

        # 再打开
        msg2 = await shop.toggle(ev, "on")
        assert "开启" in msg2, msg2
        assert shop.points_enabled(ev) is True
        r2 = await shop.show_shop(ev)
        assert "已关闭" not in r2, r2

    asyncio.run(go())
    print("SWITCH_PER_GROUP_OK (关闭本群 / 不影响全局 / 商城抽奖扣分全停 / 查询不改状态)")

    # 非法参数不误改
    ev2 = _Ev()
    out = asyncio.run(shop.toggle(ev2, "随便写的"))
    assert "用法" in out, out
    print("SWITCH_BAD_ARG_OK (非法参数只回用法，不改状态)")

    # 插件层面：指令存在且是管理指令
    import inspect
    cmd = getattr(PanshiPlugin, "cmd_points_switch", None)
    assert cmd is not None, "缺少 /积分开关 指令"
    params = list(inspect.signature(cmd).parameters)
    assert "arg" in params, params
    assert getattr(PanshiPlugin, "cmd_chances", None) is not None, "缺少 /抽奖概率 指令"
    print("SWITCH_COMMANDS_OK (/积分开关 + /抽奖概率)")

    # ---------- 每个指令都必须真的"有入口" ----------
    # 回归背景：`ShopHandle.my_records` 写好了、测试也覆盖了，
    # 但**没有任何指令或工具调用它**——购买/抽奖记录因此根本查不到。
    # 光测"函数能跑"发现不了这种问题，必须检查"函数被谁调用"。
    import inspect as _inspect
    from astrbot_plugin_panshi.core.shop_handle import ShopHandle

    src_all = ""
    for _p in ("main.py", "core/guard.py", "core/interact.py",
               "core/intent_executor.py", "core/panel.py"):
        _f = os.path.join(os.path.dirname(os.path.dirname(
            _inspect.getfile(ShopHandle))), _p)
        if os.path.exists(_f):
            with open(_f, encoding="utf-8") as fh:
                src_all += fh.read()

    # 这些是给用户用的功能，必须能从指令或 LLM 工具到达
    for meth in ("show_shop", "buy", "draw", "my_records", "toggle",
                 "show_chances"):
        assert f".{meth}(" in src_all, (
            f"ShopHandle.{meth} 没有任何入口（指令/工具都没调用它），"
            f"用户根本用不到")
    print("SHOP_ENTRYPOINTS_OK (商城/购买/抽奖/记录/开关/概率 都有指令入口)")


def test_shop_logic():
    """积分商城 / 抽奖 / 违规扣分的规则层。

    这一层是纯计算，所以可以密集覆盖边界：
    库存售罄、限购、积分不足、权重分布、保底、扣分不下穿到负数。
    """
    import random

    from astrbot_plugin_panshi.core.shop import (
        apply_points_floor, can_buy, can_draw, draw_prize, find_item,
        listable_items, parse_config, parse_penalties, render_reward,
        reward_points,
    )

    # ---------------- 1. 解析配置：坏条目跳过而不是整体失败 ----------------
    cfg, notes = parse_config({
        "enable": True,
        "items": [
            {"id": "a", "name": "头衔", "cost": 100, "reward": "title", "value": "大佬"},
            {"name": "没写价格"},                       # 缺 cost -> 跳过
            {"name": "负价格", "cost": -5},              # 负数 -> 跳过
            "不是对象",                                  # 类型错 -> 跳过
            {"name": "下架的", "cost": 10, "enabled": False},
            {"name": "库存写错", "cost": 10, "stock": "abc"},  # 库存无效 -> 当不限量
        ],
        "lottery": {
            "enable": True, "cost": 10, "daily_limit": 3, "pity": 5,
            "prizes": [
                {"id": "p1", "name": "谢谢参与", "weight": 0.7, "reward": "points", "value": "1"},
                {"id": "p2", "name": "稀有", "weight": 0.01, "rare": True,
                 "reward": "points", "value": "100"},
            ],
        },
    })
    assert cfg.enable is True
    names = [i.name for i in cfg.items]
    assert "头衔" in names and "下架的" in names, names
    assert "没写价格" not in names and "负价格" not in names, names
    assert len(cfg.items) == 3, names
    # 库存写错时退化为不限量（比"卖不出去"友好）
    assert cfg.items[2].stock is None, cfg.items[2]
    assert any("价格无效" in n for n in notes), notes
    print(f"SHOP_PARSE_OK (跳过 {len(notes)} 条坏配置，其余可用)")

    # 上架过滤
    assert [i.name for i in listable_items(cfg)] == ["头衔", "库存写错"], \
        [i.name for i in listable_items(cfg)]

    # 按 id / 名字都能找到
    assert find_item(cfg, "头衔") is not None
    assert find_item(cfg, "a") is not None
    assert find_item(cfg, "不存在") is None

    # ---------------- 2. 购买判定 ----------------
    item = find_item(cfg, "头衔")

    assert can_buy(item, points=100, stock=5, bought_total=0, bought_today=0).ok
    assert not can_buy(item, points=99, stock=5, bought_total=0, bought_today=0).ok
    assert "积分不足" in can_buy(item, points=0, stock=5,
                                bought_total=0, bought_today=0).reason
    assert "售罄" in can_buy(item, points=999, stock=0,
                            bought_total=0, bought_today=0).reason

    limited = find_item(cfg, "下架的")
    # 限购
    from astrbot_plugin_panshi.core.shop import ShopItem
    li = ShopItem(item_id="x", name="限购品", cost=10, limit_per_user=2, limit_per_day=1)
    assert can_buy(li, points=999, stock=None, bought_total=0, bought_today=0).ok
    assert "限购上限" in can_buy(li, points=999, stock=None,
                                bought_total=2, bought_today=0).reason
    assert "今天" in can_buy(li, points=999, stock=None,
                            bought_total=1, bought_today=1).reason
    # 下架
    assert "下架" in can_buy(limited, points=999, stock=None,
                            bought_total=0, bought_today=0).reason
    print("SHOP_BUY_CHECK_OK (积分/库存/限购/下架 各自给出准确原因)")

    # ---------------- 3. 抽奖判定 ----------------
    lot = cfg.lottery
    assert can_draw(lot, points=10, drawn_today=0).ok
    assert "积分不足" in can_draw(lot, points=9, drawn_today=0).reason
    assert "今天已经抽过" in can_draw(lot, points=999, drawn_today=3).reason

    from astrbot_plugin_panshi.core.shop import LotteryConfig
    off = LotteryConfig(enable=False)
    assert "未开启" in can_draw(off, points=999, drawn_today=0).reason
    empty = LotteryConfig(enable=True, cost=0, prizes=[])
    assert "奖池是空的" in can_draw(empty, points=999, drawn_today=0).reason
    print("SHOP_DRAW_CHECK_OK (未开启/空奖池/次数上限/积分不足)")

    # ---------------- 4. 权重与保底 ----------------
    rnd = random.Random(12345)
    counts = {}
    for _ in range(3000):
        p, _pity = draw_prize(lot, miss_streak=0, rng=rnd)
        counts[p.name] = counts.get(p.name, 0) + 1
    # 权重 60:1，稀有应当明显更少
    assert counts.get("谢谢参与", 0) > counts.get("稀有", 0) * 10, counts

    # 保底：连击达标必出稀有
    forced, by_pity = draw_prize(lot, miss_streak=5, rng=rnd)
    assert by_pity is True and forced.name == "稀有", (forced, by_pity)
    # 未达保底不会强制
    _p, by_pity2 = draw_prize(lot, miss_streak=4, rng=rnd)
    assert by_pity2 is False
    # pity=0 时关闭保底
    nopity = LotteryConfig(enable=True, cost=0, pity=0, prizes=lot.prizes)
    _p3, by_pity3 = draw_prize(nopity, miss_streak=999, rng=rnd)
    assert by_pity3 is False, "pity=0 不应触发保底"

    # 权重全为 0 → 走保底或返回 None，不能崩
    zero = LotteryConfig(enable=True, prizes=[
        type(lot.prizes[0])(prize_id="z", name="零权重", weight=0),
    ])
    assert draw_prize(zero, miss_streak=0, rng=rnd) == (None, False)
    print("SHOP_DRAW_PRIZE_OK (权重分布合理 / 保底必中 / pity=0 关闭 / 全零权重不崩)")

    # ---------------- 5. 奖励渲染与积分换算 ----------------
    assert reward_points(type(lot.prizes[1])(prize_id="q", name="q",
                                             reward="points", value="50")) == 50
    assert reward_points(type(lot.prizes[1])(prize_id="q", name="q",
                                             reward="title", value="x")) == 0
    assert reward_points(None) == 0
    assert render_reward("points", "30", user_id="1") == "30 积分"
    assert render_reward("title", "学霸", user_id="1") == "头衔「学霸」"
    assert render_reward("manual", "", user_id="1") == "需要管理员人工发放"
    assert render_reward("none", "", user_id="1") == "谢谢参与"
    # action 模板的两个占位符
    got = render_reward("action", "setcard {user} {name}", user_id="42", nickname="小明")
    assert got == "setcard 42 小明", got
    print("SHOP_REWARD_OK (points/title/action/manual/none + 占位符替换)")

    # ---------------- 6. 违规扣分 ----------------
    # 列表写法（现在面板里的标准写法）
    rules_list = parse_penalties([
        {"reason": "刷屏", "points": 20, "ban": True},
        {"reason": "违禁词", "points": 50, "ban": True},
        {"reason": "复读", "points": 10, "ban": False},
        {"reason": "关掉的", "points": 99, "enabled": False},
        {"points": 5},                      # 没有 reason -> 跳过
    ])
    assert rules_list["刷屏"].points == 20 and rules_list["刷屏"].ban is True
    assert rules_list["复读"].ban is False
    assert rules_list["关掉的"].enabled is False
    assert "关掉的" in rules_list
    assert len(rules_list) == 4, list(rules_list)

    # 字典写法（旧格式，保留兼容）
    rules = parse_penalties({
        "刷屏": {"points": 20, "ban": True},
        "违禁词": 50,                    # 简写成数字也认
        "复读": {"points": 10, "ban": False},
        "关掉的": {"points": 99, "enabled": False},
    })
    assert rules["刷屏"].points == 20 and rules["刷屏"].ban is True
    assert rules["违禁词"].points == 50, rules["违禁词"]
    assert rules["复读"].ban is False
    assert rules["关掉的"].enabled is False
    assert parse_penalties(None) == {}
    assert parse_penalties("坏数据") == {}
    assert parse_penalties([]) == {}
    print("SHOP_PENALTY_PARSE_OK (列表/字典两种写法 + 禁用 + 坏数据)")

    # 扣分不下穿到负数
    assert apply_points_floor(100, -20) == (80, 20)
    assert apply_points_floor(10, -20) == (0, 10), "不能扣成负数"
    assert apply_points_floor(0, -20) == (0, 0), "已为 0 时不再扣"
    assert apply_points_floor(50, 30) == (80, -30), "加分时返回负数表示增加了多少"
    print("SHOP_POINTS_FLOOR_OK (扣分不下穿到负数)")

    # ---------------- 7. 存储：库存 / 限购计数 / 记录 ----------------
    import tempfile

    from astrbot_plugin_panshi.data import Storage

    tmp = tempfile.mkdtemp(prefix="panshi_shop_")
    db = Storage(os.path.join(tmp, "d.json"))
    GID, UID = "1077250302", "2226175932"

    # 不限量
    assert db.get_stock("nope") is None
    assert db.decrement_stock("nope") is True, "不限量应总是可扣"
    # 有库存
    db.set_stock("it1", 2)
    assert db.get_stock("it1") == 2
    assert db.decrement_stock("it1") is True and db.get_stock("it1") == 1
    assert db.decrement_stock("it1") is True and db.get_stock("it1") == 0
    assert db.decrement_stock("it1") is False, "库存为 0 时必须拒绝"
    assert db.get_stock("it1") == 0, "拒绝时不能把库存扣成负数"

    # 限购计数按天区分
    assert db.purchase_count_total(GID, UID, "it1") == 0
    db.record_purchase(GID, UID, "it1", 100)
    db.record_purchase(GID, UID, "it1", 100)
    db.record_purchase(GID, UID, "it2", 50)
    assert db.purchase_count_total(GID, UID, "it1") == 2
    assert db.purchase_count_today(GID, UID, "it1") == 2
    assert db.purchase_count_total(GID, UID, "it2") == 1

    # 抽奖计数与记录
    assert db.draw_count_today(GID, UID) == 0
    db.record_draw(GID, UID, "p1", 10, note="谢谢参与")
    db.record_draw(GID, UID, "p2", 10, note="50 积分")
    assert db.draw_count_today(GID, UID) == 2
    assert db.draw_count_total(GID, UID) == 2
    recs = db.recent_draws(GID, UID, limit=5)
    assert len(recs) == 2 and recs[0]["prize"] == "p2", recs

    # 记录只看自己的：换个用户应当是空的
    assert db.purchase_count_total(GID, "999", "it1") == 0
    assert db.draw_count_today(GID, "999") == 0

    # 落盘往返
    db.save()
    db2 = Storage(os.path.join(tmp, "d.json"))
    assert db2.get_stock("it1") == 0
    assert db2.purchase_count_total(GID, UID, "it1") == 2
    assert db2.draw_count_today(GID, UID) == 2
    print("SHOP_STORAGE_OK (库存/限购/抽奖计数/按用户隔离/落盘往返)")

    print("SHOP_LOGIC_DONE")


def test_welcome_self_and_at():
    """入群欢迎的两个问题：不许欢迎自己；@ 必须是真 at 而不是 CQ 字面量。

    线上截图（用户提供）：机器人被邀请入群后，自己发了一条
        @deepseek-v4.1-flash
        [CQ:at,qq=3823105457] 你好呀，欢迎来到「机器人调试群」，有问题随时提问～
    两个毛病叠在一起：
      1. 被拉进群的是机器人自己，它却「欢迎自己」。
      2. ``{at}`` 被替换成 CQ 码字符串后整段用 plain_result 发出，
         协议端不解释纯文本里的 CQ 码，于是用户看到字面量。
    """
    import asyncio

    from astrbot_plugin_panshi.core.at_chain import at_chain
    from astrbot_plugin_panshi.main import PanshiPlugin

    # ---------------- 1. CQ 码 → 真正的组件 ----------------
    def chain_text(parts):
        """把组件链渲染成纯文本。

        链里可能是组件对象（取 .text），也可能是纯字符串
        （环境没有 Plain 组件时 at_chain 会退化成字符串），两种都要认。
        """
        buf = []
        for p in parts or []:
            if isinstance(p, str):
                buf.append(p)
            else:
                buf.append(str(getattr(p, "text", "") or ""))
        return "".join(buf)

    parts = at_chain("欢迎 [CQ:at,qq=12345] 加入本群！")
    assert parts, "带 at 的文本应当被转成组件链"
    kinds = [type(p).__name__ for p in parts]
    assert "At" in kinds, f"没有生成 At 组件: {kinds}"
    at_obj = [p for p in parts if type(p).__name__ == "At"][0]
    assert str(getattr(at_obj, "qq", "")) == "12345", f"at 目标不对: {at_obj}"
    joined = chain_text(parts)
    assert "[CQ:" not in joined, f"CQ 字面量残留: {joined!r}"
    assert "欢迎" in joined and "加入本群" in joined, f"文字丢了: {joined!r}"

    assert at_chain("欢迎新人") is None, "没有 CQ 码时应返回 None"
    assert at_chain("") is None
    assert at_chain("[CQ:at,qq=all]") is None, "@全体 不应被当成普通 at"
    odd = at_chain("[CQ:unknown,foo=1] 文本 [CQ:at,qq=1]")
    assert odd, "含已知 at 时仍应转换"
    odd_text = chain_text(odd)
    assert "[CQ:unknown,foo=1]" in odd_text, f"未知类型应原样保留: {odd_text!r}"

    print("AT_CHAIN_OK (at 转组件 / 纯文本不转 / @全体跳过 / 未知类型保留)")

    # ---------------- 2. 机器人自己被拉进群时不欢迎 ----------------
    GID = 1077250302
    BOT = "3823105457"

    class _Bot:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            async def _m(*a, **kw):
                self.calls.append((name, kw))
                if name == "get_group_member_info":
                    return {"card": "", "nickname": "某人", "role": "member"}
                if name == "get_group_member_list":
                    return []
                return {"status": "ok", "retcode": 0}
            return _m

    class _Ev:
        def __init__(self, raw_message):
            self.bot = _Bot()
            self.message_obj = types.SimpleNamespace(raw_message=raw_message)
            self.message_str = ""
            self._stopped = False

        def get_group_id(self):
            return GID

        def get_self_id(self):
            return BOT

        def get_sender_id(self):
            return BOT

        def get_sender_name(self):
            return "机器人"

        def get_messages(self):
            return []

        def get_message_str(self):
            return ""

        def is_stopped(self):
            return self._stopped

        def stop_event(self):
            self._stopped = True

        def plain_result(self, text):
            return {"kind": "plain", "text": text}

        def chain_result(self, chain):
            return {"kind": "chain", "chain": chain}

        def get_extra(self, key, default=None):
            return default

        def set_extra(self, key, value):
            pass

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    def run(raw):
        inst = PanshiPlugin(_Ctx(), {
            "basic": {"default_ban_time": 60},
            "welcome": {"welcome_enable": True},
        })
        ev = _Ev(raw)

        async def collect():
            out = []
            async for item in inst.on_group_message(ev):
                out.append(item)
            return out

        return asyncio.run(collect())

    got_self = run({"post_type": "notice", "notice_type": "group_increase",
                    "group_id": GID, "user_id": BOT, "sub_type": "invite"})
    assert not got_self, f"机器人竟然欢迎了自己: {got_self!r}"
    print("WELCOME_SKIP_SELF_OK")

    got_human = run({"post_type": "notice", "notice_type": "group_increase",
                     "group_id": GID, "user_id": "226067490", "sub_type": "approve"})
    assert got_human, "真人入群应当仍然欢迎"
    first = got_human[0]
    if isinstance(first, dict) and first.get("kind") == "chain":
        names = [type(p).__name__ for p in first["chain"]]
        assert "At" in names, f"欢迎语里应有 At 组件: {names}"
        text = chain_text(first["chain"])
        assert "[CQ:" not in text, f"不应残留 CQ 字面量: {text!r}"
        print("WELCOME_AT_COMPONENT_OK")
    else:
        text = first.get("text", "") if isinstance(first, dict) else str(first)
        assert "[CQ:" not in str(text), f"残留 CQ 字面量: {text!r}"
        print("WELCOME_PLAIN_OK（未使用 at 组件）")


def test_self_defense():
    """自主还手：机器人被 @ 辱骂时，允许它自己处置，但只开一个很窄的口子。

    线上现象（用户反馈）：
        「有人 @它 骂它，然后它禁言不了提示权限不足」
    根因：处置类 LLM 工具一律要求**发消息的人**是管理员
    （`_check(event)` 判的是 event 的发送者），而这个门槛没有考虑
    「机器人在为自己还手」——骂它的普通成员自然不是管理员，于是被拦。

    修法：新增 `defense_enable`（默认关）。开启后，仅当
    **被处置的人就是发消息的人** 且 **那条消息 @ 了机器人** 时放行，
    并受冷却与每日上限约束。

    这里验证四件事：默认关、开启后放行、限流生效、借刀杀人仍被拦。
    """
    import asyncio

    from astrbot_plugin_panshi.main import PanshiPlugin

    GID = 1077250302
    BOT = "3823105457"
    OFFENDER = "1370874686"
    OTHER = "1545638433"
    VICTIM = "999999999"

    from astrbot.api.message_components import At

    class _Bot:
        def __init__(self):
            self.bans = []

        async def get_group_member_info(self, group_id, user_id, no_cache=False):
            uid = str(user_id)
            return {"role": "admin" if uid == BOT else "member",
                    "card": f"成员{uid}", "nickname": f"成员{uid}"}

        async def get_group_member_list(self, group_id):
            return []

        async def set_group_ban(self, **kw):
            self.bans.append(kw)
            return {"status": "ok", "retcode": 0}

        async def delete_msg(self, **kw):
            return {"status": "ok", "retcode": 0}

    class _Ev:
        def __init__(self, sender, msgs=(), text="", is_admin=False):
            self.bot = _Bot()
            self.message_obj = types.SimpleNamespace(raw_message={})
            self._sender = sender
            self._msgs = list(msgs)
            self.message_str = text
            self._is_admin = is_admin
            self._stopped = False

        def get_group_id(self):
            return GID

        def get_self_id(self):
            return BOT

        def get_sender_id(self):
            return self._sender

        def get_sender_name(self):
            return "某人"

        def get_messages(self):
            return self._msgs

        def get_message_str(self):
            return self.message_str

        def is_admin(self):
            return self._is_admin

        def is_stopped(self):
            return self._stopped

        def stop_event(self):
            self._stopped = True

        def plain_result(self, text):
            return {"text": text}

        def get_extra(self, key, default=None):
            return default

        def set_extra(self, key, value):
            pass

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    import os
    import tempfile

    def make(enable, mode="ban", cooldown=300, limit=5):
        """每个用例独立数据目录。

        插件的数据目录来自 ``_resolve_data_dir()``（不读配置里的 data_dir）。
        不隔离的话所有实例共用一份 panshi_data.json，冷却与每日计数会互相串味。
        """
        d = tempfile.mkdtemp(prefix="panshi_defense_")
        orig = PanshiPlugin._resolve_data_dir
        PanshiPlugin._resolve_data_dir = lambda self, _d=d: _d
        try:
            return PanshiPlugin(_Ctx(), {
                "basic": {"default_ban_time": 60},
                "automate": {"defense_enable": enable, "defense_mode": mode,
                             "defense_cooldown_seconds": cooldown,
                             "defense_daily_limit": limit,
                             "defense_ban_seconds": 600},
            })
        finally:
            PanshiPlugin._resolve_data_dir = orig

    async def call(inst, ev, tool, **kw):
        out = []
        async for r in getattr(inst, tool)(ev, **kw):
            out.append(r)
        if not out:
            return ""
        first = out[0]
        return first.get("text", "") if isinstance(first, dict) else str(first)

    async def run():
        # ---- 1) 默认关闭：不擅自还手 ----
        inst = make(False)
        ev = _Ev(OFFENDER, [At(BOT)], "@机器人 蠢鱼")
        r = await call(inst, ev, "llm_ban", target=OFFENDER, duration=600, reason="骂我")
        assert "暂不处理" in r or "未开启" in r, f"关闭时不应放行: {r!r}"
        assert not ev.bot.bans, "关闭时竟然下发了禁言"
        assert "自主还手" in r, "提示里应告诉用户怎么开启"

        # ---- 2) 开启后：还手成功，且用配置的时长 ----
        inst2 = make(True, mode="ban")
        ev2 = _Ev(OFFENDER, [At(BOT)], "@机器人 蠢鱼")
        r2 = await call(inst2, ev2, "llm_ban", target=OFFENDER, duration=60, reason="骂我")
        assert "权限不足" not in r2, f"开启后不应再回权限不足: {r2!r}"
        assert len(ev2.bot.bans) == 1, f"开启后应下发禁言: {ev2.bot.bans!r}"
        assert ev2.bot.bans[0].get("duration") == 600, (
            f"应使用配置的还手时长 600，而不是模型给的 60: {ev2.bot.bans!r}")
        assert str(ev2.bot.bans[0].get("user_id")) == OFFENDER, "目标必须是骂它的人"

        # ---- 3) mode=warn 时记警告而不是禁言 ----
        inst3 = make(True, mode="warn")
        ev3 = _Ev(OFFENDER, [At(BOT)], "@机器人 蠢鱼")
        r3 = await call(inst3, ev3, "llm_ban", target=OFFENDER, duration=600, reason="骂我")
        assert not ev3.bot.bans, f"mode=warn 不应禁言: {ev3.bot.bans!r}"
        assert "警告" in r3, f"mode=warn 应记警告: {r3!r}"

        # ---- 4) 冷却 ----
        ev4 = _Ev(OFFENDER, [At(BOT)], "@机器人 蠢鱼")
        r4 = await call(inst2, ev4, "llm_ban", target=OFFENDER, duration=60, reason="又骂")
        assert not ev4.bot.bans, "冷却期内不应再次下发禁言"
        assert "暂不处理" in r4, f"应提示被冷却拦下: {r4!r}"

        # ---- 5) 借刀杀人：群友让机器人去打别人 → 必须仍然拦住 ----
        inst5 = make(True, mode="ban")
        ev5 = _Ev(OTHER, [At(BOT)], "@机器人 把999999999禁了")
        r5 = await call(inst5, ev5, "llm_ban", target=VICTIM, duration=600, reason="看他不爽")
        assert not ev5.bot.bans, f"不该被当枪使: {ev5.bot.bans!r}"
        assert "权限不足" in r5 or "需要" in r5, f"应拦住: {r5!r}"

        # ---- 6) 没 @ 机器人 → 不算还手 ----
        inst6 = make(True, mode="ban")
        ev6 = _Ev(OFFENDER, [], "蠢鱼")
        r6 = await call(inst6, ev6, "llm_ban", target=OFFENDER, duration=600, reason="骂我")
        assert not ev6.bot.bans, "未 @ 机器人时不该还手"
        assert "权限不足" in r6 or "需要" in r6, f"应拦住: {r6!r}"

        # ---- 7) 每日上限 ----
        inst7 = make(True, mode="ban", cooldown=0, limit=2)
        got = []
        for _ in range(3):
            e = _Ev(OFFENDER, [At(BOT)], "@机器人 蠢鱼")
            await call(inst7, e, "llm_ban", target=OFFENDER, duration=60, reason="骂")
            got.append(len(e.bot.bans))
        assert got[0] == 1 and got[1] == 1, f"前两次应放行: {got!r}"
        assert got[2] == 0, f"第三次应被每日上限拦下: {got!r}"

        # ---- 8) 管理员不受开关影响 ----
        inst8 = make(False)
        ev8 = _Ev(OTHER, [], "禁言 999999999", is_admin=True)
        r8 = await call(inst8, ev8, "llm_ban", target=VICTIM, duration=60, reason="违规")
        assert len(ev8.bot.bans) == 1, f"管理员应能正常禁言: {r8!r}"

    asyncio.run(run())
    print("SELF_DEFENSE_OK (默认关/放行/警告模式/冷却/借刀拦住/未@拦住/每日上限/管理员不受影响)")


def test_autonomous_enforcement():
    """自主处置：说明书要让模型能认准目标，且真禁到自己时会被挡下。

    线上现象（用户日志）：
        Agent 使用工具: panshi_warn_user
        参数: {'target': '226067490', ...}
    226067490 是**机器人自己的 QQ**，不是骂它的人。也就是模型没认准"该处置谁"。

    LLM 工具的 docstring 就是模型看到的工具说明书，所以"怎么确定目标"必须写进去。
    这里断言两件事：

    1. 三个处置工具的说明书都写明「绝不能是机器人自己」并给出识别顺序。
    2. 即使模型真的传了机器人自己的 QQ，运行时也会被挡下（第二道防线）。
    """
    import asyncio

    from astrbot_plugin_panshi.main import PanshiPlugin

    # ---------- 1. 说明书（模型看到的就是它）----------
    #    用一个固定短语做断言，避免因为各工具措辞略有不同而误报。
    TARGET_RULE = "绝不能是机器人自己"
    ORDER_HINT = "被 @ 的人"
    for name in ("llm_ban", "llm_warn", "llm_kick"):
        doc = (getattr(PanshiPlugin, name).__doc__ or "")
        assert TARGET_RULE in doc, f"{name} 的说明书没写明不许把机器人当目标"
        assert ORDER_HINT in doc, f"{name} 的说明书没给出目标识别顺序"

    # 违规处置应当引导"先警告"，而不是动不动就禁言/踢人
    warn_doc = PanshiPlugin.llm_warn.__doc__ or ""
    assert "首选" in warn_doc, "llm_warn 应说明它是处理违规的首选动作"
    kick_doc = PanshiPlugin.llm_kick.__doc__ or ""
    assert "最后手段" in kick_doc, "llm_kick 应说明它是最后手段"

    # ---------- 2. 运行时第二道防线 ----------
    GID = 1077250302
    BOT_ID = "3823105457"
    OFFENDER = "1370874686"

    class _Bot:
        def __init__(self):
            self.bans = []

        async def get_group_member_info(self, **kw):
            uid = str(kw.get("user_id", ""))
            role = "admin" if uid == BOT_ID else "member"
            return {"role": role, "card": f"成员{uid}", "nickname": f"成员{uid}"}

        async def get_group_member_list(self, **kw):
            return []

        async def set_group_ban(self, **kw):
            self.bans.append(kw)
            return {"status": "ok", "retcode": 0}

        async def delete_msg(self, **kw):
            return {"status": "ok", "retcode": 0}

    class _Ev:
        def __init__(self):
            self.bot = _Bot()
            self.message_obj = types.SimpleNamespace(raw_message={})
            self._stopped = False

        def get_group_id(self):
            return GID

        def get_self_id(self):
            return BOT_ID

        def get_sender_id(self):
            return OFFENDER

        def get_sender_name(self):
            return "某人"

        def get_messages(self):
            return []

        def get_message_str(self):
            return ""

        def is_stopped(self):
            return self._stopped

        def stop_event(self):
            self._stopped = True

        def plain_result(self, text):
            return {"text": text}

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.data import Storage

    class _Ctx:
        def register_web_api(self, *a, **k):
            pass

    inst = PanshiPlugin(_Ctx(), {"basic": {"default_ban_time": 60}})

    async def run():
        ev = _Ev()
        # 模型把机器人自己当成目标 —— 必须被挡下，且不能真的发出禁言请求
        r_self = await inst.normal.set_ban(ev, BOT_ID, 60)
        assert "机器人自己" in r_self, f"禁到自己时没有拒绝: {r_self!r}"
        assert not ev.bot.bans, f"禁到自己时竟然真的调用了协议端: {ev.bot.bans!r}"

        # 正常目标仍然要能禁言（别把防线做成把功能关掉）
        r_ok = await inst.normal.set_ban(ev, OFFENDER, 60)
        assert "已禁言" in r_ok, f"正常禁言失败了: {r_ok!r}"
        assert len(ev.bot.bans) == 1, f"正常禁言没有下发: {ev.bot.bans!r}"
        assert str(ev.bot.bans[0].get("user_id")) == OFFENDER

    asyncio.run(run())
    print("AUTONOMOUS_ENFORCEMENT_OK (目标识别说明书 + 禁到自己被挡下 + 正常目标可用)")


def test_group_role_permission():
    """群主/管理员发指令，权限判定必须认出来。
    回归背景：``get_user_level`` 过去只做同步判定，靠读
    ``event.message_obj.sender.role`` 取群身份。但 AstrBot 的 ``MessageMember``
    **只定义了 user_id 与 nickname**，适配器也从不下发 role —— 那个读法恒为空串。

    结果：群主或群管理员发管理指令时被判成普通成员，机器人回
    「⛔ 权限不足，该操作需要「管理员」及以上权限。」，而用户明明有权限。

    修法：改走协议端异步查询（``bot.get_group_member_info``），
    这也是本插件早就在用的两层取数逻辑。
    """
    import asyncio

    from astrbot_plugin_panshi.utils import (
        PermLevel,
        check_permission,
        check_permission_async,
        get_user_level,
        get_user_level_async,
    )
    from astrbot_plugin_panshi.utils import permission as perm_mod

    # 每个用例都要清缓存，否则会串
    perm_mod._ROLE_CACHE.clear()

    class _Bot:
        """协议端：1=群主 2=管理员 3=普通成员；列表接口故意不返回 role，
        用来验证「单人查询拿到就够用」。"""

        def __init__(self):
            self.calls = []

        async def get_group_member_info(self, group_id, user_id, no_cache=False):
            self.calls.append(("info", int(user_id)))
            role = {1: "owner", 2: "admin", 3: "member"}.get(int(user_id), "member")
            return {"role": role, "nickname": f"用户{user_id}"}

        async def get_group_member_list(self, group_id):
            self.calls.append(("list", group_id))
            return []

    class _Ev:
        """模拟真实事件：**没有** role 字段（与 AstrBot 一致）。"""

        def __init__(self, sender, is_admin=False, group="1077250302"):
            self.bot = _Bot()
            self._sender = sender
            self._group = group
            self._is_admin = is_admin
            # 与 AstrBot 的 MessageMember 一样：只有 user_id / nickname
            self.message_obj = types.SimpleNamespace(
                sender=types.SimpleNamespace(user_id=sender, nickname="某人")
            )

        def get_sender_id(self):
            return self._sender

        def get_group_id(self):
            return self._group

        def is_admin(self):
            return self._is_admin

    async def _run():
        # ---- 群主：必须判为 OWNER ----
        perm_mod._ROLE_CACHE.clear()
        ev = _Ev("1")
        lvl = await get_user_level_async(ev, [])
        assert lvl == PermLevel.OWNER, f"群主应判为 OWNER，实际 {lvl!r}"
        ok = await check_permission_async(ev, PermLevel.ADMIN, [])
        assert ok, "群主必须能通过管理员门槛"

        # ---- 群管理员：必须判为 ADMIN ----
        perm_mod._ROLE_CACHE.clear()
        ev2 = _Ev("2")
        lvl2 = await get_user_level_async(ev2, [])
        assert lvl2 == PermLevel.ADMIN, f"群管理员应判为 ADMIN，实际 {lvl2!r}"
        assert await check_permission_async(ev2, PermLevel.ADMIN, [])
        # 管理员不是群主，不该通过群主门槛
        assert not await check_permission_async(ev2, PermLevel.OWNER, [])

        # ---- 普通成员：仍然要拦住 ----
        perm_mod._ROLE_CACHE.clear()
        ev3 = _Ev("3")
        lvl3 = await get_user_level_async(ev3, [])
        assert lvl3 == PermLevel.MEMBER, f"普通成员应判为 MEMBER，实际 {lvl3!r}"
        assert not await check_permission_async(ev3, PermLevel.ADMIN, [])

        # ---- 超管仍然最高优先 ----
        perm_mod._ROLE_CACHE.clear()
        lvl4 = await get_user_level_async(_Ev("3"), ["3"])
        assert lvl4 == PermLevel.SUPER, f"超管应判为 SUPER，实际 {lvl4!r}"

        # ---- AstrBot 全局管理员也认 ----
        perm_mod._ROLE_CACHE.clear()
        lvl5 = await get_user_level_async(_Ev("3", is_admin=True), [])
        assert lvl5 == PermLevel.ADMIN, f"全局管理员应判为 ADMIN，实际 {lvl5!r}"

        # ---- 同步版读不到群身份（这是事实，不是 bug）----
        # 它不该假装知道；同步版只在「已有缓存」时才能给出群身份。
        perm_mod._ROLE_CACHE.clear()
        ev6 = _Ev("1")
        assert get_user_level(ev6, []) == PermLevel.MEMBER, (
            "同步版没有缓存时读不到群主身份（这是设计：它在等异步查询填缓存）"
        )
        # 异步查过一次后，同步版应能命中缓存
        await get_user_level_async(ev6, [])
        assert get_user_level(ev6, []) == PermLevel.OWNER, (
            "异步查询后同步版应命中缓存，直接判为 OWNER"
        )

        # ---- 缓存生效：第二次不应再打协议端 ----
        perm_mod._ROLE_CACHE.clear()
        ev7 = _Ev("2")
        await get_user_level_async(ev7, [])
        n1 = len(ev7.bot.calls)
        await get_user_level_async(ev7, [])
        n2 = len(ev7.bot.calls)
        assert n1 == n2, f"第二次查询应命中缓存，协议端调用数 {n1} -> {n2}"

        # ---- 协议端全挂时不能崩，优雅降级为 MEMBER ----
        perm_mod._ROLE_CACHE.clear()

        class _DeadBot:
            async def get_group_member_info(self, **kw):
                raise RuntimeError("协议端不可用")

            async def get_group_member_list(self, **kw):
                raise RuntimeError("协议端不可用")

        ev8 = _Ev("2")
        ev8.bot = _DeadBot()
        lvl8 = await get_user_level_async(ev8, [])
        assert lvl8 == PermLevel.MEMBER, "协议端不可用时应降级为 MEMBER 而不是抛异常"

        # ---- check_permission（同步版）仍可调用，不崩 ----
        perm_mod._ROLE_CACHE.clear()
        assert check_permission(_Ev("3"), PermLevel.ADMIN, []) is False

    asyncio.run(_run())
    perm_mod._ROLE_CACHE.clear()
    print("GROUP_ROLE_PERMISSION_OK (群主/管理员/成员/超管/降级/缓存)")


def test_keyword_only_misclassification():
    """不许「只识关键词」：提到功能名 ≠ 要执行该功能。

    回归背景：``looks_like_command`` 对 ``_COMMAND_VERBS`` 做子串匹配，
    而词表里有 ``群公告``。于是任何提到「公告」的话都被当成管理指令——
    群友问「你觉得群公告应该加上什么」，机器人判成指令、拦下、回
    「⛔ 权限不足」，同时这条消息也没能好好回答。

    用户原话：「只识关键词，提问和指令傻傻分不清。」

    这里的用例全部取自用户的真实聊天日志。
    """
    from astrbot_plugin_panshi.core.intent_gate import looks_like_command

    # 取自日志：这些**不是**指令
    NOT_COMMANDS = [
        ("你觉得此次事件群公告应该加上什么", "在问公告该写什么"),
        ("禁不了我吧", "成员挑衅"),
        ("我帮你说话你还撤回我消息啊", "抱怨，动作词是在描述对方做过的事"),
        ("瞎总结公告", "吐槽"),
        ("蠢鱼只识关键词吗？", "质问"),
        ("跟他讲发生了什么", "让机器人转述，不是管理动作"),
        ("群公告", "裸话题词，是名词"),
        ("你怎么看群公告", "征询意见"),
        ("群公告应该加上什么", "提问"),
        ("今天天气不错", "闲聊"),
        ("哈哈哈", "闲聊"),
        ("这个群规是不是该更新了", "征询"),
        ("为什么要禁言他", "在问原因"),
    ]
    # 这些**是**指令，不能因为防误判而漏掉
    COMMANDS = [
        ("禁言他", "明确动作"),
        ("给他禁言一小时", "动作+时长"),
        ("把某人踢了", "明确动作"),
        ("撤回刚刚那个群公告", "明确动作（撤回优先于话题词）"),
        ("能不能帮我禁言他", "疑问句但仍是请求执行"),
        ("帮我禁言@某人", "请求执行"),
        ("全体禁言", "明确动作"),
        ("开启全体禁言", "明确动作"),
        ("解除禁言", "明确动作"),
        ("改群名 新名字", "明确动作"),
        ("把他拉黑", "明确动作"),
        ("上管给某某", "明确动作"),
        ("群公告改成明天考试", "话题词 + 真动作，不能因防误判而漏掉"),
        ("清屏", "明确动作"),
        ("设个精华", "明确动作"),
    ]

    miss, over = [], []
    for text, why in NOT_COMMANDS:
        if looks_like_command(text):
            over.append((text, why))
    for text, why in COMMANDS:
        if not looks_like_command(text):
            miss.append((text, why))

    assert not over, f"被误判成指令（会回「权限不足」并吞掉消息）：{over}"
    assert not miss, f"真指令被漏判为提问：{miss}"
    print(f"KEYWORD_ONLY_OK (提问 {len(NOT_COMMANDS)} 条不误判 / "
          f"指令 {len(COMMANDS)} 条不漏判)")


def test_notice_dispatch():
    """入群通知与加群申请必须能一路走到欢迎语 / 审批。

    线上两轮反馈「入群没欢迎」「入群申请依旧没有」。根因有两层，都不是过滤器的事：

    1. **AstrBot 根本没有 on_notice / on_request 钩子。**
       它的钩子是固定集合（on_llm_request / on_agent_begin / on_decorating_result /
       on_after_message_sent / on_platform_loaded …），不含任何通知类钩子。
       适配器的 notice/request 回调最终走
       ``EventBus.dispatch -> pipeline_scheduler -> 普通消息管线``，
       即**通知是以普通消息事件派发出来的**。
       把方法挂成 ``on_notice`` 就永远不会被调用。

    2. **读错了字段层级。**
       适配器把整包原始事件存在 ``message_obj.raw_message``（一个 dict）里，
       ``AstrBotMessage`` 自身没有 ``post_type``。
       旧代码读 ``message_obj.post_type``，恒为 None。

    本用例因此驱动的是**真实入口** ``on_group_message``（它才是会被框架调用的那个），
    并把 ``raw_message`` 造成与协议端一致的 dict。
    """
    import asyncio

    from astrbot_plugin_panshi.main import PanshiPlugin

    GID = 1077250302
    NEWBIE = "226067490"
    FLAG = "flag-apply-001"

    class _Bot:
        def __init__(self):
            self.calls = []

        def __getattr__(self, name):
            async def _m(*a, **kw):
                self.calls.append((name, a, kw))
                if name == "get_group_member_info":
                    return {"card": "", "nickname": f"新人{NEWBIE}", "role": "member"}
                if name == "get_group_member_list":
                    return []
                return {"status": "ok", "retcode": 0}
            return _m

    class _Ev:
        """与 AstrBot 一致：原始包在 message_obj.raw_message（dict）。"""

        def __init__(self, robot, raw_message):
            self.bot = _Bot()
            self.message_obj = types.SimpleNamespace(raw_message=raw_message)
            self._group = GID
            self._self = robot
            self._stopped = False
            self.message_str = ""

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

        def get_message_str(self):
            return self.message_str

        def is_stopped(self):
            return self._stopped

        def stop_event(self):
            self._stopped = True

        def plain_result(self, text):
            return {"text": text}

        def chain_result(self, chain):
            return {"kind": "chain", "chain": chain}

        def get_extra(self, key, default=None):
            return default

        def set_extra(self, key, value):
            pass

    def run(raw_message, cfg_extra=None):
        cfg = {"basic": {"default_ban_time": 60},
               "welcome": {"welcome_enable": True}}
        if cfg_extra:
            for key, val in cfg_extra.items():
                cfg.setdefault(key, {}).update(val)

        class _Ctx:
            def register_web_api(self, *a, **k):
                pass

        inst = PanshiPlugin(_Ctx(), cfg)
        ev = _Ev(inst.cfg.get("basic", "self_id", "") or "3823105457", raw_message)

        async def collect():
            out = []
            # 关键：驱动真实入口 on_group_message，不是 on_notice
            async for item in inst.on_group_message(ev):
                out.append(item)
            return out, ev

        return asyncio.run(collect())

    def text_of(items):
        """把结果渲染成可断言的纯文本。

        欢迎语现在可能走 chain_result（@ 要用真 At 组件）。
        链里既可能是组件对象（取 .text），也可能是纯字符串
        （环境里没有 Plain 组件时会退化成字符串），两种都要认。
        """
        if not items:
            return ""
        first = items[0]
        if not isinstance(first, dict):
            return str(first)
        if first.get("kind") == "chain" or "chain" in first:
            buf = []
            for p in (first.get("chain") or []):
                if isinstance(p, str):
                    buf.append(p)
                else:
                    buf.append(str(getattr(p, "text", "") or ""))
            return "".join(buf)
        return first.get("text", "")

    # ---------- 1) 入群通知 → 欢迎语 ----------
    items, _ = run({"post_type": "notice", "notice_type": "group_increase",
                    "group_id": GID, "user_id": NEWBIE, "sub_type": "approve"})
    text = text_of(items)
    assert text.strip(), (
        f"入群通知没有产出欢迎语 —— 通知分派断了。实际产出: {items!r}"
    )
    print(f"NOTICE_WELCOME_OK ({text[:26]}…)")

    # ---------- 2) 加群申请 → 有人处理，不崩 ----------
    items_j, _ = run({"post_type": "request", "request_type": "group",
                      "group_id": GID, "user_id": NEWBIE, "flag": FLAG,
                      "comment": "想进群看看", "sub_type": "add"})
    text_j = text_of(items_j)
    print(f"JOIN_REQUEST_OK ({text_j[:30] or '已进入审批流程（无即时回执）'}…)")

    # ---------- 3) 普通消息不能被当成通知 ----------
    items_m, _ = run({"post_type": "message", "message_type": "group",
                      "group_id": GID, "user_id": NEWBIE,
                      "raw_message": "你好", "message_id": 1})
    assert not [i for i in items_m
                if "欢迎" in str(i) or "入群" in str(i)], \
        f"普通消息被误当成入群通知: {items_m!r}"

    # ---------- 4) 关闭欢迎 → 不产出欢迎语 ----------
    items_off, _ = run({"post_type": "notice", "notice_type": "group_increase",
                        "group_id": GID, "user_id": NEWBIE, "sub_type": "approve"},
                       cfg_extra={"welcome": {"welcome_enable": False}})
    assert not text_of(items_off).strip(), \
        f"已关闭欢迎却仍然产出: {items_off!r}"

    # ---------- 5) 退群通知 → 不崩 ----------
    run({"post_type": "notice", "notice_type": "group_decrease",
         "group_id": GID, "user_id": NEWBIE, "sub_type": "leave"})

    # ---------- 6) 空 raw_message → 当作普通消息，不能崩 ----------
    run({})

    # ---------- 7) 回归：这两个方法绝不能是框架回调 ----------
    #    如果哪天有人又把 @filter 装饰器加回去，这里会失败——
    #    因为 AstrBot 不认这个钩子，加了就等于永远不会被调用。
    #
    #    检查方式要看**装饰器本身**，不能扫源码文本：函数的 docstring 里
    #    正好在解释「不要加 @filter」，扫文本会把自己的说明当成违规。
    import inspect

    def decorator_names(fn):
        # 被 wraps 包过的函数会带 __wrapped__；滤镜装饰器不改签名，
        # 所以直接看源码里 def 之前那一行是否真的是装饰器行。
        lines = inspect.getsource(fn).splitlines()
        out = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                break
            if stripped.startswith("@"):
                out.append(stripped)
        return out

    for name in ("on_notice",):
        decs = decorator_names(getattr(PanshiPlugin, name))
        assert not decs, (
            f"{name} 被挂上了装饰器 {decs}。AstrBot 没有这个钩子，"
            "挂上它方法就永远不会被调用（入群欢迎会再次失效）。"
            "它应当由 on_group_message 主动调用，并保持无装饰器。"
        )

    # on_group_message 才是框架调用的入口，它必须有过滤器
    gm = decorator_names(PanshiPlugin.on_group_message)
    assert any("event_message_type" in d for d in gm), (
        f"on_group_message 缺少 event_message_type 过滤器，它收不到事件：{gm}"
    )

    print("NOTICE_OTHER_TYPES_OK (退群/普通消息/空包/非回调)")


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
    # 分组会随功能增加，所以断言"包含哪些必需组"和"数量随时间只增不减"，
    # 而不是写死一个数字——写死的话每加一个配置组都要改测试，
    # 久而久之就没人维护这条断言了。
    groups = cfg.schema_snapshot()
    keys = [g["key"] for g in groups]
    required = ["basic", "guard", "welcome", "warning", "smart",
                "activity", "automate", "interact"]
    missing = [k for k in required if k not in keys]
    assert not missing, f"缺少配置分组 {missing}，实际 {keys}"
    assert keys[:2] == ["basic", "guard"], keys
    assert len(groups) >= len(required), [g["key"] for g in groups]
    total_fields = sum(len(g["fields"]) for g in groups)
    assert total_fields >= 40, total_fields
    print(f"SCHEMA_OK ({len(groups)} 组 / {total_fields} 项)")

    # shop 这一组**已从 schema 里删掉**。
    #
    # 它的设置都搬到了顶栏的「🛒 商品」与「🎰 抽奖」两个页面。
    # 一开始我只是在自己的面板里隐藏它，结果 AstrBot 原生配置页照样显示——
    # 用户看到的还是那个「积分商城与抽奖」卡片。所以要删就得从 schema 删。
    #
    # 删掉是安全的：parse_config 对每一项都有代码内默认值，
    # 而面板里改的值存在 storage，不在配置里。
    assert "shop" not in keys, f"面板里不该有 shop 组：{keys}"
    import json as _json2
    import os as _os2
    _schema_path = _os2.path.join(
        _os2.path.dirname(_os2.path.abspath(__file__)), "_conf_schema.json")
    with open(_schema_path, encoding="utf-8") as _f:
        _raw_schema = _json2.load(_f)
    assert "shop" not in _raw_schema, (
        "schema 里不该再有 shop 组——只在自己的面板里隐藏挡不住 "
        "AstrBot 原生配置页，用户照样能看到那个卡片")
    print("SCHEMA_NO_SHOP_GROUP_OK (schema 里已无 shop 组，原生配置页也不会显示)")

    # 没有 shop 配置组时，代码默认值必须齐全
    from astrbot_plugin_panshi.core.shop import parse_config as _parse_shop
    _empty, _ = _parse_shop({})
    assert _empty.enable is False and _empty.lottery.enable is False
    assert _empty.lottery.cost == 10, _empty.lottery
    assert _empty.lottery.daily_limit == 3, _empty.lottery
    assert _empty.lottery.pity == 10, _empty.lottery
    assert _empty.items == [] and _empty.lottery.prizes == []
    print("SCHEMA_SHOP_DEFAULTS_IN_CODE_OK (删掉配置组后，默认值由代码提供)")
    # 三个版本号必须一致。
    #
    # 回归背景：__init__.py 一直停在 1.6.2，而 metadata.yaml 已经到 v1.9.8，
    # 没人发现——因为 CI 只查了 metadata 和 pages_service 两处。
    # 三处都表示「插件版本」，不一致时排查问题会先被误导。
    import pathlib as _pl
    import re as _re
    _repo = _pl.Path(_schema_path).parent

    def _grab(fname, pattern):
        t = (_repo / fname).read_text(encoding="utf-8")
        m = _re.search(pattern, t, _re.M)
        return m.group(1) if m else None

    _versions = {
        "metadata.yaml": _grab("metadata.yaml", r"^version:\s*v?(\S+)\s*$"),
        "pages_service.py": _grab("pages_service.py",
                                  r'FALLBACK_VERSION\s*=\s*"v?([^"]+)"'),
        "__init__.py": _grab("__init__.py", r'__version__\s*=\s*"([^"]+)"'),
    }
    assert None not in _versions.values(), f"读不到版本号：{_versions}"
    assert len(set(_versions.values())) == 1, (
        f"三处版本号必须一致，改一处就要全改：{_versions}")
    print(f"VERSION_CONSISTENT_OK (三处都是 v{_versions['metadata.yaml']})")

    # 面板显示的版本号必须来自**正在运行的代码**，不是磁盘上的文件。
    #
    # 回归背景：线上装的 1.9.9，面板一直显示 1.9.5。原因就是
    # plugin_version() 读 metadata.yaml —— 文件旧了、或不在预期位置，
    # 显示出来的数字和你实际跑的那份代码没有任何必然关系。
    # 现在读包里 __init__.py 的 __version__，跑哪份代码就显示哪个版本。
    from astrbot_plugin_panshi.pages_service import PageService
    import astrbot_plugin_panshi as _pkg

    _svc = PageService.__new__(PageService)
    _shown = _svc.plugin_version()
    assert str(_shown).lstrip("v") == str(_pkg.__version__).lstrip("v") == \
        _versions["__init__.py"], (
            f"面板显示的版本号（{_shown}）必须等于运行中代码的 __version__"
            f"（{_pkg.__version__}）")
    print(f"VERSION_FROM_CODE_OK (面板显示 {_shown}，来自运行中的代码)")

    # metadata.yaml 的路径解析也要对（之前多找了一级，永远读不到）
    _svc2 = PageService.__new__(PageService)
    _meta_v = _svc2._version_from_metadata()
    assert _meta_v, (
        "_version_from_metadata() 读不到 metadata.yaml —— 路径算错了。"
        "它用于交叉核对打包是否完整，读不到就失去意义。")
    assert _meta_v.lstrip("v") == _versions["metadata.yaml"], _meta_v
    print("VERSION_METADATA_PATH_OK (metadata.yaml 路径解析正确)")

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


def test_group_role_and_evict():
    """群列表：补齐机器人身份 + 退群立刻摘除。

    回归背景：
      1. OneBot 的 ``get_group_list`` **不返回**机器人自己在群里的角色，
         所以面板上"我在这个群是不是管理员"一直显示不出来——只能逐群问
         ``get_group_member_info``；
      2. 机器人退群后群缓存还会挂最多 60 秒，面板上留着一个已经不存在的群。
    """
    import asyncio

    from astrbot_plugin_panshi.data import GroupInfoCache

    calls = {"list": 0, "member": 0}

    class _Client:
        #: 让候选带上 self_id=99（查成员信息时要显式带账号）
        _wsr_api_clients = {"99": object()}

        async def call_action(self, action, **kw):
            if action == "get_group_list":
                calls["list"] += 1
                return [
                    {"group_id": 1, "group_name": "甲群", "member_count": 10},
                    {"group_id": 2, "group_name": "乙群", "member_count": 20},
                ]
            if action == "get_group_member_info":
                calls["member"] += 1
                assert str(kw.get("user_id")) == "99", kw
                role = {1: "owner", 2: "member"}.get(
                    int(kw.get("group_id")), "member")
                return {"status": "ok", "retcode": 0, "data": {"role": role}}
            raise AssertionError(f"意外的接口 {action}")

    class _Platform:
        @staticmethod
        def get_client():
            return _Client()

    class _PM:
        def get_insts(self):
            return [_Platform()]

    class _C:
        platform_manager = _PM()

    cache = GroupInfoCache(_C())
    groups = asyncio.run(cache.list_groups(force=True))
    by_id = {g["group_id"]: g for g in groups}
    assert by_id["1"]["bot_role"] == "owner", by_id["1"]
    assert by_id["2"]["bot_role"] == "member", by_id["2"]
    print("GROUP_ROLE_FILLED_OK "
          f"(甲群={by_id['1']['bot_role']}，乙群={by_id['2']['bot_role']})")

    # 身份有独立缓存：再刷群列表不该重复问成员信息
    before = calls["member"]
    asyncio.run(cache.list_groups(force=True))
    assert calls["member"] == before, (before, calls["member"])
    print("GROUP_ROLE_CACHED_OK (身份单独缓存，不跟着群列表反复查)")

    # 机器人退群：立刻摘掉，且可重复调用
    assert cache.evict("2") is True
    assert [g["group_id"] for g in cache.snapshot()] == ["1"], cache.snapshot()
    assert cache.evict("2") is False
    assert cache.evict("") is False
    print("GROUP_EVICT_OK (退群立即从缓存摘掉，重复调用安全)")


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


def test_guard_punish_reports_failure():
    """风控处罚失败时必须如实报告，不能回「已处理」。

    回归背景：机器人不是群管理员时 `set_group_ban` 与 `delete_msg` 都会被
    协议端拒绝，而原实现无论成败都回「⚠️ 检测到 X，已处理。」——用户以为
    群友已被禁言，其实没有，只能靠"他怎么还在说话"才发现。
    「静默失效的处罚」是群管插件里最难排查的一类问题，必须堵死。
    """
    import asyncio
    import shutil
    import tempfile
    from collections import deque

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.guard import GuardHandle
    from astrbot_plugin_panshi.data import Storage

    class _BotDenied:
        """机器人不是管理员：禁言与撤回都被协议端拒绝。"""

        async def set_group_ban(self, **kw):
            return {"status": "failed", "retcode": 1200,
                    "message": "not group admin"}

        async def delete_msg(self, **kw):
            return {"status": "failed", "retcode": 1200,
                    "message": "no permission"}

    class _BotAllowed(_BotDenied):
        async def set_group_ban(self, **kw):
            return {"status": "ok", "retcode": 0}

        async def delete_msg(self, **kw):
            return {"status": "ok", "retcode": 0}

    class _Ev:
        def __init__(self, bot):
            self.bot = bot

        def get_group_id(self):
            return 100

        def get_sender_id(self):
            return 3

        def get_self_id(self):
            return 99

        def get_sender_name(self):
            return "测试"

    tmp = tempfile.mkdtemp(prefix="panshi_guard_")
    try:
        h = GuardHandle(PluginConfig({}), Storage(tmp))

        # 对照组：动作成功 —— 回执要明确「已撤回并禁言」
        h._recent["100"] = deque([{"user_id": 3, "message_id": 12345}])
        ok_msg = asyncio.run(h._punish(_Ev(_BotAllowed()), "违禁词", 60))
        assert "已撤回并禁言" in ok_msg, ok_msg
        print(f"GUARD_PUNISH_OK_MSG_OK ({ok_msg})")

        # 关键用例：动作全被拒 —— 必须如实报告，不能再出现「已处理」
        h._recent["100"] = deque([{"user_id": 3, "message_id": 12345}])
        bad_msg = asyncio.run(h._punish(_Ev(_BotDenied()), "刷屏", 300))
        assert "已处理" not in bad_msg, \
            f"禁言和撤回都失败了，回执却还说「已处理」：{bad_msg}"
        assert "未生效" in bad_msg, f"没说明禁言失败：{bad_msg}"
        assert ("管理员" in bad_msg or "权限" in bad_msg), \
            f"没把协议端给的原因/建议带出来：{bad_msg}"
        assert "已记警告" in bad_msg, f"没说明警告仍然生效：{bad_msg}"
        print("GUARD_PUNISH_FAILURE_REPORTED_OK "
              f"({bad_msg.replace(chr(10), ' / ')})")

        # 没有可撤回的消息时，成功回执也不能谎称「已撤回」
        h._recent["100"] = deque()
        no_recall = asyncio.run(h._punish(_Ev(_BotAllowed()), "广告", 60))
        assert "已撤回" not in no_recall, no_recall
        assert "禁言" in no_recall, no_recall
        print(f"GUARD_PUNISH_NO_RECALL_HONEST_OK ({no_recall})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_points_natural_language():
    """加减分：不打斜杠的自然写法 + 「我」指代自己。

    用户要的用法：「加分@张三 10」；给自己加就写「我」。
    """
    from astrbot.api.message_components import At, Reply

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.local_intent import LocalIntentParser
    from astrbot_plugin_panshi.utils import parse_target, self_target

    class _Ev:
        bot = None

        def __init__(self, uid=7, gid=100, ats=(), text="", reply=None):
            self._uid, self._gid = uid, gid
            self._ats, self.message_str, self._reply = ats, text, reply

        def get_group_id(self):
            return self._gid

        def get_sender_id(self):
            return self._uid

        def get_self_id(self):
            return 99

        def get_sender_name(self):
            return f"用户{self._uid}"

        def get_messages(self):
            # 必须用真正的组件类型：代码里是 isinstance(seg, At) 判断的
            segs = []
            if self._reply:
                segs.append(Reply(id="m1", sender_id=self._reply))
            segs += [At(qq=q) for q in self._ats]
            return segs

    # ---- 「我」只能是独立成词的 ----
    assert self_target(_Ev(text="加分 我 10"), "加分 我 10") == "7"
    assert self_target(_Ev(text="加分@我 10"), "加分@我 10") == "7"
    assert self_target(_Ev(text="我给他加10分"), "我给他加10分") is None, \
        "「我给他加10分」里的「我」是主语，不该被当成目标"
    assert self_target(_Ev(text="加分@张三 10"), "加分@张三 10") is None
    print("SELF_TARGET_TOKEN_OK (只有独立成词的「我」才指代自己)")

    # ---- 斜杠指令通道：parse_target 认「我」 ----
    assert parse_target(_Ev(text="加分 我 10"), "我 10")[0] == "7"
    assert parse_target(_Ev(text="加分 我 10", reply="555"), "我 10")[0] == "7", \
        "写了「我」却被顺手引用的消息带偏了"
    assert parse_target(_Ev(text="加分 10", reply="555"), "10")[0] == "555", \
        "没写「我」时应当仍按引用走"
    print("PARSE_TARGET_SELF_OK (写了「我」优先，不写仍按引用)")

    # ---- 自然语言通道：不打斜杠 ----
    p = LocalIntentParser(PluginConfig({}))

    it = p.parse(_Ev(uid=7, ats=[1001], text="加分@某人 10"), "加分@某人 10")
    assert it and it["action"] == "add_points" and it["amount"] == 10, it
    assert it["target"] == "1001", it
    print(f"NL_ADD_AT_OK ({it})")

    it = p.parse(_Ev(uid=7, ats=[1001], text="扣@张三 5分"), "扣@张三 5分")
    assert it and it["action"] == "sub_points" and it["amount"] == 5, it
    print(f"NL_SUB_OK ({it})")

    it = p.parse(_Ev(uid=7, text="给我加20分"), "给我加20分")
    assert it and it["action"] == "add_points" and it["target"] == "7", it
    print(f"NL_SELF_COMPACT_OK ({it})")

    it = p.parse(_Ev(uid=7, text="加分 我 10"), "加分 我 10")
    assert it and it["target"] == "7" and it["amount"] == 10, it
    print(f"NL_SELF_TOKEN_OK ({it})")

    # 「帮我@张三 加10分」的目标是张三，不是"我"
    it = p.parse(_Ev(uid=7, ats=[1001], text="帮我@张三 加10分"), "帮我@张三 加10分")
    assert it and it["target"] == "1001", it
    print(f"NL_HELP_ME_TARGET_OTHERS_OK ({it})")

    it = p.parse(_Ev(uid=7, ats=[1002], text="奖励@李四 50 表现好"), "奖励@李四 50 表现好")
    assert it and it["action"] == "add_points" and it["amount"] == 50, it
    print(f"NL_REWARD_OK ({it})")

    # 否定不能被执行
    assert p.parse(_Ev(uid=7, text="别扣分"), "别扣分") is None, "「别扣分」被当成了指令"
    print("NL_NEGATION_OK (「别扣分」不会被解析成扣分)")

    # QQ 号不能被当成数量
    it = p.parse(_Ev(uid=7, ats=[1001], text="加分@张三 2226175932"), "加分@张三 2226175932")
    assert it is None, f"QQ 号被当成了数量：{it}"
    print("NL_QQ_NOT_AMOUNT_OK (QQ 号不会被当成数量)")

    # 认不出目标就放弃，绝不猜
    assert p.parse(_Ev(uid=7, text="加分 10"), "加分 10") is None
    print("NL_NO_TARGET_GIVES_UP_OK (认不出目标就不执行)")


def test_points_earning_ways():
    """积分获取途径：连签加成 / 早鸟奖 / 发言得分 / 新人礼包 / 互动得分。

    重点是**限流**：没有冷却与每日上限，「发言给分」就是把积分系统
    交给刷屏脚本——一次连发就能把分刷满。
    """
    import asyncio
    import shutil
    import tempfile
    import time

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.activity import ActivityHandle
    from astrbot_plugin_panshi.core.interact import InteractHandle
    from astrbot_plugin_panshi.data import Storage

    class _Ev:
        bot = None

        def __init__(self, uid=7, gid=100):
            self._uid, self._gid = uid, gid

        def get_group_id(self):
            return self._gid

        def get_sender_id(self):
            return self._uid

        def get_self_id(self):
            return 99

        def get_sender_name(self):
            return f"用户{self._uid}"

    tmp = tempfile.mkdtemp(prefix="panshi_earn_")
    try:
        db = Storage(tmp)
        yesterday = time.strftime("%Y-%m-%d",
                                  time.localtime(time.time() - 86400))

        # 连签加成 + 早鸟奖
        cfg = PluginConfig({"activity": {
            "checkin_enable": True, "checkin_points": 10,
            "checkin_random_bonus": 0, "checkin_streak_bonus": 2,
            "checkin_streak_bonus_cap": 10, "checkin_first_bonus": 5}})
        h = ActivityHandle(cfg, db)
        rec = db._user(100, 7)
        rec["checkin_date"] = yesterday
        rec["checkin_streak"] = 3
        db.save()
        r = asyncio.run(h.checkin(_Ev(7)))
        # 基础 10 + 连签 min(2*(4-1), 10)=6 + 早鸟 5 = 21
        assert db.get_points(100, 7) == 21, (r, db.get_points(100, 7))
        assert "早鸟" in r and "连续签到 4 天" in r, r
        print(f"CHECKIN_STREAK_FIRST_OK ({r.replace(chr(10), ' / ')})")

        # 同一天第二个人签到：没有早鸟，连签从 1 开始
        r2 = asyncio.run(h.checkin(_Ev(8)))
        assert db.get_points(100, 8) == 10, (r2, db.get_points(100, 8))
        assert "早鸟" not in r2, r2
        print(f"CHECKIN_EARLY_BIRD_ONCE_OK ({r2.splitlines()[0]})")

        # 连签加成必须封顶
        tmp2 = tempfile.mkdtemp(prefix="panshi_earn2_")
        try:
            db2 = Storage(tmp2)
            h2 = ActivityHandle(PluginConfig({"activity": {
                "checkin_enable": True, "checkin_points": 0,
                "checkin_random_bonus": 0, "checkin_streak_bonus": 100,
                "checkin_streak_bonus_cap": 10, "checkin_first_bonus": 0}}), db2)
            rec2 = db2._user(100, 7)
            rec2["checkin_date"] = yesterday
            rec2["checkin_streak"] = 99
            db2.save()
            asyncio.run(h2.checkin(_Ev(7)))
            assert db2.get_points(100, 7) == 10, db2.get_points(100, 7)
            print("CHECKIN_STREAK_CAP_OK (连签加成被封顶)")
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # 发言得积分：最短字数 + 每日上限（按**积分**夹紧）
        chat = PluginConfig({"activity": {
            "chat_points_enable": True, "chat_points_value": 2,
            "chat_points_cooldown": 0, "chat_points_daily_cap": 4,
            "chat_points_min_len": 4, "chat_first_bonus": 0,
            "newbie_bonus": 0}}).activity
        hc = ActivityHandle(PluginConfig({}), db)
        ev = _Ev(20)
        assert asyncio.run(hc.award_chat_points(ev, "嗯", chat)) == 0, "太短却给分了"
        assert asyncio.run(hc.award_chat_points(ev, "今天天气不错啊", chat)) == 2
        assert asyncio.run(hc.award_chat_points(ev, "第二条发言内容", chat)) == 2
        assert asyncio.run(hc.award_chat_points(ev, "第三条发言内容", chat)) == 0
        print("CHAT_POINTS_LIMITS_OK (最短字数与每日上限生效)")

        # 冷却
        cool = PluginConfig({"activity": {
            "chat_points_enable": True, "chat_points_value": 1,
            "chat_points_cooldown": 600, "chat_points_daily_cap": 999,
            "chat_points_min_len": 0}}).activity
        hc2 = ActivityHandle(PluginConfig({}), db)
        assert asyncio.run(hc2.award_chat_points(_Ev(30), "一句长一点的话", cool)) == 1
        assert asyncio.run(hc2.award_chat_points(_Ev(30), "再来一句长话试试", cool)) == 0
        print("CHAT_POINTS_COOLDOWN_OK (冷却生效)")

        # 新人礼包 + 每日首次发言（各只给一次）
        nb = PluginConfig({"activity": {
            "chat_points_enable": True, "chat_points_value": 1,
            "chat_points_cooldown": 0, "chat_points_daily_cap": 999,
            "chat_points_min_len": 0, "chat_first_bonus": 3,
            "newbie_bonus": 20}}).activity
        hc3 = ActivityHandle(PluginConfig({}), db)
        first = asyncio.run(hc3.award_chat_points(_Ev(40), "大家好我是新人", nb))
        second = asyncio.run(hc3.award_chat_points(_Ev(40), "我又说了一句", nb))
        assert first == 24, first          # 1 + 首次 3 + 新人 20
        assert second == 1, second         # 只剩基础分
        print(f"CHAT_FIRST_NEWBIE_OK (首次 +{first}，之后 +{second})")

        # 互动得分（投票/接龙共用每日上限）
        icfg = PluginConfig({"shop": {"enable": True},
                             "interact": {"vote_points": 5, "chain_points": 5,
                                          "interact_points_daily_cap": 5}})
        hi = InteractHandle(icfg, db)
        hi._votes["100"] = {"title": "t", "options": ["a", "b"], "votes": {},
                            "start": time.time(), "closed": False}
        msg = asyncio.run(hi.cast_vote(_Ev(50), "1"))
        assert "积分" in msg, msg
        assert db.get_points(100, 50) == 5, db.get_points(100, 50)
        hi._votes["100"]["votes"] = {}
        msg2 = asyncio.run(hi.cast_vote(_Ev(50), "2"))
        assert "积分" not in msg2, msg2
        print(f"INTERACT_POINTS_CAP_OK ({msg.splitlines()[-1]})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_games():
    """群内小游戏：猜数字 / 摇骰子 / 猜拳 / 押注。

    重点守三件最容易出事的事：
      1. **没有进行中的局时，纯数字消息绝不能被吃掉**（那等于抢话）；
      2. 猜数字有开局冷却与每日发奖上限（否则就是台无限发分机）；
      3. 押注**默认关闭**，开启后单次押注会被压到上限内。
    """
    import asyncio
    import shutil
    import tempfile
    import time

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.games import GamesHandle, GuessRound
    from astrbot_plugin_panshi.data import Storage

    class _Ev:
        bot = None

        def __init__(self, uid=7, gid=100):
            self._uid, self._gid = uid, gid

        def get_group_id(self):
            return self._gid

        def get_sender_id(self):
            return self._uid

        def get_self_id(self):
            return 99

        def get_sender_name(self):
            return f"用户{self._uid}"

    tmp = tempfile.mkdtemp(prefix="panshi_games_")
    try:
        db = Storage(tmp)
        cfg = PluginConfig({}).game
        h = GamesHandle(PluginConfig({}), db)
        ev = _Ev()

        # 1) 没有局时，纯数字必须放行（最关键的"不抢话"用例）
        assert asyncio.run(h.play(ev, "50", cfg)) is None, \
            "没有进行中的局，纯数字却被吃掉了"
        print("GAMES_NUMBER_PASS_THROUGH_OK (没开局时纯数字不抢话)")

        # 2) 开局
        start = asyncio.run(h.play(ev, "猜数字", cfg))
        assert "1~100" in start, start
        print(f"GAMES_GUESS_START_OK ({start.splitlines()[0]})")

        # 3) 冷却：刚开过不能再开
        again = asyncio.run(h.play(ev, "猜数字", cfg))
        assert ("已经有一局" in again) or ("秒后再来" in again), again
        print(f"GAMES_GUESS_COOLDOWN_OK ({again})")

        # 4) 猜错给大小提示
        h._round["100"] = GuessRound(answer=42, started=time.time())
        low = asyncio.run(h.play(_Ev(7), "10", cfg))
        high = asyncio.run(h.play(_Ev(8), "90", cfg))
        assert "太小" in low, low
        assert "太大" in high, high
        print(f"GAMES_GUESS_HINT_OK ({low} / {high})")

        # 5) 猜中发分（积分系统开着时）
        shop_on = PluginConfig({"shop": {"enable": True}})
        h_on = GamesHandle(shop_on, db)
        h_on._round["100"] = GuessRound(answer=42, started=time.time())
        win = asyncio.run(h_on.play(_Ev(7), "42", shop_on.game))
        assert "猜中" in win, win
        assert db.get_points(100, 7) > 0, win
        print(f"GAMES_GUESS_WIN_OK ({win.replace(chr(10), ' / ')})")

        # 6) 越界数字不响应（不报错、也不抢话）
        h._round["100"] = GuessRound(answer=42, started=time.time())
        assert asyncio.run(h.play(ev, "999", cfg)) is None, "越界数字被吃了"
        h._round.pop("100", None)
        print("GAMES_GUESS_OUT_OF_RANGE_OK (越界数字不响应)")

        # 7) 摇骰子 / 猜拳
        r1 = asyncio.run(h.play(ev, "摇骰子", cfg))
        assert "🎲" in r1 and "合计" in r1, r1
        r3 = asyncio.run(h.play(ev, "摇骰子 3", cfg))
        assert "摇了 3 个" in r3, r3
        rps = asyncio.run(h.play(ev, "猜拳石头", cfg))
        assert "石头" in rps and ("赢" in rps or "平局" in rps), rps
        usage = asyncio.run(h.play(ev, "猜拳", cfg))
        assert "猜拳石头" in usage, usage
        print(f"GAMES_DICE_RPS_OK ({r1.splitlines()[0]} / {rps})")

        # 8) 押注默认关闭
        closed = asyncio.run(h.play(ev, "押注 10", cfg))
        assert "没开启" in closed, closed
        print(f"GAMES_BET_OFF_BY_DEFAULT_OK ({closed})")

        # 9) 押注开启后：单次押注被压到上限内
        bet_cfg = PluginConfig({
            "shop": {"enable": True},
            "game": {"bet_enable": True, "bet_max": 20, "bet_daily_loss": 30},
        })
        hb = GamesHandle(bet_cfg, db)
        db.add_points(100, 7, 1000)
        out = asyncio.run(hb.play(ev, "押注999", bet_cfg.game))
        assert out and "999" not in out, out
        print(f"GAMES_BET_CAPPED_OK ({out.splitlines()[-1]})")

        # 10) 每日发奖局数上限：设成 1 局
        cap_cfg = PluginConfig({
            "shop": {"enable": True},
            "game": {"guess_reward": 5, "guess_daily_games": 1,
                     "guess_cooldown": 0},
        })
        h2 = GamesHandle(cap_cfg, db)
        asyncio.run(h2.play(_Ev(11), "猜数字", cap_cfg.game))
        h2._round["100"] = GuessRound(answer=7, started=time.time())
        first = asyncio.run(h2.play(_Ev(11), "7", cap_cfg.game))
        assert "猜中" in first and "发完" not in first, first
        asyncio.run(h2.play(_Ev(12), "猜数字", cap_cfg.game))
        h2._round["100"] = GuessRound(answer=7, started=time.time())
        second = asyncio.run(h2.play(_Ev(12), "7", cap_cfg.game))
        assert "发完" in second, second
        print(f"GAMES_DAILY_CAP_OK ({second.splitlines()[-1]})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_points_shared_across_groups():
    """积分跨群共用。

    用户确认的四条语义：
      ① 共用一份积分  ② 签到每天只能签一次  ③ 违规扣分跨群生效  ④ 历史从零重算
    另外守住一条：**只有积分和签到跨群，违规记录/发言/购买记录仍按群隔离**
    ——「一个群的违规不该跨群累计」和「积分可以跨群花」是两件事。
    """
    import shutil
    import tempfile

    from astrbot_plugin_panshi.data import Storage

    tmp = tempfile.mkdtemp(prefix="panshi_shared_")
    try:
        db = Storage(tmp)
        # 默认行为不能被改坏：每群各算各的
        db.add_points(100, 7, 30)
        db.add_points(200, 7, 5)
        assert db.get_points(100, 7) == 30, db.get_points(100, 7)
        assert db.get_points(200, 7) == 5, db.get_points(200, 7)
        print("POINTS_PER_GROUP_DEFAULT_OK (默认仍是每群各算各的)")

        # ④ 打开共用后从零重算
        db.set_points_shared(True)
        assert db.get_points(100, 7) == 0, "切换共用后应当从零重算"
        assert db.get_points(200, 7) == 0
        print("POINTS_SHARED_RESET_OK (切换后从零重算)")

        # ① 共用一份：A 群加分 B 群可见，B 群扣分 A 群同步
        db.add_points(100, 7, 50)
        assert db.get_points(100, 7) == 50
        assert db.get_points(200, 7) == 50, "在 A 群加的分 B 群没看到"
        db.add_points(200, 7, -20)
        assert db.get_points(100, 7) == 30, "在 B 群扣的分 A 群没同步"
        print("POINTS_SHARED_SAME_POOL_OK (A/B 群是同一份积分)")

        # ② 签到每天只算一次
        assert not db.has_checked_in(100, 7)
        db.set_checkin(100, 7)
        assert db.has_checked_in(200, 7), "在 A 群签到后 B 群还能再签"
        print("POINTS_SHARED_CHECKIN_ONCE_OK (签到每天全局只算一次)")

        # ③ 违规扣分跨群生效
        before = db.get_points(200, 7)
        db.add_points(100, 7, -10)
        assert db.get_points(200, 7) == before - 10, "扣分没有跨群生效"
        print("POINTS_SHARED_PENALTY_CROSS_OK (扣分跨群生效)")

        # 共用模式下排行为全服榜
        db.add_points(300, 8, 999)
        top = dict(db.top_points(100, 10))
        assert top.get("8") == 999, top
        assert top.get("7") == before - 10, top
        print(f"POINTS_SHARED_GLOBAL_RANK_OK (全服榜 {top})")

        # 其它数据仍按群隔离
        db.add_warning(100, 7, "刷屏")
        assert len(db.get_warnings(100, 7)) == 1
        assert len(db.get_warnings(200, 7)) == 0, "违规记录被跨群合并了"
        print("POINTS_SHARED_OTHER_DATA_PER_GROUP_OK (违规等仍按群隔离)")

        # 关掉后回到各自的分，历史没丢
        db.set_points_shared(False)
        assert db.get_points(100, 7) == 30, db.get_points(100, 7)
        assert db.get_points(200, 7) == 5, db.get_points(200, 7)
        print("POINTS_SHARED_OFF_RESTORES_OK (关掉后回到各自的分)")

        # 落盘往返：共用分本身要能读回来（共用模式由插件重新同步）
        db2 = Storage(tmp, points_shared=True)
        assert db2.get_points(100, 7) == 20, db2.get_points(100, 7)
        print("POINTS_SHARED_PERSIST_OK (重启后共用分仍在)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bare_word_shortcuts():
    """裸词快捷通道：整句精确才触发，句中夹着绝不触发。

    这是这个功能最容易出事的地方——一旦放宽成「包含即触发」，
    群里正常聊天就会被抢答。所以「不误触发」比「多命中」重要得多。
    """
    import inspect as _inspect
    import shutil
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.interact import BARE_ACTIONS, InteractHandle
    from astrbot_plugin_panshi.data import Storage

    tmp = tempfile.mkdtemp(prefix="panshi_bare_")
    try:
        h = InteractHandle(PluginConfig({}), Storage(tmp))

        hits = {
            "积分": "points", "我的积分": "points", "查积分": "points",
            "签到": "checkin", "打卡": "checkin",
            "积分排行": "rank", "排行榜": "rank", "发言排行": "rank_msg",
            "积分商城": "shop", "商城": "shop",
            "抽奖": "lottery", "我的": "self", "帮助": "help",
        }
        for text, want in hits.items():
            got = h.match_bare_word(text)
            assert got == want, f"「{text}」应命中 {want}，实际 {got}"
        print(f"BARE_WORD_HIT_OK ({len(hits)} 个裸词都命中)")

        noisy = {
            "积分！": "points", "积分。": "points", "积分呢": "points",
            "积分？": "points", "签到吧": "checkin", "@机器人 积分": "points",
            "  积分  ": "points",
        }
        for text, want in noisy.items():
            got = h.match_bare_word(text)
            assert got == want, f"「{text}」应命中 {want}，实际 {got}"
        print("BARE_WORD_NOISE_TOLERANT_OK (标点/语气词/@前缀都能归一化)")

        # 关键用例：正常聊天绝不能被抢答
        for text in ["我积分怎么还没到", "这个积分有什么用", "帮我看看积分",
                     "积分榜第一名是谁", "签到功能坏了吧", "我刚才签到成功了",
                     "大家积分都多少", "商城里有啥好东西", "能不能帮我查下积分",
                     "", "   ", "嗯"]:
            got = h.match_bare_word(text)
            assert got is None, f"正常聊天被误触发：「{text}」-> {got}"
        print("BARE_WORD_NO_HIJACK_OK (句子里的词不会误触发)")

        # 自定义关键词：合法动作生效，乱写的动作被忽略
        got = h.match_bare_word("宝石", "宝石 => points\n# 注释行\n乱写 => 没有这个动作")
        assert got == "points", got
        assert h.match_bare_word("乱写", "乱写 => 没有这个动作") is None
        print("BARE_WORD_CUSTOM_OK (自定义关键词生效，非法动作被忽略)")

        # 动作名必须都能在 main._run_bare_word 里找到实现
        from astrbot_plugin_panshi.main import PanshiPlugin

        src = _inspect.getsource(PanshiPlugin._run_bare_word)
        unwired = [a for a in BARE_ACTIONS if f'"{a}"' not in src]
        assert not unwired, f"这些动作在 _run_bare_word 里没有实现：{unwired}"
        print(f"BARE_WORD_ACTIONS_WIRED_OK ({len(BARE_ACTIONS)} 个动作都有实现)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_checkin_respects_switch():
    """「启用签到」必须真的能关掉签到。

    回归背景：`checkin_enable` 以前只被面板/自检读去「显示状态」，
    签到逻辑本身从不看它——关掉开关后 /签到 照样加分，是个假开关。
    """
    import asyncio
    import shutil
    import tempfile

    from astrbot_plugin_panshi.config import PluginConfig
    from astrbot_plugin_panshi.core.activity import ActivityHandle
    from astrbot_plugin_panshi.data import Storage

    class _Ev:
        bot = None

        def get_group_id(self):
            return 100

        def get_sender_id(self):
            return 3

        def get_self_id(self):
            return 99

        def get_sender_name(self):
            return "测试"

    tmp = tempfile.mkdtemp(prefix="panshi_checkin_")
    try:
        db = Storage(tmp)

        off = ActivityHandle(
            PluginConfig({"activity": {"checkin_enable": False}}), db)
        r = asyncio.run(off.checkin(_Ev()))
        assert "未开启" in r, r
        assert db.get_points(100, 3) == 0, "关掉签到却还是加了分"
        print(f"CHECKIN_SWITCH_OFF_OK ({r})")

        on = ActivityHandle(PluginConfig({
            "activity": {"checkin_enable": True, "checkin_points": 10,
                         "checkin_random_bonus": 0}}), db)
        r2 = asyncio.run(on.checkin(_Ev()))
        assert "签到成功" in r2, r2
        assert db.get_points(100, 3) == 10, db.get_points(100, 3)
        print(f"CHECKIN_SWITCH_ON_OK ({r2.replace(chr(10), ' / ')})")

        again = asyncio.run(on.checkin(_Ev()))
        assert "已经签到" in again, again
        print(f"CHECKIN_TWICE_OK ({again})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
