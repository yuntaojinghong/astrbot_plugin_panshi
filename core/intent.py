"""智能意图识别：把自然语言翻译成结构化的群管操作。

混合模式：
1. 明确指令（/禁言 ...）由 main.py 的指令通道直接处理，不经过这里。
2. 仅当消息"像在指挥机器人"时（@机器人 / 含动作词 / 以称呼开头），
   才调用 LLM 解析意图，避免浪费 token。
"""

from __future__ import annotations

import json
import re

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

# 触发智能识别的动作关键词
_ACTION_WORDS = [
    "禁言", "解禁", "踢", "拉黑", "撤回", "删了", "删除", "清理", "清屏", "净化",
    "全禁", "全体禁言", "闭嘴", "警告", "改名", "改头衔", "头衔", "上管", "下管",
    "公告", "设精", "精华", "群名", "举报", "处理", "管管", "管一下", "安排",
    "宵禁", "夜间禁言", "违禁词",
]

# LLM 输出的意图 -> 合法动作
VALID_ACTIONS = {
    "ban", "unban", "kick", "block", "recall", "purge", "whole_ban",
    "warn", "set_card", "set_title", "set_admin", "unset_admin",
    "notice", "set_name", "essence", "query_warn", "set_curfew",
    "banword_add", "banword_del", "none",
}

SYSTEM_PROMPT = """你是一个 QQ 群管理助理的意图解析器。请把用户的话翻译成结构化 JSON 操作。

可用动作 (action) 及参数：
- ban           禁言，参数: target(QQ号/名字), duration(秒)
- unban         解禁，参数: target
- kick          踢出，参数: target, reason(可选)
- block         踢出并拉黑，参数: target, reason(可选)
- recall        撤回消息，参数: count(数量,默认1), 若有引用则撤回被引用消息
- purge         批量撤回最近消息，参数: count
- whole_ban     全体禁言，参数: enable(true/false)
- warn          警告，参数: target, reason(可选)
- set_card      改群名片，参数: target, name(新昵称)
- set_title     设头衔，参数: target, title
- set_admin     设管理员，参数: target
- unset_admin   取消管理员，参数: target
- notice        发群公告，参数: content
- set_name      改群名，参数: name
- essence       设精华，参数: enable(true/false)
- query_warn    查违规记录，参数: target
- set_curfew    设置宵禁（夜间自动全体禁言），参数: enable(true/false，可选), start(开始时间), end(结束时间)
- banword_add   添加违禁词，参数: content(要添加的词)
- banword_del   删除违禁词，参数: content(要删除的词)
- none          无法识别或不需要操作

解析 target 时的规则（重要）：
- 如果用户引用了某条消息，target 填 "reply"。
- 如果用户 @ 了某人，target 填 "at"。
- 如果用户明确说了 QQ 号，target 填该数字。
- 如果用户用名字/昵称指代（如"张三""那个发广告的"），target 填该名字原文。
- 如果指代模糊（"他""那个刷屏的"），target 填 "recent_offender" 或保留原描述。

解析 set_curfew 时的规则：
- 用户提到宵禁/夜间禁言的开关或时段设置时使用；普通"全体禁言"用 whole_ban 而不是 set_curfew。
- 时间一律归一化为 24 小时制 "HH:MM"（补零），如"晚上十一点半"->"23:30"，"7点"->"07:00"，"23点半"->"23:30"。
- 只改时间没提开关 -> 省略 enable；只说"开启/关闭宵禁" -> 只给 enable。
- 用户说"到早上七点"这种只给了结束时间 -> 只填 end。

解析违禁词操作的规则：
- "把XX加进违禁词/屏蔽词" -> banword_add；"把XX从违禁词里删掉/移除" -> banword_del。
- XX 原样放入 content，去掉"加进违禁词"等动词短语，只留词本身。

只输出 JSON，不要任何解释。格式：
{"action": "...", "target": "...", "duration": 秒数, "reason": "...", "content": "...", "name": "...", "title": "...", "count": 数字, "enable": true/false, "start": "HH:MM", "end": "HH:MM"}

没有的参数省略。"""


class IntentParser:
    """调用 LLM 解析自然语言意图。"""

    def __init__(self, config, storage, context):
        self.cfg = config
        self.db = storage
        self.ctx = context
        self._last_error: str = ""

    @property
    def last_error(self) -> str:
        """最近一次解析失败的原因（供面板展示）。"""
        return self._last_error

    def should_trigger(self, event, message: str) -> bool:
        """判断是否值得走智能识别（省 token）。"""
        smart = self.cfg.smart
        if not smart.get("smart_enable", True):
            return False

        text = (message or "").strip()
        if not text:
            return False

        # 已经是指令（以 / 或命令前缀开头）的不走这里
        if text.startswith("/") or text.startswith("／"):
            return False

        # @了机器人
        if smart.get("trigger_on_at", True) and self._at_bot(event):
            return True

        # 以机器人称呼开头
        for name in smart.get("bot_names", []) or []:
            if name and text.startswith(str(name)):
                return True
            if name and str(name) in text[: len(str(name)) + 2]:
                return True

        # 含管理动作词
        if smart.get("trigger_on_keyword", True):
            for w in _ACTION_WORDS:
                if w in text:
                    return True

        return False

    async def parse(self, event) -> dict | None:
        """调用 LLM 解析意图，返回结构化 dict（失败返回 None）。"""
        message = (getattr(event, "message_str", "") or "").strip()
        if not message:
            return None

        group_id = str(event.get_group_id())

        # 组装上下文
        history = self.ctx.format_for_llm(group_id, 15)
        reply_info = self._reply_info(event)
        at_info = self._at_info(event)
        sender = f"{self._safe(event.get_sender_name)}({self._safe(event.get_sender_id)})"

        user_prompt = (
            f"【最近群聊记录】\n{history}\n\n"
            f"【当前这条消息】\n"
            f"发送者: {sender}\n"
            f"内容: {message}\n"
            f"{reply_info}\n"
            f"{at_info}\n\n"
            f"请解析出要执行的操作。"
        )

        try:
            provider = await self._get_provider(event)
            if provider is None:
                # 详细原因已在 _get_provider 里打了日志
                self._last_error = "未找到可用的 LLM 供应商"
                return None

            resp = await provider.text_chat(
                prompt=user_prompt,
                system_prompt=SYSTEM_PROMPT,
            )
            text = getattr(resp, "completion_text", None) or str(resp)
            self._last_error = ""
            return self._extract_json(text)
        except Exception as e:
            self._last_error = f"意图解析失败: {e}"
            logger.error(f"[磐石] 意图解析失败: {e}")
            return None

    async def _get_provider(self, event):
        """按「指定 ID → 会话默认 → 全局第一个」的顺序解析供应商。

        每一层都单独 try，并把真实原因写进日志，便于排查「未找到供应商」。
        """
        ctx = None
        try:
            ctx = event.get_context()
        except Exception as e:
            logger.warning(f"[磐石] 无法获取 AstrBot 上下文: {e}")
            return None

        provider_id = (self.cfg.smart.get("smart_provider_id", "") or "").strip()

        # 1) 配置里指定的供应商 ID
        if provider_id:
            try:
                prov = ctx.get_provider_by_id(provider_id=provider_id)
            except TypeError:
                # 老版本可能不接受关键字参数
                prov = ctx.get_provider_by_id(provider_id)
            except Exception as e:
                logger.warning(f"[磐石] 按 ID 取供应商失败(id={provider_id}): {e}")
                prov = None
            if prov is not None:
                return prov
            logger.warning(
                f"[磐石] 配置的供应商 ID「{provider_id}」不存在，"
                f"将回退到当前默认供应商。请到面板「智能识别」里重新选择。"
            )

        # 2) 当前会话使用的供应商
        try:
            prov = await ctx.get_using_provider_async(
                umo=getattr(event, "unified_msg_origin", None)
            )
            if prov is not None:
                return prov
        except Exception as e:
            logger.warning(f"[磐石] 获取会话默认供应商失败: {e}")

        # 3) 兜底：列表里第一个对话供应商
        try:
            all_providers = ctx.get_all_providers() or []
            if all_providers:
                logger.info(
                    f"[磐石] 会话未设置默认模型，回退到第一个可用供应商"
                    f"（共 {len(all_providers)} 个）"
                )
                return all_providers[0]
        except Exception as e:
            logger.warning(f"[磐石] 枚举供应商列表失败: {e}")

        logger.warning(
            "[磐石] 未找到任何可用的对话模型供应商。"
            "请在 AstrBot「服务提供商」页添加一个对话模型，"
            "或在插件面板「智能识别」里指定供应商。"
        )
        return None

    # ---------- 解析输出 ----------
    @staticmethod
    def _extract_json(text: str) -> dict | None:
        if not text:
            return None
        # 去除 markdown 代码块
        text = re.sub(r"```(?:json)?", "", text).strip("` \n")
        # 找第一个 { 到最后一个 }
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        action = data.get("action")
        if action not in VALID_ACTIONS:
            return None
        return data

    # ---------- 触发判断辅助 ----------
    @staticmethod
    def _at_bot(event) -> bool:
        try:
            from astrbot.api.message_components import At

            self_id = str(event.get_self_id()) if hasattr(event, "get_self_id") else ""
            for seg in event.get_messages():
                if isinstance(seg, At):
                    qq = str(getattr(seg, "qq", ""))
                    if qq == "all":
                        continue
                    if not self_id or qq == self_id:
                        return True
        except Exception:
            pass
        return False

    @staticmethod
    def _reply_info(event) -> str:
        try:
            from astrbot.api.message_components import Reply

            for seg in event.get_messages():
                if isinstance(seg, Reply):
                    sid = getattr(seg, "sender_id", "")
                    return f"【引用消息】被引用者QQ: {sid}"
        except Exception:
            pass
        return ""

    @staticmethod
    def _at_info(event) -> str:
        try:
            from astrbot.api.message_components import At

            ats = [str(getattr(s, "qq", "")) for s in event.get_messages() if isinstance(s, At)]
            ats = [a for a in ats if a and a != "all"]
            if ats:
                return f"【@对象】{', '.join(ats)}"
        except Exception:
            pass
        return ""

    @staticmethod
    def _safe(func):
        try:
            return func() or ""
        except Exception:
            return ""
