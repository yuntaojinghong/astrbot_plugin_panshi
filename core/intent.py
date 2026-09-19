"""智能意图识别：把自然语言翻译成结构化的群管操作。

混合模式：
1. 明确指令（/禁言 ...）由 main.py 的指令通道直接处理，不经过这里。
2. 明确喊话（@机器人 / 以称呼开头 / 含动作词）直接放行给 LLM。
3. 其余消息先过 :mod:`.intent_gate` 的意图闸门（四个本地零成本判定），
   通过后才调用 LLM，避免把闲聊灌给模型白烧 token。
"""

from __future__ import annotations

import json
import re

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

# 触发智能识别的动作关键词（明确的群管动作）
_ACTION_WORDS = [
    "禁言", "解禁", "踢", "拉黑", "撤回", "删了", "删除", "清理", "清屏", "净化",
    "全禁", "全体禁言", "闭嘴", "警告", "改名", "改头衔", "头衔", "上管", "下管",
    "公告", "设精", "精华", "群名", "举报", "处理", "管管", "管一下", "安排",
    "宵禁", "夜间禁言", "违禁词",
]

# 「意图型」软信号（仅作为闸门缺失时的兜底判定使用）。
# 正常路径下由 IntentGate 做更精确的四道闸门判定，不再依赖这张词表。
_INTENT_HINTS = [
    # 要求 / 祈使
    "帮我", "帮忙", "麻烦", "能不能", "可不可以", "可以帮", "给我", "替我",
    # 指向违规现象（人话描述，不含动作词）
    "刷屏", "广告", "复读", "捣乱", "骂人", "引战", "乱发", "太吵", "安静",
    "收拾", "整治", "治一下", "有人", "这个人", "那个人", "刚才那个",
    # 疑问式指挥
    "怎么办", "该不该", "要不要", "处理一下", "看一下", "管一下",
    # 口语时长（"禁他十分钟"这类）
    "分钟", "小时", "永久",
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

    def __init__(self, config, storage, context, gate=None):
        self.cfg = config
        self.db = storage
        self.ctx = context
        # 意图闸门（IntentGate）：决定是否值得花 token，由 main.py 注入。
        # 为 None 时 should_trigger 退回宽松的软信号判定。
        self.gate = gate
        self._last_error: str = ""

    @property
    def last_error(self) -> str:
        """最近一次解析失败的原因（供面板展示）。"""
        return self._last_error

    def should_trigger(self, event, message: str) -> bool:
        """判断是否值得走智能识别（省 token）。

        三级判定，越靠前越便宜：

        **A. 明确指挥（免费放行）**
        ``@机器人`` / 以机器人称呼开头 / 含标准动作词 —— 这些几乎不可能是
        闲聊，直接放行，不再做额外检查。

        **B. 意图闸门（免费拦截）**
        其余消息交给 :class:`IntentGate` 做四个本地闸门判定：
        上下文佐证 → 效果动词 → 否定/闲聊否决 → 冷却与信用额度。
        全部零成本，通过后才允许调用模型。这是「听懂人话」与
        「不烧 token」之间的平衡点。

        **C. 兜底**
        闸门不可用时（未注入）退回旧的软信号宽松判定，保证不会因为
        闸门缺失而完全听不懂人话。
        """
        smart = self.cfg.smart
        if not smart.get("smart_enable", True):
            return False

        text = (message or "").strip()
        if not text:
            return False

        # 已经是指令（以 / 或命令前缀开头）的不走这里
        if text.startswith("/") or text.startswith("／"):
            return False

        at_bot = self._at_bot(event)

        # ---------- A. 明确指挥：直接放行 ----------
        if smart.get("trigger_on_at", True) and at_bot:
            return True

        for name in smart.get("bot_names", []) or []:
            name = str(name or "")
            if name and text.startswith(name):
                return True
            if name and name in text[: len(name) + 2]:
                return True

        if smart.get("trigger_on_keyword", True):
            for w in _ACTION_WORDS:
                if w in text:
                    return True

        # ---------- B. 意图闸门 ----------
        if smart.get("trigger_on_intent", True) and self.gate is not None:
            try:
                group_id = str(event.get_group_id())
            except Exception:
                group_id = ""
            allowed, reason = self.gate.allow(group_id, text)
            if not allowed:
                # AstrBot 的 logger 实现不一定有 debug（部分版本只有 info 以上），
                # 用 getattr 兜底，避免因为日志级别缺失而打断主流程。
                _dbg = getattr(logger, "debug", None)
                if callable(_dbg):
                    try:
                        _dbg(f"[磐石] 意图闸门拦截（{reason}）：{text[:40]}")
                    except Exception:
                        pass
            return allowed

        # ---------- C. 兜底：闸门缺失时用宽松软信号 ----------
        if smart.get("trigger_on_intent", True):
            hits = sum(1 for w in _INTENT_HINTS if w in text)
            if hits >= 2 or (hits == 1 and len(text) >= 4):
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

        设计目标：**默认就用 AstrBot 里配好的模型**，用户不需要额外配置。
        每一层都单独 try，并把真实原因写进日志 + ``last_error``，
        便于排查「为什么听不懂人话」。
        """
        ctx = None
        try:
            ctx = event.get_context()
        except Exception:
            ctx = None
        if ctx is None:
            ctx = getattr(self.cfg, "context", None)
        if ctx is None:
            self._last_error = "拿不到 AstrBot 上下文"
            logger.warning("[磐石] 无法获取 AstrBot 上下文，无法解析模型")
            return None

        umo = getattr(event, "unified_msg_origin", None)
        tried = []

        # 1) 面板里显式指定的供应商 ID（优先级最高，用户意图明确）
        provider_id = (self.cfg.smart.get("smart_provider_id", "") or "").strip()
        if provider_id:
            prov = self._provider_by_id(ctx, provider_id, tried)
            if prov is not None:
                logger.info(f"[磐石] 使用面板指定的模型：{provider_id}")
                return prov

        # 2) 当前会话正在使用的模型（群里 /model 切换过就走这里）
        prov = await self._provider_for_session(ctx, umo, tried)
        if prov is not None:
            logger.info("[磐石] 使用当前会话设置的模型")
            return prov

        # 3) 兜底：全局第一个可用对话模型
        prov = self._provider_first(ctx, tried)
        if prov is not None:
            logger.info("[磐石] 会话未指定模型，使用全局第一个可用模型")
            return prov

        # 全部失败：把真实原因交给上层展示，而不是笼统的「未找到供应商」
        self._last_error = (
            "没有找到可用的对话模型（" + "；".join(tried) + "）"
            if tried
            else "没有找到可用的对话模型"
        )
        logger.warning(
            "[磐石] 未找到任何可用的对话模型。请到 AstrBot「服务提供商」页"
            f"添加一个对话模型。尝试记录：{tried}"
        )
        return None

    # ---------- 供应商解析的三级实现 ----------
    @staticmethod
    def _provider_by_id(ctx, provider_id: str, tried: list):
        """按 ID 取供应商；兼容不接收关键字参数的旧版本。"""
        for call in (
            lambda: ctx.get_provider_by_id(provider_id=provider_id),
            lambda: ctx.get_provider_by_id(provider_id),
        ):
            try:
                prov = call()
                if prov is not None:
                    return prov
                tried.append(f"指定ID「{provider_id}」未命中")
                return None
            except TypeError:
                continue
            except Exception as e:
                tried.append(f"指定ID「{provider_id}」取用失败: {e}")
                logger.warning(f"[磐石] 按 ID 取模型失败(id={provider_id}): {e}")
                return None
        tried.append(f"指定ID「{provider_id}」接口不兼容")
        return None

    @staticmethod
    async def _provider_for_session(ctx, umo, tried: list):
        """取当前会话使用的供应商。"""
        for call in (
            lambda: ctx.get_using_provider_async(umo=umo),
            lambda: ctx.get_using_provider_async(),
        ):
            try:
                prov = await call()
                if prov is not None:
                    return prov
                tried.append("会话未设置默认模型")
                return None
            except TypeError:
                continue
            except Exception as e:
                tried.append(f"取会话模型失败: {e}")
                logger.warning(f"[磐石] 获取会话默认模型失败: {e}")
                return None
        tried.append("会话模型接口不兼容")
        return None

    @staticmethod
    def _provider_first(ctx, tried: list):
        """兜底：取全局第一个可用对话模型。"""
        try:
            all_providers = ctx.get_all_providers() or []
        except Exception as e:
            tried.append(f"枚举模型列表失败: {e}")
            logger.warning(f"[磐石] 枚举模型列表失败: {e}")
            return None
        if all_providers:
            return all_providers[0]
        tried.append("AstrBot 未配置任何模型")
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
