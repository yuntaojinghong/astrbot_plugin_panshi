"""面板体验优化：群管面板 / 自检诊断 / 配置向导。

为什么要它：
    此前所有配置只能在 AstrBot 控制台的面板里改，群里管理想看
    「本群到底开了哪些功能、阈值是多少」只能去翻面板；插件一旦
    因权限/配置缺失而行为异常，成员也只看到零散报错，无从排查。

本模块把两件事放进群聊：
    - **群管面板**（``/面板``）：一屏展示本群所有已开启的能力与关键参数。
    - **自检诊断**（``/自检``）：逐项检查权限、依赖、配置一致性，
      直接指出「哪里没配好、怎么修」。

设计原则：
    - 只读聚合，不改配置；字段名严格对齐 ``_conf_schema.json``，
      保证面板显示的就是真实生效项，不出现「两套真相」。
    - 任何一项检查失败都不能抛异常，最差也要降级成一行提示。
"""

from __future__ import annotations

import time

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("panshi")

from .base_handle import BaseHandle


def _yn(flag: bool) -> str:
    return "✅" if flag else "⬜"


def _fmt_seconds(sec) -> str:
    """把秒数渲染成人话时长。"""
    try:
        sec = int(sec)
    except Exception:
        return str(sec)
    if sec <= 0:
        return "0s"
    if sec % 86400 == 0:
        return f"{sec // 86400}天"
    if sec % 3600 == 0:
        return f"{sec // 3600}小时"
    if sec % 60 == 0:
        return f"{sec // 60}分钟"
    return f"{sec}秒"


class PanelHandle(BaseHandle):
    """群管面板 / 自检诊断 / 配置向导。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        self._boot_ts = time.time()

    # ========== 群管面板 ==========
    def dashboard(self, event) -> str:
        """渲染本群的能力总览面板。"""
        cfg = self.cfg_for(event)
        try:
            gid = self.group_id(event)
        except Exception:
            gid = 0

        lines: list[str] = []
        lines.append("🪨 磐石群管面板")
        lines.append(f"群号 {gid} · 版本 {cfg.version}")
        lines.append("━━━━━━━━━━━━━━")

        basic = cfg.basic
        guard = cfg.guard
        welcome = cfg.welcome
        warning = cfg.warning
        activity = cfg.activity
        automate = cfg.automate
        interact = cfg.interact

        # —— 基础 ——
        lines.append("⚙️ 基础")
        supers = basic.get("super_admins") or []
        groups = basic.get("enable_groups") or []
        scope = "全部群" if not groups or "all" in [str(g).lower() for g in groups] else f"{len(groups)} 个群"
        lines.append(f"  超管 {len(supers)} 人 · 生效范围 {scope}")
        lines.append(
            f"  默认禁言 {_fmt_seconds(basic.get('default_ban_time', 60))}"
            f" · 上限 {_fmt_seconds(basic.get('max_ban_time', 2592000))}"
            f" · 播报{_yn(bool(basic.get('operation_notice', True)))}"
        )

        # —— 风控 ——
        lines.append("🛡️ 风控")
        lines.append(
            f"  {_yn(bool(guard.get('forbidden_enable', True)))} 违禁词"
            f" {_yn(bool(guard.get('spam_enable', True)))} 刷屏"
            f" {_yn(bool(guard.get('ad_enable', True)))} 广告"
            f" {_yn(bool(guard.get('repeat_enable', False)))} 复读"
        )
        wl = guard.get("whitelist") or []
        fw = guard.get("forbidden_words") or []
        extra = []
        if wl:
            extra.append(f"白名单 {len(wl)} 人")
        if fw:
            extra.append(f"违禁词 {len(fw)} 条")
        if guard.get("forbidden_builtin"):
            extra.append("内置词库开")
        if extra:
            lines.append("  " + " · ".join(extra))
        lines.append(
            f"  {_yn(bool(basic.get('anon_protect', True)))} 匿名保护"
        )

        # —— 入群 ——
        if welcome.get("welcome_enable") or welcome.get("verify_enable") or welcome.get("join_review_enable"):
            lines.append("👋 入群")
            bits = []
            if welcome.get("welcome_enable"):
                bits.append("欢迎")
            if welcome.get("verify_enable"):
                bits.append(f"验证({welcome.get('verify_timeout', 120)}s)")
            if welcome.get("join_review_enable"):
                bits.append("申请审核")
            if welcome.get("leave_notify"):
                bits.append("退群通知")
            lines.append("  " + " · ".join(bits))
            if welcome.get("leave_block"):
                lines.append("  ⚠️ 退群自动拉黑：已开启")

        # —— 警告 ——
        lines.append("⚠️ 警告")
        lines.append(
            f"  {_yn(bool(warning.get('warning_enable', True)))} 已启用"
            f" · 记录保留 {warning.get('warn_expire_days', 30)} 天"
        )
        ladder = cfg.escalation_ladder
        if ladder:
            lines.append("  阶梯：" + " → ".join(self._ladder_desc(t) for t in ladder))
        else:
            ban_at = warning.get("warn_ban_threshold", 0) or 0
            kick_at = warning.get("warn_kick_threshold", 0) or 0
            segs = []
            if ban_at:
                segs.append(
                    f"{ban_at}次禁言{_fmt_seconds(warning.get('warn_ban_time', 600))}"
                )
            if kick_at:
                segs.append(f"{kick_at}次踢出")
            if segs:
                lines.append("  阈值：" + " → ".join(segs))
            else:
                lines.append("  ⬜ 未设阶梯/阈值（警告不会自动处置）")

        # —— 活跃 ——
        lines.append("🎯 活跃")
        lines.append(
            f"  {_yn(bool(activity.get('checkin_enable', True)))} 签到"
            f"（{activity.get('checkin_points', 10)}+随机{activity.get('checkin_random_bonus', 10)} 分）"
        )

        # —— 自动化 ——
        lines.append("🌙 自动化")
        if automate.get("curfew_enable"):
            active = self._curfew_active(automate)
            lines.append(
                f"  宵禁 {automate.get('curfew_start', '23:00')}~{automate.get('curfew_end', '07:00')}"
                f"{'（进行中）' if active else ''}"
            )
        else:
            lines.append("  ⬜ 宵禁未开启")
        if automate.get("announce_enable") and (automate.get("announce_content") or "").strip():
            lines.append(f"  定时公告：每 {automate.get('announce_interval_minutes', 360)} 分钟")

        # —— 互动 ——
        lines.append("🎮 互动")
        lines.append(
            f"  {_yn(bool(interact.get('vote_enable', True)))} 投票"
            f" {_yn(bool(interact.get('chain_enable', True)))} 接龙"
            f" {_yn(bool(interact.get('self_query_enable', True)))} 自助查询"
            f" {_yn(bool(interact.get('auto_reply_enable', False)))} 自动回复"
        )
        ar = cfg.parsed_auto_replies()
        if ar:
            lines.append(f"  自动回复 {len(ar)} 条规则")

        lines.append("━━━━━━━━━━━━━━")
        lines.append("发 /自检 做一次完整体检 · /群管帮助 看指令")
        return "\n".join(lines)

    @staticmethod
    def _ladder_desc(tier: dict) -> str:
        cnt = tier.get("count", 0)
        act = tier.get("action", "warn")
        if act == "ban":
            return f"{cnt}次禁言{_fmt_seconds(tier.get('duration', 0))}"
        if act == "kick":
            return f"{cnt}次踢出"
        return f"{cnt}次警告"

    @staticmethod
    def _curfew_active(automate: dict) -> bool:
        """当前是否正处于宵禁时段。"""
        try:
            start = str(automate.get("curfew_start", "23:00"))
            end = str(automate.get("curfew_end", "07:00"))
            now = time.strftime("%H:%M")
            if start <= end:
                return start <= now < end
            # 跨零点，如 23:00~07:00
            return now >= start or now < end
        except Exception:
            return False

    # ========== 自检诊断 ==========
    async def self_check(self, event) -> str:
        """逐项体检：权限 / 依赖 / 配置一致性。"""
        checks: list[tuple[str, str]] = []  # (状态图标, 描述)

        cfg = self.cfg_for(event)

        # 1) 超管 / 权限体系
        try:
            supers = cfg.super_admins
            if supers:
                checks.append(("✅", f"超级管理员：{len(supers)} 人"))
            else:
                checks.append(("✅", "超级管理员：未配置（仅群管理员可用）"))
        except Exception as e:
            checks.append(("⚠️", f"读取超管失败：{e}"))

        # 2) 适配器能力探测
        bot = getattr(event, "bot", None)
        api_names = {
            "set_group_ban": "禁言",
            "set_group_kick": "踢人",
            "delete_msg": "撤回",
            "set_group_whole_ban": "全体禁言",
        }
        if bot is not None:
            missing = [label for name, label in api_names.items() if not hasattr(bot, name)]
            if missing:
                checks.append(("⚠️", "适配器缺少 API：" + "、".join(missing)))
            else:
                checks.append(("✅", "适配器 API 齐备（禁言/踢人/撤回/全体禁言）"))
        else:
            checks.append(("⚠️", "未取到 bot 实例，无法探测适配器能力"))

        # 3) 存储
        try:
            if self.db is None:
                checks.append(("⚠️", "存储未绑定（积分/警告记录可能不落库）"))
            else:
                checks.append(("✅", "存储已绑定"))
        except Exception as e:
            checks.append(("⚠️", f"存储检查失败：{e}"))

        # 4) 警告处置策略
        try:
            ladder = cfg.escalation_ladder
            ban_at = cfg.get("warning", "warn_ban_threshold", 0) or 0
            kick_at = cfg.get("warning", "warn_kick_threshold", 0) or 0
            if not cfg.get("warning", "warning_enable", True):
                checks.append(("⬜", "警告系统已关闭"))
            elif ladder:
                checks.append(("✅", f"警告阶梯 {len(ladder)} 级（阶梯优先于阈值）"))
            elif ban_at or kick_at:
                checks.append(("✅", f"警告阈值：禁言@{ban_at} 踢出@{kick_at}"))
            else:
                checks.append(("⚠️", "既未配警告阶梯、也未配阈值——累积警告不会自动处置"))
        except Exception as e:
            checks.append(("⚠️", f"警告策略检查失败：{e}"))

        # 5) 风控参数一致性
        try:
            if cfg.get("guard", "spam_enable", True):
                win = cfg.get("guard", "spam_window", 0) or 0
                cnt = cfg.get("guard", "spam_count", 0) or 0
                if win <= 0 or cnt <= 0:
                    checks.append(("⚠️", "已开启防刷屏但窗口/条数未设——将使用默认值"))
                else:
                    checks.append(("✅", f"防刷屏：{win}s 内 {cnt} 条触发"))
            else:
                checks.append(("✅", "防刷屏未开启"))
        except Exception as e:
            checks.append(("⚠️", f"风控检查失败：{e}"))

        # 6) 定时公告
        try:
            if cfg.get("automate", "announce_enable", False):
                content = (cfg.get("automate", "announce_content") or "").strip()
                interval = cfg.get("automate", "announce_interval_minutes", 0) or 0
                if not content:
                    checks.append(("⚠️", "定时公告已开启但内容为空——不会发送"))
                elif int(interval) <= 0:
                    checks.append(("⚠️", "定时公告间隔为 0——不会发送"))
                else:
                    checks.append(("✅", f"定时公告已配置（每 {interval} 分钟）"))
            else:
                checks.append(("✅", "定时公告未开启"))
        except Exception as e:
            checks.append(("⚠️", f"定时公告检查失败：{e}"))

        # 7) 启用范围
        try:
            groups = cfg.enable_groups
            gid = self.group_id(event)
            if not groups or "all" in [str(g).lower() for g in groups]:
                checks.append(("✅", "生效范围：全部群"))
            elif str(gid) in [str(g) for g in groups]:
                checks.append(("✅", f"本群 {gid} 在启用列表中"))
            else:
                checks.append(("⚠️", f"本群 {gid} 不在启用列表——指令可能不响应"))
        except Exception as e:
            checks.append(("⚠️", f"启用范围检查失败：{e}"))

        # 8) 本地意图解析可用性（无 LLM 也能用）
        try:
            from .local_intent import LocalIntentParser

            parser = LocalIntentParser(cfg)
            got = parser.parse(event, "全体禁言")
            if got and got.get("action") == "whole_ban":
                checks.append(("✅", "本地指令解析正常（无 LLM 也可用自然语言）"))
            else:
                checks.append(("⚠️", "本地指令解析自测未通过"))
        except Exception as e:
            checks.append(("⚠️", f"本地指令解析不可用：{e}"))

        warns = sum(1 for icon, _ in checks if icon == "⚠️")
        lines = ["🩺 磐石自检报告", "━━━━━━━━━━━━━━"]
        for icon, desc in checks:
            lines.append(f"{icon} {desc}")
        lines.append("━━━━━━━━━━━━━━")
        if warns:
            lines.append(f"发现 {warns} 处待处理项，可用 /配置 查看对应主题怎么改。")
        else:
            lines.append("全部通过，磐石运行良好 🎉")
        return "\n".join(lines)

    # ========== 配置向导 ==========
    def config_guide(self, topic: str = "") -> str:
        """按主题给出配置指引；不带参数则列出所有主题。"""
        topic = (topic or "").strip()

        guides: dict[str, tuple[str, list[str]]] = {
            "风控": (
                "🛡️ 风控与自动处置",
                [
                    "· guard.forbidden_enable / forbidden_words —— 违禁词开关与词表",
                    "· guard.spam_enable / spam_window / spam_count —— 防刷屏",
                    "· guard.ad_enable —— 广告检测",
                    "· guard.repeat_enable / repeat_count —— 复读检测",
                    "· guard.whitelist —— 白名单，命中者永不处置",
                    "· basic.anon_protect —— 匿名消息保护（避免误伤他人马甲）",
                    "· warning.escalation_ladder —— 阶梯处置，一行一级：",
                    "    1|warn            1 次：仅警告",
                    "    3|ban|600         3 次：禁言 10 分钟",
                    "    5|kick            5 次：踢出",
                ],
            ),
            "活跃": (
                "🎯 活跃与积分",
                [
                    "· activity.checkin_enable —— 开启签到",
                    "· activity.checkin_points —— 每次签到基础积分",
                    "· activity.checkin_random_bonus —— 签到随机额外积分上限",
                    "指令：/签到 · /积分 [@某人] · /排行 [积分|发言]",
                ],
            ),
            "互动": (
                "🎮 互动工具",
                [
                    "· interact.vote_enable —— 群投票（/投票 标题|选项1|选项2）",
                    "· interact.chain_enable —— 接龙（/接龙 主题）",
                    "· interact.self_query_enable —— 成员自助查询（/我的 积分）",
                    "· interact.auto_reply_enable —— 关键词自动回复开关",
                    "· interact.auto_replies_text —— 规则，每行一条：",
                    "    群规,规矩 => 群规见置顶公告",
                    "    签到 => 发送 /签到 即可",
                ],
            ),
            "宵禁": (
                "🌙 宵禁 / 定时公告",
                [
                    "· automate.curfew_enable —— 开关（也可用 /宵禁 开|关）",
                    "· automate.curfew_start / curfew_end —— 时段，如 23:00 / 07:00",
                    "· automate.curfew_ban_time —— 宵禁期间违规禁言时长",
                    "· automate.announce_enable —— 开启定时公告",
                    "· automate.announce_content —— 公告内容",
                    "· automate.announce_interval_minutes —— 间隔（分钟），需 > 0",
                    "命令行更省事：/宵禁 23:30-07:00",
                ],
            ),
            "按群": (
                "🏘️ 按群独立配置",
                [
                    "面板「基础设置」→ 按群配置，可给单个群覆盖任意字段，",
                    "未覆盖的字段自动继承全局值（深合并）。",
                    "运行时所有读取都走 for_group()，改完即时生效。",
                    "配合 basic.enable_groups 控制插件在哪些群生效。",
                ],
            ),
            "本地": (
                "🧠 无 LLM 也能用自然语言",
                [
                    "磐石内置本地规则解析：即使没配对话模型，",
                    "「禁言@某人 10 分钟」「全群禁言」「宵禁改到 23 点半」",
                    "这类明确指令也能直接执行。",
                    "配置了 LLM 时，本地解析 + 模型理解双保险，命中即执行。",
                    "发 /自检 可确认本地解析是否正常。",
                ],
            ),
        }

        if not topic:
            lines = ["📖 配置向导", "━━━━━━━━━━━━━━"]
            for key, (title, _) in guides.items():
                lines.append(f"· /配置 {key} —— {title}")
            lines.append("━━━━━━━━━━━━━━")
            lines.append("提示：/面板 看当前生效状态，/自检 查异常。")
            return "\n".join(lines)

        for key, (title, items) in guides.items():
            if key in topic or topic in key:
                return f"{title}\n━━━━━━━━━━━━━━\n" + "\n".join(items)

        return (
            f"🤔 没有「{topic}」这个主题。可用："
            + "、".join(guides.keys())
            + "\n发 /配置 查看全部。"
        )
