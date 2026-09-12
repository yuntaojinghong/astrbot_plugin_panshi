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


def main():
    main_mod = importlib.import_module("astrbot_plugin_panshi.main")
    print("MAIN_IMPORTED_OK")
    inst = main_mod.PanshiPlugin(None, {})
    print("PLUGIN_INSTANTIATED_OK")

    cmds = sorted(n for n in dir(inst) if n.startswith("cmd_"))
    print(f"COMMANDS: {len(cmds)}")
    for c in cmds:
        print("  ", c)

    # 校验核心模块可用
    assert inst.normal and inst.guard and inst.intent and inst.executor
    print("HANDLES_OK")

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

    print("ALL_SELFTEST_PASS")


if __name__ == "__main__":
    main()
