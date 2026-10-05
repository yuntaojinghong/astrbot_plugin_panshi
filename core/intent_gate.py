"""意图闸门：在「是否值得调用 LLM」这件事上做零成本前置判定。

## 为什么要这个模块

早期方案是「命中软信号词就交给模型」，问题是「帮我」「麻烦」「怎么办」这类
词在日常闲聊里出现频率极高，等于把大量废话塞给模型，token 白烧。

## 新思路：证据打分 + 信用额度

把「要不要调用模型」拆成**四个本地零成本闸门**，四道全过才允许碰模型：

1. **上下文佐证（context evidence）**
   「收拾一下」「管一下」这类要求之所以值得响应，是因为**群里真的在发生
   事情**（刷屏/广告/复读）。本地查最近的群消息统计就能判断，完全不用问模型。
   没有异常，这类话大概率只是聊天，直接放过。

2. **效果动词（effect verb）**
   区分「要求动作」和「陈述事实」：
   - 「张三是广告」     → 陈述，放过
   - 「把张三广告处理下」→ 要求，放行
   靠的是「动作动词 + 目标指代」的组合，而不是单个词。

3. **否定与闲聊否决**
   「别管他」「不用处理了」「开玩笑的」「算了」——出现否定语境直接否决。
   这类词单独看像软信号，实际语义是相反的。

4. **信用额度（credit bucket）**
   每群一个独立的 token 预算桶（默认 20 点/小时，按群配置可调）。
   通过前三道闸门才扣额度；额度耗尽就退回本地规则，不会让插件瘫痪。
   同时带**冷却**：同一群短时间内不重复调用。

再加上**意图缓存**：相同句子在 TTL 内重复出现，直接复用上一次结果。

最终效果：本地规则继续兜底常用指令，真正模糊的口语化指挥才有机会用上模型，
无效调用基本被挡在模型之前。
"""

from __future__ import annotations

import hashlib
import re
import time

from astrbot.api import logger


# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

# 「要求对方做事」的动作动词——注意是**要别人执行**的动词，不是描述状态。
_EFFECT_VERBS = [
    "管", "管管", "管一下", "处理", "处理下", "处理一下", "收拾", "整治",
    "治一下", "治治", "安排", "安排下", "清一下", "清理", "净化", "整整",
    "教训", "警告", "提一下", "盯一下", "看一下", "查一下", "查查",
]

# 祈使 / 请求语气（表达「我在让你做事」）
_REQUEST_MARKERS = [
    "帮我", "帮忙", "麻烦", "替我", "给我", "能不能", "可不可以", "可否",
    "可以帮", "请", "劳驾", "拜托", "求", "有没有人", "谁来",
]

# 目标指代（说明「对谁/对什么」动手，是要求而非陈述的关键特征）
_TARGET_MARKERS = [
    "这个人", "那个人", "刚才那个", "刚刚那个", "这个", "那个", "他", "她",
    "这人", "那人", "楼上", "楼下", "谁在", "是谁", "哪个", "有人",
]

# 否定语境：出现即否决（语义与「指挥」相反）。
#
# 注意不要把「别人」「特别」「别的不说」这类词误伤成否定——裸的「别」字在
# 「别人一直在刷屏，管一下」里与否定毫无关系，按子串匹配会直接把真实的
# 举报丢掉且不给任何回复。因此这里只保留多字词，并对「别」单独用正则，
# 要求它紧跟在动作词前才是否定。
_NEGATION = [
    "不用管", "不用处理", "不要处理", "别处理", "不用禁言", "不要禁言",
    "别禁言", "别踢", "不用踢", "不要踢", "算了", "随他", "随他们",
    "别理", "不理", "放过", "开玩笑", "逗你", "别在意", "没关系",
    "不需要", "不必", "无需", "取消",
]

# 「别/不要/不用/不必/无需 + 可选修饰 + 动作词」才算否定指令。
_NEGATION_RE = re.compile(
    r"(?:别|不要|不用|不必|无需|甭|请勿)\s*(?:再|去|给我|给他|把他|把她|把|帮忙|帮我)?\s*"
    r"(?:禁言|解禁|踢|拉黑|撤回|删|清理|清屏|净化|全禁|全体禁言|警告|改名|头衔|"
    r"上管|下管|公告|设精|精华|群名|处理|管|安排|宵禁|封)"
)

# 闲聊词（短句命中直接否决）
_CHITCHAT_WORDS = [
    "哈哈", "呵呵", "嘻嘻", "笑死", "晚安", "早安", "早上好", "中午好",
    "吃饭", "睡觉", "下班", "上班", "打游戏", "开黑", "溜了", "拜拜",
]

# 管理动作词——用来区分「管理指令」与「普通提问」。
#
# 用途：无管理权限的人 @机器人 时，要判断这条该拦还是该放。
# 「总结一下今天群里聊了什么」是普通提问，机器人应当正常回答；
# 「有本事你禁言我」带动作词，属于管理指令，应当明确回权限不足并消费事件，
# 否则消息会漏给 LLM，用户会看到「日志说已忽略，可它还是回话了」。
_COMMAND_VERBS = re.compile(
    r"(?:禁言|解禁|解除禁言|全禁|全体禁言|全员禁言|闭嘴|踢|踢出|拉黑|封禁|"
    r"撤回|撤了|删了这条|删除这条|清屏|净化|清理消息|批量撤回|"
    r"警告|记一笔|改名|改名片|改昵称|头衔|称号|上管|下管|设为管理员|"
    r"发公告|发布公告|群公告|改群名|修改群名|设精华|加精|精华|"
    r"宵禁|夜间禁言|签到|违禁词|加白名单|移出黑名单|黑名单|"
    r"开启全体|关闭全体)"
)

#: 明确的「要机器人执行动作」的动词。
#: 这些词即使出现在疑问句里，也仍然是在下指令（「能不能帮我禁言他？」），
#: 所以不受疑问语气豁免。
_STRONG_ACTION = re.compile(
    r"(?:禁言|解禁|踢出|踢了|踢掉|拉黑|撤回|撤了|封禁|宵禁|上管|下管|"
    r"设为管理员|改群名|改名片|改名|加精|设精华|全体禁言|全员禁言|全禁)"
)

#: 疑问 / 征询语气。
#:
#: 用途：把「在问关于某功能的问题」与「在指挥机器人做这件事」分开。
#: 用户实测反馈「只识关键词、提问和指令傻傻分不清」，根源就是
#: ``群公告`` 这类词做子串匹配——只要提到就当成指令。
#: 有了疑问标记，「你觉得群公告应该加上什么」就不会再被误判成指令。
_QUESTION_MARKERS = re.compile(
    r"(?:[?？]|吗|呢|吧\s*[?？]?$|"
    r"怎么|如何|怎样|为什么|为何|是不是|有没有|能不能|可不可以|可否|"
    r"该不该|要不要|需不需要|什么|哪些|哪个|多少|何时|几点|"
    r"觉得|认为|看法|意见|建议|评价|怎么办|好不好|行不行|"
    r"是什么|什么是|会不会|可不可行)"
)

#: 第一人称「受害 / 抱怨」标记。
#: 「我帮你说话你还撤回我消息啊」是在抱怨，不是在要求撤回。
#: 注意它**优先于**动作词判定：这里出现的「撤回」是在描述对方做过的事。
_COMPLAINT_MARKERS = re.compile(
    r"(?:你还|你怎么|为什么要|凭什么|干嘛|干啥|搞什么|"
    r"看不懂|不明白|没看懂|离谱|过分|无语)"
)

#: 裸话题词：只是提到某个功能，没有任何「要去做」的动作。
#:
#: 「群公告」单独发出来是名词，不是命令。用户实测反馈「只识关键词」，
#: 就是这类词做子串匹配导致的——提到功能名 ≠ 要执行该功能。
#: 这类消息被拦下会回「⛔ 权限不足」，而成员其实只是在说一个词。
#:
#: 只在**全文就等于这些词**（去掉标点后）时生效，避免误伤
#: 「群公告改成…」这种真指令。
#:
#: 注意不要把「签到」这类**本身就是指令**的词放进来——它既是功能名也是动作，
#: 单独发出来就是在要求签到。
_BARE_TOPICS = (
    "群公告", "公告", "群名", "精华", "头衔", "称号", "黑名单", "违禁词",
    "白名单", "宵禁", "全禁", "全体禁言", "全员禁言",
)


#: 插件功能关键词。命中它们、且消息像是在"问插件"，就由插件自己给准确答复。
#:
#: 回归背景：线上出现过机器人一本正经地说「积分插件还没启用，相关指令没注册
#: 到我这」，而同一条消息后面 /加分 500 明明成功了——因为 `/减分` 当时不是
#: 插件的指令，消息漏给了模型，模型就照着字面把插件状态编了一遍。
PLUGIN_FEATURE_WORDS = (
    "积分", "加分", "扣分", "减分", "给分", "签到", "商城", "商店",
    "抽奖", "奖池", "奖品", "排行", "小游戏", "磐石",
    "订单", "核销", "发放",
)

#: 疑问 / 征询 / 抱怨"没生效"的语气词
_QUESTION_HINTS = (
    "吗", "呢", "怎么", "如何", "能不能", "可不可以", "什么", "是啥", "多少",
    "有没有", "为啥", "为什么", "在哪", "哪里", "生效", "启用", "上线", "没用",
    "没反应", "不生效", "能用", "怎么回事",
)


def is_plugin_query(text: str, *, addressed: bool = False) -> bool:
    """这句话是不是「在问本插件的功能」（而不是在指挥群管做事）。

    只用于**兜底应答**：像指令、或明确在叫机器人，且提到了插件功能，本地
    又没认出具体意图——这时候让插件自己给准确答复，远比让模型去猜可靠。

    判定刻意保守（宁可漏答，不可抢话）：

    * 斜杠开头 —— 用户本来就是想敲指令，直接接管；
    * 或者在叫机器人，且消息很短 / 带疑问语气。

    这样「帮我总结一下大家对积分的看法」这类正常提问不会被抢走。
    """
    t = str(text or "").strip()
    if not t or not any(w in t for w in PLUGIN_FEATURE_WORDS):
        return False
    if t[:1] in ("/", "／"):
        return True
    if not addressed:
        return False
    if len(t) <= 12:
        return True
    return any(w in t for w in _QUESTION_HINTS)


def looks_like_command(text: str) -> bool:
    """粗略判断一句话是不是「在指挥群管做事」。

    只用于权限门槛的分流：命中就当管理指令处理（拦下并告知无权限），
    否则视为普通提问，放给 AstrBot 回答。

    宁可漏判也不能误判——把普通提问判成指令会让群友问不了问题，
    这个代价比偶尔漏放一条指令大得多。所以这里只认明确的动作词。

    判定顺序（越靠前越优先）：

    1. 第一人称抱怨 → 普通发言。这里出现的动作词是在描述对方做过的事：
       「我帮你说话你还撤回我消息啊」。
    2. 明确的动作词（禁言/撤回/踢…）→ 指令。即使是疑问句，
       「能不能帮我禁言他」也是在要求执行。
    3. 裸话题词 → 普通发言：「群公告」是名词，不是命令。
    4. 疑问 / 征询语气 → 提问。这条在问**这个功能**，不是要用它：
       「你觉得群公告应该加上什么」。
    5. 其余命中词表 → 指令。
    """
    if not text:
        return False
    t = str(text).strip()
    if not t:
        return False

    # 1. 抱怨优先
    if _COMPLAINT_MARKERS.search(t):
        return False

    # 2. 明确动作词：疑问语气也不豁免
    if _STRONG_ACTION.search(t):
        return True

    # 3. 裸话题词
    if t.strip("。.!！?？~～、,，") in _BARE_TOPICS:
        return False

    # 4. 疑问 / 征询语气
    if _QUESTION_MARKERS.search(t):
        return False

    # 5. 其余按词表判定
    return bool(_COMMAND_VERBS.search(t))

# 群内「异常迹象」词——用来做上下文佐证
_ANOMALY_WORDS = [
    "刷屏", "广告", "复读", "捣乱", "骂人", "引战", "乱发", "太吵", "吵死",
    "刷屏了", "发广告", "机器人", "烦死", "好吵", "禁言", "踢了", "管管",
]


# ---------------------------------------------------------------------------
# 闸门
# ---------------------------------------------------------------------------

class IntentGate:
    """决定一条消息是否值得交给 LLM 解析（全部本地判定，零 token）。"""

    #: 额度桶的默认容量（每群每小时）
    DEFAULT_BUDGET = 20
    #: 默认冷却秒数（同一群两次模型调用之间的最小间隔）
    DEFAULT_COOLDOWN = 8
    #: 意图缓存 TTL（秒）
    CACHE_TTL = 120

    def __init__(self, config, context=None):
        self.cfg = config
        self.ctx = context
        # {group_id: {"tokens": float, "ts": float}}
        self._buckets: dict[str, dict] = {}
        # {(group_id, text_hash): (ts, result)}
        self._cache: dict[tuple, tuple] = {}
        # {group_id: last_call_ts}
        self._last_call: dict[str, float] = {}
        # 统计（供面板展示，说明省了多少）
        self.stats = {
            "checked": 0,
            "passed": 0,
            "blocked_negation": 0,
            "blocked_chitchat": 0,
            "blocked_no_evidence": 0,
            "blocked_no_verb": 0,
            "blocked_budget": 0,
            "blocked_cooldown": 0,
            "cache_hit": 0,
        }
        self._last_cleanup = 0.0

    # ---------- 对外主入口 ----------

    def allow(
        self, group_id: str, text: str, has_action_word: bool = False
    ) -> tuple[bool, str]:
        """返回 ``(是否放行, 原因标签)``。

        原因标签用于日志与面板统计，便于用户理解为什么某句话没被处理。

        Args:
            group_id: 群号（用于每群独立的额度与冷却）。
            text: 待判定的消息文本。
            has_action_word: 文本里是否含明确的群管动作词。命中说明这多半是
                一条真实指挥而不是闲聊，可以跳过「无异常迹象」这道证据闸门
                （群里不一定正在出事，但「把张三禁言」显然是在下命令）。
                注意额度与冷却**依然生效**，所以不会因此烧掉更多 token。
        """
        self._cleanup()
        self.stats["checked"] += 1
        text = (text or "").strip()
        group_id = str(group_id or "")

        # --- 闸门 ③：否定与闲聊（先说否决，成本最低且优先） ---
        if self._is_negated(text):
            self.stats["blocked_negation"] += 1
            return False, "否定语境"
        if self._is_chitchat(text):
            self.stats["blocked_chitchat"] += 1
            return False, "日常闲聊"

        # --- 闸门 ①：上下文佐证 ---
        has_evidence = self._has_context_evidence(group_id, text)

        # --- 闸门 ②：效果动词 / 明确要求 ---
        has_effect = self._has_effect_request(text)

        # 有上下文佐证：只要带一点点要求语气就放行
        # 无上下文佐证：必须同时有「效果动词 + 目标指代」，否则大概率是闲聊。
        # 例外：文本里已有明确的群管动作词（如「禁言@张三」）时，本身就是在下命令，
        # 不该因为「群里当前没出事」而被丢掉。
        if has_evidence:
            if not (has_effect or self._has_request_marker(text) or has_action_word):
                self.stats["blocked_no_verb"] += 1
                return False, "无明确诉求"
        elif not has_action_word:
            if not (has_effect and self._has_target_marker(text)):
                self.stats["blocked_no_evidence"] += 1
                return False, "无异常迹象"

        # --- 闸门 ④：冷却 + 信用额度（按群独立） ---
        now = time.time()
        cooldown = self._cooldown(group_id)
        last = self._last_call.get(group_id, 0.0)
        if cooldown > 0 and now - last < cooldown:
            self.stats["blocked_cooldown"] += 1
            return False, "冷却中"

        if not self._spend(group_id, now):
            self.stats["blocked_budget"] += 1
            return False, "额度已用尽"

        self._last_call[group_id] = now
        self.stats["passed"] += 1
        return True, "放行"

    # ---------- 缓存 ----------

    def cached(self, group_id: str, text: str):
        """命中缓存则返回上次结果，否则 None（过期结果会被清掉）。"""
        key = (group_id, self._hash(text))
        hit = self._cache.get(key)
        if not hit:
            return None
        ts, result = hit
        if time.time() - ts > self.CACHE_TTL:
            self._cache.pop(key, None)
            return None
        self.stats["cache_hit"] += 1
        return result

    def remember(self, group_id: str, text: str, result) -> None:
        """记住一次解析结果，短时间内相同句子不再重复调用模型。"""
        self._cache[(group_id, self._hash(text))] = (time.time(), result)

    @staticmethod
    def _hash(text: str) -> str:
        return hashlib.md5((text or "").strip().encode("utf-8")).hexdigest()

    # ---------- 信用额度 ----------

    def _smart_for(self, group_id: str | None = None) -> dict:
        """取该群视角的 smart 配置（额度与冷却按群独立计费）。"""
        try:
            if group_id:
                return self.cfg.for_group(group_id).smart
        except Exception:
            pass
        return self.cfg.smart

    def _budget(self, group_id: str | None = None) -> int:
        try:
            val = int(
                self._smart_for(group_id).get("intent_budget", self.DEFAULT_BUDGET)
            )
        except Exception:
            val = self.DEFAULT_BUDGET
        return max(0, val)

    def _cooldown(self, group_id: str | None = None) -> int:
        try:
            val = int(
                self._smart_for(group_id).get("intent_cooldown", self.DEFAULT_COOLDOWN)
            )
        except Exception:
            val = self.DEFAULT_COOLDOWN
        return max(0, val)

    def _spend(self, group_id: str, now: float) -> bool:
        """令牌桶：按时间匀速回填，消费 1 点。返回是否成功。"""
        capacity = self._budget(group_id)
        if capacity <= 0:
            return False  # 额度为 0 表示不花模型（退回本地规则）
        bucket = self._buckets.get(group_id)
        if bucket is None:
            bucket = {"tokens": float(capacity), "ts": now}
            self._buckets[group_id] = bucket

        # 回填：每小时补满 capacity
        elapsed = max(0.0, now - bucket["ts"])
        refill = elapsed * (capacity / 3600.0)
        bucket["tokens"] = min(float(capacity), bucket["tokens"] + refill)
        bucket["ts"] = now

        if bucket["tokens"] >= 1.0:
            bucket["tokens"] -= 1.0
            return True
        return False

    def budget_left(self, group_id: str) -> int:
        """当前剩余额度（整数，供面板/命令展示）。"""
        bucket = self._buckets.get(str(group_id))
        capacity = self._budget(group_id)
        if not bucket:
            return capacity
        now = time.time()
        elapsed = max(0.0, now - bucket["ts"])
        tokens = min(float(capacity), bucket["tokens"] + elapsed * (capacity / 3600.0))
        return int(tokens)

    # ---------- 各闸门实现 ----------

    def _has_context_evidence(self, group_id: str, text: str) -> bool:
        """最近的群消息里是否真有异常迹象。

        两个来源：
        1. 上下文里最近若干条消息命中异常词；
        2. 当前这句话本身就在描述异常（"有人在刷屏"）。
        """
        if any(w in text for w in _ANOMALY_WORDS):
            return True
        if not self.ctx or not group_id:
            return False
        try:
            rows = self.ctx.recent(group_id, 12)
        except Exception:
            return False
        if not rows:
            return False
        now = time.time()
        for r in rows:
            # 只看最近 3 分钟
            if now - float(r.get("ts", 0)) > 180:
                continue
            body = str(r.get("text", ""))
            if any(w in body for w in _ANOMALY_WORDS):
                return True
        # 同一人短时间高频发言也算异常
        recent = [r for r in rows if now - float(r.get("ts", 0)) <= 60]
        if len(recent) >= 6:
            counter: dict[str, int] = {}
            for r in recent:
                uid = str(r.get("user_id", ""))
                counter[uid] = counter.get(uid, 0) + 1
            if counter and max(counter.values()) >= 5:
                return True
        return False

    @staticmethod
    def _has_effect_request(text: str) -> bool:
        """是否包含「要求执行动作」的动词。"""
        return any(v in text for v in _EFFECT_VERBS)

    @staticmethod
    def _has_request_marker(text: str) -> bool:
        return any(m in text for m in _REQUEST_MARKERS)

    @staticmethod
    def _has_target_marker(text: str) -> bool:
        """是否指明了「对谁动手」——这是要求 vs 陈述的关键区分。"""
        if any(m in text for m in _TARGET_MARKERS):
            return True
        # @ 某人 / 引用 / 显式 QQ 号
        if re.search(r"\d{5,12}", text):
            return True
        return False

    @staticmethod
    def _is_negated(text: str) -> bool:
        if _NEGATION_RE.search(text):
            return True
        return any(n in text for n in _NEGATION)

    @staticmethod
    def _is_chitchat(text: str) -> bool:
        # 长句里出现"哈哈"不算闲聊；只有短句才判定
        if len(text) > 15:
            return False
        return any(w in text for w in _CHITCHAT_WORDS)

    # ---------- 维护 ----------

    def _cleanup(self) -> None:
        now = time.time()
        if now - self._last_cleanup < 120:
            return
        self._last_cleanup = now
        # 清过期缓存
        for key in list(self._cache.keys()):
            ts, _ = self._cache[key]
            if now - ts > self.CACHE_TTL:
                self._cache.pop(key, None)
        # 清长期不活跃的额度桶
        for gid in list(self._buckets.keys()):
            if now - self._buckets[gid]["ts"] > 7200:
                self._buckets.pop(gid, None)
        for gid in list(self._last_call.keys()):
            if now - self._last_call[gid] > 7200:
                self._last_call.pop(gid, None)

    def describe(self) -> str:
        """人类可读的统计（供 /自检 或面板展示）。"""
        s = self.stats
        saved = (
            s["blocked_negation"]
            + s["blocked_chitchat"]
            + s["blocked_no_evidence"]
            + s["blocked_no_verb"]
            + s["blocked_budget"]
            + s["blocked_cooldown"]
            + s["cache_hit"]
        )
        total = s["checked"] or 1
        return (
            f"共检查 {s['checked']} 条，放行 {s['passed']} 条，"
            f"拦截 {saved} 条（省下约 {saved} 次模型调用，"
            f"拦截率 {saved / total * 100:.0f}%）\n"
            f"明细：闲聊 {s['blocked_chitchat']} · 否定 {s['blocked_negation']} · "
            f"无异常 {s['blocked_no_evidence']} · 无诉求 {s['blocked_no_verb']} · "
            f"冷却 {s['blocked_cooldown']} · 额度 {s['blocked_budget']} · "
            f"缓存命中 {s['cache_hit']}"
        )
