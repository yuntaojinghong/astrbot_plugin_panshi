"""本地规则意图解析：不依赖任何 LLM，把「人话」翻译成群管操作。

为什么需要它：
    此前自然语言指挥（如「禁言张三 10 分钟」）**只能**走 LLM，
    一旦 AstrBot 没有配置对话模型，就直接回「未找到 LLM 供应商」并
    把整条消息吞掉 —— 用户看到的是「没有 llm 服务商」。
    本模块提供纯正则/关键词的本地解析兜底，让常见指令在无 LLM 时依然可用。

设计原则：
    - **宁可不识别，不可误操作**：只解析足够明确的句式；
      模糊指代（"他""那个"）交给 LLM 或直接放弃，绝不瞎猜目标。
    - 输出格式与 :class:`core.intent.IntentParser` 完全一致，
      可直接交给 :class:`IntentExecutor` 执行。
"""

from __future__ import annotations

import re

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("panshi")

from ..utils import parse_duration

# ---------- 动作词表（长词优先，避免「全体禁言」被「禁言」抢先匹配） ----------
_WHOLE_BAN_WORDS = ("全体禁言", "全员禁言", "全禁", "全体闭嘴", "所有人闭嘴")
_WHOLE_HINTS = ("全体", "全员", "全群", "所有人", "每个人", "大家", "整个群")
_UNBAN_WORDS = ("解禁", "解除禁言", "取消禁言", "解除全体禁言", "取消全体禁言", "解除全员禁言")
_UNBAN_HINTS = ("解禁", "解除", "取消", "放开", "恢复", "开禁")
_BAN_WORDS = ("禁言", "闭嘴", "罚他", "把.*禁言")
_KICK_WORDS = ("踢出", "踢了", "踢掉", "飞出", "请出", "赶出", "移除")
_BLOCK_WORDS = ("拉黑", "踢出并拉黑", "封禁", "黑名单", "永久踢")
_RECALL_WORDS = ("撤回", "删了这条", "删除这条", "撤了")
_PURGE_WORDS = ("清屏", "净化", "清理聊天", "清理消息", "批量撤回")
_WARN_WORDS = ("警告", "记一笔", "记一次")
_QUERY_WARN_WORDS = ("违规记录", "查警告", "警告记录", "违规历史")
_CARD_WORDS = ("改名", "改名片", "改昵称", "修改名片")
_TITLE_WORDS = ("头衔", "称号")
_SET_ADMIN_WORDS = ("设为管理员", "上管", "给.*管理员", "任命管理")
_UNSET_ADMIN_WORDS = ("取消管理员", "下管", "撤销管理员", "撤管理")
_NOTICE_WORDS = ("发公告", "发布公告", "群公告")
_GROUP_NAME_WORDS = ("改群名", "修改群名", "设置群名")
_ESSENCE_WORDS = ("设为精华", "加精", "设精", "精华")
_CURFEW_WORDS = ("宵禁", "夜间禁言", "夜间全体禁言", "夜间模式")
_BANWORD_ADD_WORDS = ("加入违禁词", "添加违禁词", "加到违禁词", "屏蔽词加", "把.*加到违禁词")
_BANWORD_DEL_WORDS = ("删除违禁词", "移除违禁词", "从违禁词里删", "去掉违禁词")

# 中文数字 -> 阿拉伯数字（仅覆盖常见量级）
_CN_NUM = {
    "零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}

# 目标指代关键词（模糊指代 -> recent_offender）
_OFFENDER_HINTS = (
    "刚才那个", "刚刚那个", "刚才刷", "刚刚刷", "刚才发", "刚刚发",
    "那个刷屏的", "那个发广告的", "刷屏的", "发广告的", "捣乱的",
    "那个人", "这家伙", "这个人", "他", "她", "上一条", "楼上",
)
# 正则形式（更宽松）：刚才/刚刚 + 任意修饰 + 的/那人
_OFFENDER_RE = re.compile(r"(?:刚才|刚刚|刚刚那个|上一条|上面那个)[^，,。！!]{0,8}?(?:的|那人|那个人)")

# 起手称呼 / 礼貌前缀，解析前剥离
_PREFIX_RE = re.compile(
    r"^\s*(?:@\S+\s*|请|麻烦|帮忙|帮我|你|磐石|机器人|群管|管理|麻烦你|请你|帮我)\s*[，,、:：]?\s*"
)
_PREFIX_RE2 = re.compile(r"^(?:磐石|机器人|群管)[，,、:：]?\s*")


def _cn_to_int(text: str) -> int | None:
    """把「十」「二十三」这类中文数字转成 int；失败返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if "十" in text:
        parts = text.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    total = 0
    for ch in text:
        if ch in _CN_NUM:
            total = total * 10 + _CN_NUM[ch]
        else:
            return None
    return total or None


def _norm_duration(text: str) -> str:
    """把中文时长表达归一化成 parse_duration 能懂的写法。"""
    text = (text or "").strip()
    if not text:
        return text
    # 「十分钟」->「10分钟」，「半小时」->「30分钟」
    if "半" in text and "小时" in text:
        text = text.replace("半小时", "30分钟")
    if "小时" in text and not re.search(r"\d", text):
        m = re.search(r"([零一两二三四五六七八九十]+)\s*个?\s*小时", text)
        if m:
            n = _cn_to_int(m.group(1))
            if n is not None:
                text = text[: m.start()] + f"{n}小时" + text[m.end():]
    if "天" in text and not re.search(r"\d", text):
        m = re.search(r"([零一两二三四五六七八九十]+)\s*天", text)
        if m:
            n = _cn_to_int(m.group(1))
            if n is not None:
                text = text[: m.start()] + f"{n}天" + text[m.end():]
    # 「十分钟」「5分钟」这类
    def _sub(m):
        n = _cn_to_int(m.group(1))
        return f"{n}{m.group(2)}" if n is not None else m.group(0)

    text = re.sub(r"([零一两二三四五六七八九十]+)\s*(分钟|分|秒|个小时|小时|天)", _sub, text)
    return text


class LocalIntentParser:
    """纯本地规则解析器：把自然语言转成结构化意图。"""

    def __init__(self, config=None):
        self.cfg = config

    # ---------- 主入口 ----------
    def parse(self, event, message: str | None = None, arg_text: str = "") -> dict | None:
        """解析自然语言，返回意图 dict 或 None（无法识别）。

        返回值结构与本项目 ``IntentParser`` 保持一致：
        ``{"action": ..., "target": ..., "duration": ..., ...}``
        """
        text = (message if message is not None else getattr(event, "message_str", "")) or ""
        text = text.strip()
        if not text:
            return None

        # 去掉 @机器人 段与礼貌前缀
        text = re.sub(r"@\S+\s*", "", text).strip()
        text = _PREFIX_RE.sub("", text)
        text = _PREFIX_RE2.sub("", text)
        text = text.strip()
        if not text:
            return None

        # 目标来源：引用 / @（优先级最高，最可靠）
        target_from_ctx = self._target_from_event(event)

        # —— 按「特异性从高到低」匹配动作，避免误判 ——
        # 1) 全体禁言 / 解禁
        #    先判「全体 + 禁言」的任意组合（允许「把全体都禁言了」这类插入字）
        is_whole = any(w in text for w in _WHOLE_HINTS)
        is_unban = any(w in text for w in _UNBAN_HINTS)
        has_ban_verb = any(re.search(w, text) for w in _BAN_WORDS) or any(
            w in text for w in ("禁言", "闭嘴", "封嘴")
        )
        if is_whole and is_unban:
            return {"action": "whole_ban", "enable": False}
        if is_whole and has_ban_verb:
            return {"action": "whole_ban", "enable": True}
        if any(w in text for w in _UNBAN_WORDS) and is_whole:
            return {"action": "whole_ban", "enable": False}
        if any(w in text for w in _WHOLE_BAN_WORDS):
            # 「解除全体禁言」已在上面处理
            return {"action": "whole_ban", "enable": True}

        # 2) 宵禁
        if any(w in text for w in _CURFEW_WORDS):
            return self._parse_curfew(text)

        # 3) 违禁词
        if any(re.search(w, text) for w in _BANWORD_ADD_WORDS):
            word = self._extract_banword(text, add=True)
            return {"action": "banword_add", "content": word} if word else None
        if any(re.search(w, text) for w in _BANWORD_DEL_WORDS):
            word = self._extract_banword(text, add=False)
            return {"action": "banword_del", "content": word} if word else None

        # 4) 踢出并拉黑（要先于「踢出」判断）
        if any(w in text for w in _BLOCK_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "block", "target": tgt, "reason": ""} if tgt else None

        # 5) 踢出
        if any(w in text for w in _KICK_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "kick", "target": tgt, "reason": ""} if tgt else None

        # 6) 解禁（单人或引用，无「全体」字样）
        if any(w in text for w in _UNBAN_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "unban", "target": tgt} if tgt else None

        # 7) 禁言
        if any(re.search(w, text) for w in _BAN_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            if not tgt:
                return None
            dur_text = self._extract_duration_text(text)
            dur = parse_duration(dur_text, self._default_ban()) if dur_text else self._default_ban()
            return {"action": "ban", "target": tgt, "duration": dur}

        # 8) 撤回 / 清屏
        if any(w in text for w in _PURGE_WORDS):
            n = self._extract_count(text)
            return {"action": "purge", "count": n or 30}
        if any(w in text for w in _RECALL_WORDS):
            n = self._extract_count(text) or 1
            return {"action": "recall", "count": n}

        # 9) 警告 / 查违规
        if any(w in text for w in _QUERY_WARN_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "query_warn", "target": tgt}
        if any(w in text for w in _WARN_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "warn", "target": tgt, "reason": ""} if tgt else None

        # 10) 改名 / 头衔
        if any(w in text for w in _CARD_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            name = self._extract_new_name(text, _CARD_WORDS)
            if tgt and name:
                return {"action": "set_card", "target": tgt, "name": name}
            return None
        if any(w in text for w in _TITLE_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            title = self._extract_new_name(text, _TITLE_WORDS)
            if tgt and title:
                return {"action": "set_title", "target": tgt, "title": title}
            return None

        # 11) 管理员任免
        if any(re.search(w, text) for w in _UNSET_ADMIN_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "unset_admin", "target": tgt} if tgt else None
        if any(re.search(w, text) for w in _SET_ADMIN_WORDS):
            tgt = self._resolve(text, target_from_ctx, event)
            return {"action": "set_admin", "target": tgt} if tgt else None

        # 12) 公告 / 群名 / 精华
        if any(w in text for w in _NOTICE_WORDS):
            content = self._strip_words(text, _NOTICE_WORDS)
            return {"action": "notice", "content": content} if content else None
        if any(w in text for w in _GROUP_NAME_WORDS):
            name = self._strip_words(text, _GROUP_NAME_WORDS)
            return {"action": "set_name", "name": name} if name else None
        if any(w in text for w in _ESSENCE_WORDS):
            enable = not any(w in text for w in ("取消", "移除", "去掉", "删除"))
            return {"action": "essence", "enable": enable}

        return None

    # ---------- 目标解析 ----------
    def _target_from_event(self, event) -> str | None:
        """从事件的引用 / @ 中取出目标（最可靠）。"""
        # 引用消息
        try:
            from astrbot.api.message_components import Reply

            for seg in event.get_messages():
                if isinstance(seg, Reply):
                    sid = getattr(seg, "sender_id", None)
                    if sid:
                        return str(sid)
        except Exception:
            pass
        # @某人
        try:
            from astrbot.api.message_components import At

            for seg in event.get_messages():
                if isinstance(seg, At):
                    qq = getattr(seg, "qq", None)
                    if qq and str(qq) != "all":
                        return str(qq)
        except Exception:
            pass
        return None

    def _resolve(self, text: str, target_from_ctx: str | None, event) -> str | None:
        """解析操作目标：引用/@ > 显式 QQ 号 > 模糊指代。"""
        if target_from_ctx:
            return target_from_ctx
        # 显式 QQ 号
        m = re.search(r"(?<!\d)(\d{5,12})(?!\d)", text)
        if m:
            return m.group(1)
        # 模糊指代 -> recent_offender（由 context collector 提供）
        if _OFFENDER_RE.search(text):
            return "recent_offender"
        for hint in _OFFENDER_HINTS:
            if hint in text:
                return "recent_offender"
        # 「昵称」在文本里但没有明确对象 —— 交给 LLM 更稳妥，本地放弃
        m = re.search(r"「([^」]{1,20})」|《([^》]{1,20})》", text)
        if m:
            return (m.group(1) or m.group(2)).strip()
        return None

    # ---------- 时长 / 数量 ----------
    @staticmethod
    def _extract_duration_text(text: str) -> str:
        """从文本里抽出时长片段，如「10分钟」「2小时」「1天」。"""
        text = _norm_duration(text)
        m = re.search(
            r"(\d+\s*(?:分钟|分|小时|个小时|秒|天))|(\d+\s*(?:m|min|h|hr|d|s)\b)",
            text,
            re.IGNORECASE,
        )
        if m:
            return m.group(0)
        return ""

    @staticmethod
    def _extract_count(text: str) -> int | None:
        """抽出数量，如「清屏 20 条」。"""
        m = re.search(r"(\d+)\s*(?:条|个)", text)
        if m:
            return int(m.group(1))
        m = re.search(r"(?:撤回|清屏|净化)\s*(\d+)", text)
        if m:
            return int(m.group(1))
        return None

    @staticmethod
    def _extract_new_name(text: str, words: tuple) -> str:
        """抽出新昵称/头衔，如「给张三改名叫 阿伟」。"""
        for pat in (r"改(?:名|昵称|名片)?(?:为|成|叫|：|:)?\s*(.+)", r"[设更]为\s*(.+)"):
            m = re.search(pat, text)
            if m:
                val = m.group(1).strip().strip("「」\"'’‘“”")
                val = re.sub(r"^(?:叫|为|成)\s*", "", val)
                if val:
                    return val
        return ""

    # ---------- 违禁词 ----------
    @staticmethod
    def _extract_banword(text: str, add: bool) -> str:
        """抽出要增删的违禁词。"""
        # 「把XX加到违禁词」
        m = re.search(r"把\s*[「\"']?([^「」\"'，,。]{1,20}?)[」\"']?\s*(?:加入|添加|加到|放进)", text)
        if m:
            return m.group(1).strip()
        # 「加入违禁词 XX」「添加屏蔽词：XX」
        for kw in ("违禁词", "屏蔽词"):
            if kw in text:
                tail = text.split(kw, 1)[1]
                tail = re.sub(r"^[：:，,、\s]+", "", tail)
                tail = re.sub(r"(里|中|里面|当中|了|吧|。|！|!)+$", "", tail)
                tail = tail.strip().strip("「」\"'’‘“”")
                if tail:
                    return tail
                head = text.split(kw, 1)[0]
                head = re.sub(r"(删除|移除|去掉|加入|添加|加到|把)+$", "", head)
                head = head.strip().strip("「」\"'’‘“”")
                if head:
                    return head
        return ""

    # ---------- 宵禁 ----------
    def _parse_curfew(self, text: str) -> dict | None:
        intent: dict = {"action": "set_curfew"}
        if any(w in text for w in ("关闭", "关掉", "取消", "停用", "不开")):
            intent["enable"] = False
            return intent
        if any(w in text for w in ("开启", "打开", "开一下", "启用", "开启吧")):
            intent["enable"] = True
        # 提取时间 HH:MM 或「23点半」
        times = self._extract_times(text)
        if len(times) >= 2:
            intent["start"], intent["end"] = times[0], times[1]
        elif len(times) == 1:
            # 只有一个时间且带「到/至」-> 结束；否则开始
            if re.search(r"(到|至|直到)\s*" + re.escape(times[0]), text) or "早上" in text or "早晨" in text:
                intent["end"] = times[0]
            else:
                intent["start"] = times[0]
        if len(intent) == 1:
            return None
        return intent

    @staticmethod
    def _extract_times(text: str) -> list[str]:
        """把「23点」「23点半」「晚上11点半」「7:30」归一化成 HH:MM 列表。"""
        out: list[str] = []
        # HH:MM
        for m in re.finditer(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text):
            h, mi = int(m.group(1)), int(m.group(2))
            if 0 <= h <= 23 and 0 <= mi <= 59:
                out.append(f"{h:02d}:{mi:02d}")
        if out:
            return out
        # 「11点半」「23点」「11点30分」
        for m in re.finditer(r"([零一两二三四五六七八九十]+|\d{1,2})\s*[点时]\s*(半|([零一两二三四五六七八九十]+|\d{1,2})\s*分)?", text):
            raw_h = m.group(1)
            h = _cn_to_int(raw_h)
            if h is None:
                continue
            mi = 0
            if m.group(2) == "半":
                mi = 30
            elif m.group(3):
                mi = _cn_to_int(m.group(3)) or 0
            # 晚上/下午/凌晨 修正
            seg = text[max(0, m.start() - 4): m.start()]
            if ("晚上" in seg or "下午" in seg or "傍晚" in seg) and h < 12:
                h += 12
            if ("凌晨" in seg or "半夜" in seg) and h == 12:
                h = 0
            if 0 <= h <= 23 and 0 <= mi <= 59:
                out.append(f"{h:02d}:{mi:02d}")
        return out

    # ---------- 杂项 ----------
    @staticmethod
    def _strip_words(text: str, words: tuple) -> str:
        """去掉动作词，留下内容。"""
        for w in words:
            if w in text:
                head, _, tail = text.partition(w)
                tail = re.sub(r"^[：:，,、\s]+", "", tail)
                tail = re.sub(r"^(?:内容|是|为|叫)\s*", "", tail)
                tail = tail.strip().strip("「」\"'’‘“”")
                if tail:
                    return tail
                head = re.sub(r"(发|发布|改|修改|设置|设为|把)+$", "", head).strip()
                if head:
                    return head
        return ""

    def _default_ban(self) -> int:
        try:
            return int(self.cfg.default_ban_time)
        except Exception:
            return 60
