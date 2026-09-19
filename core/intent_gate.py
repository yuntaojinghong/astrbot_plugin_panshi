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

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")


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

# 否定语境：出现即否决（语义与「指挥」相反）
_NEGATION = [
    "别管", "别动", "不用管", "不用处理", "不要处理", "别处理", "不用", "不要",
    "别", "算了", "随他", "随他们", "别理", "不理", "放过", "开玩笑", "逗你",
    "假的", "别在意", "没事", "没关系",
]

# 闲聊词（短句命中直接否决）
_CHITCHAT_WORDS = [
    "哈哈", "呵呵", "嘻嘻", "笑死", "晚安", "早安", "早上好", "中午好",
    "吃饭", "睡觉", "下班", "上班", "打游戏", "开黑", "溜了", "拜拜",
]

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

    def allow(self, group_id: str, text: str) -> tuple[bool, str]:
        """返回 ``(是否放行, 原因标签)``。

        原因标签用于日志与面板统计，便于用户理解为什么某句话没被处理。
        """
        self._cleanup()
        self.stats["checked"] += 1
        text = (text or "").strip()

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
        # 无上下文佐证：必须同时有「效果动词 + 目标指代」，否则大概率是闲聊
        if has_evidence:
            if not (has_effect or self._has_request_marker(text)):
                self.stats["blocked_no_verb"] += 1
                return False, "无明确诉求"
        else:
            if not (has_effect and self._has_target_marker(text)):
                self.stats["blocked_no_evidence"] += 1
                return False, "无异常迹象"

        # --- 闸门 ④：冷却 + 信用额度 ---
        now = time.time()
        cooldown = self._cooldown()
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

    def _budget(self) -> int:
        try:
            val = int(self.cfg.smart.get("intent_budget", self.DEFAULT_BUDGET))
        except Exception:
            val = self.DEFAULT_BUDGET
        return max(0, val)

    def _cooldown(self) -> int:
        try:
            val = int(self.cfg.smart.get("intent_cooldown", self.DEFAULT_COOLDOWN))
        except Exception:
            val = self.DEFAULT_COOLDOWN
        return max(0, val)

    def _spend(self, group_id: str, now: float) -> bool:
        """令牌桶：按时间匀速回填，消费 1 点。返回是否成功。"""
        capacity = self._budget()
        if capacity <= 0:
            return False  # 配置为 0 表示禁用额度限制？不——0 表示不花模型
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
        if not bucket:
            return self._budget()
        now = time.time()
        capacity = self._budget()
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
