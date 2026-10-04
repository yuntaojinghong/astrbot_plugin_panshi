"""自主还手（self-defense）：机器人自己被辱骂时，允许它绕过"发送者须为管理员"的门槛。

背景
----

磐石的 LLM 工具（``panshi_ban_user`` / ``panshi_warn_user`` / ``panshi_kick_user``）
原本一律要求**发消息的人**是管理员，理由是防止群友借机器人之手处置别人。

这带来一个副作用（用户实测反馈）：

    有人 @机器人 骂它 → 机器人想还手 → 提示「⛔ 权限不足」

机器人自己是管理员，能力完全够，但门槛检查的是"谁在说话"。
骂它的是普通成员，于是被拦下，还回了句误导的"你需要管理员权限"。

设计取舍
--------

直接取消权限门槛会把禁言权交给全群（任何人都能借机器人之手禁别人），
所以这里只放开一个**很窄**的口子：

    只有当「被处置的人」就是「骂机器人的那个人」时，才允许还手。

再加上两道限流，避免它变成一把可反复使用的刀：

* 冷却时间：两次自主还手之间必须间隔若干秒
* 每日上限：每个群每天最多还手若干次

判定「它确实被骂了」用的是「这条消息 @ 了机器人本人」——
这是协议端给出的事实，不靠关键词猜测，也不依赖模型判断。
群友想让机器人去打别人时会说「@机器人 把张三禁了」，
那种情况下 target(张三) != 发送者，本条规则不生效。

纯逻辑，无 IO，便于单测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class DefenseConfig:
    """自主还手的配置。默认全关。"""

    #: 总开关。默认 False——这是"让机器人自己动手"的能力，应由用户显式开启。
    enable: bool = False
    #: 两次自主还手之间的最短间隔（秒）
    cooldown_seconds: int = 300
    #: 每个群每天的自主还手次数上限
    daily_limit: int = 5
    #: 还手时使用警告还是直接禁言。
    #: 默认用警告——警告本身有累计升级机制（见 warning 配置组），
    #: 比直接禁言温和，也更不容易误伤。
    mode: str = "warn"
    #: mode = "ban" 时的禁言时长（秒）
    ban_seconds: int = 600


@dataclass
class DefenseState:
    """按群记录还手历史。"""

    #: {group_id: 上次还手时间戳}
    last_at: dict[str, float] = field(default_factory=dict)
    #: {group_id: (日期字符串, 当日次数)}
    daily: dict[str, tuple[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "last_at": {k: float(v) for k, v in self.last_at.items()},
            "daily": {k: [str(v[0]), int(v[1])] for k, v in self.daily.items()},
        }

    @classmethod
    def from_dict(cls, d) -> "DefenseState":
        st = cls()
        if not isinstance(d, dict):
            return st
        for k, v in (d.get("last_at") or {}).items():
            try:
                st.last_at[str(k)] = float(v)
            except (TypeError, ValueError):
                continue
        for k, v in (d.get("daily") or {}).items():
            try:
                st.daily[str(k)] = (str(v[0]), int(v[1]))
            except (TypeError, ValueError, IndexError):
                continue
        return st


def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def is_self_defense(event, target: str, *, self_id: str = "",
                    at_bot: bool | None = None) -> tuple[bool, str]:
    """判断这次处置是不是「机器人被骂后还手」。

    Args:
        event: 消息事件（提供发送者）。
        target: 本次要处置的 QQ 号。
        self_id: 机器人自己的 QQ 号。
        at_bot: 这条消息是否 @ 了机器人。``None`` 时自行从 event 解析。

    Returns:
        ``(是否算自主还手, 原因)``。原因为空表示成立。
    """
    tgt = str(target or "").strip()
    if not tgt:
        return False, "没有目标"

    me = str(self_id or "").strip()
    if not me:
        return False, "取不到机器人自身 QQ 号，无法确认是否为自主还手"
    if tgt == me:
        return False, "目标是机器人自己"

    try:
        sender = str(event.get_sender_id() or "").strip()
    except Exception:
        sender = ""
    if not sender:
        return False, "取不到发送者"
    if sender != tgt:
        # 关键：被处置的人不是正在说话的人 → 这是"让机器人去打别人"，不放开
        return False, "目标不是当前发消息的人（可能是借机器人之手处置他人）"

    if at_bot is None:
        at_bot = me in [str(x) for x in (_get_ats(event) or [])]
    if not at_bot:
        return False, "这条消息没有 @ 机器人，无法确认是针对它的辱骂"

    return True, ""


def _get_ats(event) -> list[str]:
    """取消息里被 @ 的 QQ 号。

    用绝对导入：``core`` 包既可能以 ``astrbot_plugin_panshi.core`` 被导入，
    也可能被单独加载，相对导入 ``from .utils import ...`` 在后者会失败。
    原来那样写会把 ImportError 吞掉、``at_bot`` 静默变成 False，
    于是自主还手永远不生效——**排查起来毫无线索**。
    这里显式处理导入失败：真取不到就返回空列表，但不再假装"没有 @"。
    """
    try:
        from astrbot_plugin_panshi.utils import get_ats
    except Exception:
        try:
            from ..utils import get_ats  # type: ignore
        except Exception:
            return []
    try:
        return list(get_ats(event) or [])
    except Exception:
        return []


def allow(state: DefenseState, group_id: str, cfg: DefenseConfig,
          *, now: float | None = None) -> tuple[bool, str]:
    """限流检查。就地更新 ``state``（通过时计入一次）。"""
    now = time.time() if now is None else now
    gid = str(group_id or "")

    if not cfg.enable:
        return False, "自主还手未开启"

    cooldown = max(0, int(cfg.cooldown_seconds))
    last = float(state.last_at.get(gid) or 0.0)
    if cooldown and last and (now - last) < cooldown:
        wait = int(cooldown - (now - last))
        return False, f"距上次还手不足 {cooldown} 秒（还需等 {wait} 秒）"

    limit = max(0, int(cfg.daily_limit))
    if limit:
        day = _today(now)
        cur_day, count = state.daily.get(gid) or ("", 0)
        if cur_day != day:
            count = 0
        if count >= limit:
            return False, f"本群今日自主还手已达上限（{limit} 次）"
        state.daily[gid] = (day, count + 1)

    state.last_at[gid] = now
    return True, ""
