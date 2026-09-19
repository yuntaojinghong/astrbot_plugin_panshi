"""磐石 · 智能群管插件主入口。

职责：
1. 注册所有指令（/禁言、/踢、/撤回 …）
2. 监听群消息，做防护检查 + 上下文记录 + 智能意图识别
3. 监听入群/退群/加群申请事件
4. 权限校验（超级管理员 > 群主 > 管理员 > 成员）
"""

from __future__ import annotations

import os
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .config import PluginConfig
from .core import (
    ActivityHandle,
    AutomateHandle,
    ContextCollector,
    GuardHandle,
    IntentExecutor,
    IntentGate,
    IntentParser,
    InteractHandle,
    JoinHandle,
    LocalIntentParser,
    NormalHandle,
    PanelHandle,
    WarningHandle,
    WelcomeHandle,
)
from .data import GroupInfoCache, Storage
from .utils import PermLevel, check_permission, parse_duration, parse_target
from .utils.helpers import get_group_id

# 内置事件子类型常量（aiocqhttp / OneBot v11）
try:
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
        AiocqhttpMessageEvent,  # noqa: F401
    )
except Exception:  # pragma: no cover
    AiocqhttpMessageEvent = None  # type: ignore


class PanshiPlugin(Star):
    """磐石群管插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.cfg = PluginConfig(config or {}, context)
        self._super_admins = self.cfg.super_admins

        # 数据目录：AstrBot 的 data 目录下
        data_dir = self._resolve_data_dir()
        self.db = Storage(data_dir)

        # 打通按群配置：让 PluginConfig 能读取每群 override（修复「独立配置不生效」）
        try:
            self.cfg.bind_storage(self.db)
        except Exception as e:
            logger.warning(f"[磐石] 绑定按群配置存储失败: {e}")

        # 群信息缓存（供配置面板自动识别群聊）
        self.group_cache = GroupInfoCache(context)

        # 功能模块
        self.normal = NormalHandle(self.cfg, self.db)
        self.guard = GuardHandle(self.cfg, self.db)
        self.welcome = WelcomeHandle(self.cfg, self.db)
        self.join = JoinHandle(self.cfg, self.db)
        self.warning = WarningHandle(self.cfg, self.db)
        self.activity = ActivityHandle(self.cfg, self.db)
        self.automate = AutomateHandle(self.cfg, self.db)
        self.interact = InteractHandle(self.cfg, self.db)
        # 面板 / 自检 / 配置向导
        self.panel = PanelHandle(self.cfg, self.db)

        # 消息缓存打通：让「撤回/净化」能读到 GuardHandle 缓存的最近消息
        self.normal.bind_cache(self.guard)

        # 智能模块
        self.ctx = ContextCollector()
        # 意图闸门：本地四道判定 + 信用额度，先把废话挡在模型之外
        self.gate = IntentGate(self.cfg, self.ctx)
        self.intent = IntentParser(self.cfg, self.db, self.ctx, gate=self.gate)
        # 本地规则解析器：无需 LLM，常见自然语言指令的兜底
        self.local_intent = LocalIntentParser(self.cfg)
        self.executor = IntentExecutor(
            self.cfg, self.db, self.ctx, self.normal, self.warning, self.automate
        )

        # 已解析的群列表（宵禁用）
        self._enabled_groups: list[str] = []

        # 配置面板（WebUI Pages）
        self.web = None
        self._register_pages(context)

    def _register_pages(self, context: Context) -> None:
        """注册 WebUI 配置面板。

        低版本 AstrBot 不支持插件 Pages，此时静默降级，
        不影响插件其余功能。
        """
        try:
            from .pages_api import PanshiWebController
            from .pages_service import PageService

            service = PageService(self.cfg, self.db, self.group_cache)
            self.web = PanshiWebController(context, service)
            # 面板保存全局配置后，让宵禁等自动化设置立即生效（不用重载插件）
            self.web.on_config_saved = self._apply_automate_sync
            self.web.register_routes()
        except Exception as e:
            logger.warning(f"[磐石] 配置面板注册失败（不影响群管功能）: {e}")
            self.web = None

    # ========== 生命周期 ==========
    async def initialize(self):
        logger.info("[磐石] 插件初始化完成")
        try:
            await self._refresh_groups()
            self.automate.bind_sender(self._send_whole_ban, self._enabled_groups)
            self.automate.bind_announce_sender(self._send_group_text)
            self.automate.bind_groups_provider(self._current_group_ids)
            await self.automate.apply_now()
        except Exception as e:
            logger.warning(f"[磐石] 初始化后置任务异常: {e}")

    async def _current_group_ids(self) -> list[str]:
        """动态获取 bot 当前所在群列表（宵禁/定时公告每轮执行前调用）。"""
        try:
            groups = await self.group_cache.list_groups(force=False)
            ids = [str(g.get("group_id")) for g in groups if g.get("group_id")]
            if ids:
                self._enabled_groups = ids
            return ids
        except Exception:
            return self._enabled_groups

    async def _send_group_text(self, group_id: str, text: str):
        """向指定群发送纯文本（定时公告用），支持多账号。"""
        clients = self.group_cache.iter_clients()
        if not clients:
            logger.warning("[磐石] 发送群消息时协议端未连接")
            return
        for sid, cli in clients:
            try:
                params = {"group_id": int(group_id), "message": str(text)}
                if sid:
                    params["self_id"] = sid
                await cli.call_action("send_group_msg", **params)
                return
            except Exception as e:
                logger.warning(f"[磐石] 群消息发送失败(账号 {sid or '默认'}): {e}")
                continue

    async def terminate(self):
        await self.automate.stop_curfew()
        self.db.save()
        logger.info("[磐石] 插件已卸载")

    def _resolve_data_dir(self) -> str:
        """定位 AstrBot 的 data 目录。"""
        # 优先使用 AstrBot 提供的路径
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return os.path.join(get_astrbot_data_path(), "plugin_data", "astrbot_plugin_panshi")
        except Exception:
            pass
        # 回退：假设插件位于 <AstrBot>/data/plugins/<plugin>/
        base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        return os.path.join(base, "data", "plugin_data", "astrbot_plugin_panshi")

    async def _refresh_groups(self):
        """从适配器获取 bot 所在群列表。"""
        try:
            groups = await self.group_cache.refresh()
            self._enabled_groups = [str(g.get("group_id")) for g in groups if g.get("group_id")]
        except Exception as e:
            logger.warning(f"[磐石] 获取群列表失败: {e}")

    async def _apply_automate_sync(self):
        """配置保存后即时同步宵禁状态（面板保存 / 智能设置共用）。"""
        try:
            state = await self.automate.apply_now()
            if state != "off":
                logger.info(f"[磐石] 宵禁状态已即时同步: {state}")
        except Exception as e:
            logger.warning(f"[磐石] 宵禁即时同步失败: {e}")

    async def _send_whole_ban(self, group_id: str, enable: bool):
        """宵禁时对指定群开/关全体禁言。

        注意：此前用 platform_manager.get_instances()，该方法并不存在，
        异常被静默吞掉，导致宵禁全体禁言一直不生效。
        现改走 group_cache 的统一客户端解析（含多账号 self_id 显式传参）。
        """
        try:
            clients = self.group_cache.iter_clients()
        except Exception as e:
            logger.warning(f"[磐石] 宵禁获取协议端客户端失败: {e}")
            return
        if not clients:
            logger.warning("[磐石] 宵禁执行时协议端未连接，跳过全体禁言")
            return
        for sid, cli in clients:
            try:
                if sid:
                    await cli.call_action(
                        "set_group_whole_ban",
                        group_id=int(group_id),
                        enable=enable,
                        self_id=sid,
                    )
                else:
                    await cli.call_action(
                        "set_group_whole_ban",
                        group_id=int(group_id),
                        enable=enable,
                    )
                return
            except Exception as e:
                logger.warning(f"[磐石] 宵禁全体禁言失败(账号 {sid or '默认'}): {e}")
                continue

    # ========== 群消息总入口（事件监听，优先级低于指令） ==========
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_group_message(self, event: AstrMessageEvent):
        """处理每条群消息：上下文记录 -> 防护 -> 智能识别。"""
        group_id = get_group_id(event)
        if group_id and not self.cfg.group_enabled(group_id):
            return

        message_id = self._message_id(event)
        text = (getattr(event, "message_str", "") or "").strip()

        # 1. 记录上下文（供智能识别使用）
        self.ctx.record(event, message_id)

        # 2. 记录发言统计
        try:
            if group_id:
                self.db.record_message(group_id, event.get_sender_id())
        except Exception:
            pass

        # 2.5 关键词自动回复（命中即回复，不打断后续统计）
        try:
            if self.cfg.interact.get("auto_reply_enable", False):
                reply = self.interact.match_auto_reply(text)
                if reply:
                    yield event.plain_result(reply)
                    return
        except Exception as e:
            logger.warning(f"[磐石] 自动回复异常: {e}")

        # 3. 防护检查（违禁词/刷屏/广告/复读）
        try:
            result = await self.guard.inspect(event, message_id)
            if result:
                yield event.plain_result(result)
                return
        except Exception as e:
            logger.warning(f"[磐石] 防护检查异常: {e}")

        # 4. 验证码回复检查
        try:
            verify_result = await self.welcome.check_verify_reply(event)
            if verify_result:
                yield event.plain_result(verify_result)
                return
        except Exception as e:
            logger.warning(f"[磐石] 验证检查异常: {e}")

        # 5. 待确认操作回复
        if self.executor.has_pending(event):
            confirm_result = await self.executor.confirm(event, text)
            if confirm_result:
                yield event.plain_result(confirm_result)
                return

        # 6. 智能意图识别
        #    策略：**LLM 为主（听得懂人话），本地规则为辅（零成本快速通道）**。
        #    - 本地规则只处理「目标明确、句式标准」的指令，命中即秒执行，不消耗 token；
        #    - 其余一切交给 AstrBot 里已配置的模型理解，覆盖口语化表达；
        #    - 完全没有模型时，本地规则仍能扛住常用指令，不会变成不可用。
        try:
            if self.intent.should_trigger(event, text):
                # 权限门槛：至少管理员才能驱动智能操作
                if not check_permission(event, PermLevel.ADMIN, self._super_admins):
                    yield event.plain_result("⛔ 抱歉，管理操作需要管理员权限。")
                    return

                llm_ok = self._llm_available()
                intent = None
                source = ""
                group_key = get_group_id(event) or ""

                # 6a. 缓存优先：短时间内同一句话不重复问模型
                if group_key:
                    try:
                        intent = self.gate.cached(group_key, text)
                        if intent:
                            source = "缓存"
                    except Exception:
                        intent = None

                # 6b. 本地规则快速通道：仅在「有 LLM」时作为前置快路径，
                #     命中则省一次模型调用；本地判定不了会返回 None，
                #     自然落到 LLM，不会因为本地没覆盖就丢掉这句话。
                if intent is None and self.cfg.smart.get("local_fast_path", True):
                    try:
                        intent = self.local_intent.parse(event, text)
                        if intent:
                            source = "本地规则"
                    except Exception as e:
                        logger.warning(f"[磐石] 本地意图解析异常: {e}")
                        intent = None

                # 6c. 主通道：交给 AstrBot 已配置的模型理解（听得懂人话的关键）
                if intent is None and llm_ok:
                    intent = await self.intent.parse(event)
                    if intent:
                        source = "AI 理解"
                        # 记住结果，短时间内相同句子直接复用
                        if group_key:
                            try:
                                self.gate.remember(group_key, text, intent)
                            except Exception:
                                pass

                # 6d. 没有 LLM 时的兜底：再试一次本地规则
                #     （上面 local_fast_path 关闭时也要保证无模型可用）
                if intent is None and not llm_ok:
                    try:
                        intent = self.local_intent.parse(event, text)
                        if intent:
                            source = "本地规则"
                    except Exception:
                        intent = None

                if intent:
                    action = intent.get("action")
                    if action and action != "none":
                        result = await self.executor.execute(event, intent)
                        if result:
                            yield event.plain_result(result)
                            return
                elif not llm_ok:
                    # 本地也没覆盖 + 没有可用模型：给可执行引导，
                    # 而不是「未找到 LLM 供应商」这种让人一头雾水的报错。
                    yield event.plain_result(
                        "🤔 我没太理解这句话的意图。\n"
                        "可以试试这些说法：\n"
                        "· 「禁言张三 10 分钟」（也可以 @他 或引用他的消息）\n"
                        "· 「@某人 再发广告就踢了」\n"
                        "· 「全体禁言」「解除全体禁言」\n"
                        "· 「宵禁改到 23:30 到 07:00」\n"
                        "· 或直接发送 /群管帮助 查看全部指令\n"
                        "💡 想让我听懂更口语化的表达，可在 AstrBot「服务提供商」里配置一个对话模型。"
                    )
                    return
                elif self.intent.last_error:
                    # 配了模型但这次解析失败：把真实原因透出来，便于排查
                    logger.warning(f"[磐石] 意图解析未成功：{self.intent.last_error}")
        except Exception as e:
            logger.error(f"[磐石] 智能识别异常: {e}")

    # ========== 入群 / 退群 / 加群申请事件 ==========
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_notice(self, event: AstrMessageEvent):
        """处理群成员变动与加群申请的通知事件。"""
        raw = getattr(event, "message_obj", None)
        if raw is None:
            return
        post_type = getattr(raw, "post_type", None)
        if post_type != "notice":
            return

        notice_type = getattr(raw, "notice_type", None)
        group_id = get_group_id(event)
        if group_id and not self.cfg.group_enabled(group_id):
            return

        try:
            # 成员增加
            if notice_type == "group_increase":
                user_id = str(getattr(raw, "user_id", ""))
                sub_type = getattr(raw, "sub_type", "approve")
                result = await self.welcome.on_member_increase(event, user_id, sub_type)
                if result:
                    yield event.plain_result(result)
            # 成员减少
            elif notice_type == "group_decrease":
                user_id = str(getattr(raw, "user_id", ""))
                result = await self.welcome.on_member_decrease(event, user_id)
                if result:
                    yield event.plain_result(result)
        except Exception as e:
            logger.warning(f"[磐石] 通知事件处理异常: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def on_request(self, event: AstrMessageEvent):
        """处理加群申请。"""
        raw = getattr(event, "message_obj", None)
        if raw is None or getattr(raw, "post_type", None) != "request":
            return
        if getattr(raw, "request_type", None) != "group":
            return
        try:
            flag = str(getattr(raw, "flag", ""))
            user_id = str(getattr(raw, "user_id", ""))
            comment = str(getattr(raw, "comment", "") or "")
            result = await self.join.on_request(event, flag, user_id, comment)
            if result:
                yield event.plain_result(result)
        except Exception as e:
            logger.warning(f"[磐石] 加群申请处理异常: {e}")

    # ========== 指令：基础管理 ==========
    @filter.command("禁言", alias={"mute"})
    async def cmd_ban(self, event: AstrMessageEvent, arg: str = ""):
        """禁言：/禁言 @某人 10m 或 引用消息 /禁言 10m"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, duration_text = self._split_target_duration(event, arg)
        seconds = parse_duration(duration_text, self.cfg.default_ban_time) if duration_text else self.cfg.default_ban_time
        result = await self.normal.set_ban(event, target, seconds)
        yield event.plain_result(result)

    @filter.command("解禁", alias={"unmute"})
    async def cmd_unban(self, event: AstrMessageEvent):
        """解禁：/解禁 @某人"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        yield event.plain_result(await self.normal.cancel_ban(event, target))

    @filter.command("全禁", alias={"全体禁言"})
    async def cmd_whole_ban(self, event: AstrMessageEvent, arg: str = ""):
        """全体禁言：/全禁 开 或 /全禁 关"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        enable = arg.strip() in ("开", "开启", "on", "true", "1", "")
        yield event.plain_result(await self.normal.whole_ban(event, enable))

    @filter.command("踢", alias={"踢了", "kick"})
    async def cmd_kick(self, event: AstrMessageEvent, arg: str = ""):
        """踢人：/踢 @某人 [原因]"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        reason = self._strip_target_text(event, arg)
        yield event.plain_result(await self.normal.kick(event, target, reject=False, reason=reason))

    @filter.command("拉黑")
    async def cmd_block(self, event: AstrMessageEvent, arg: str = ""):
        """踢出并拉黑：/拉黑 @某人"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        yield event.plain_result(await self.normal.kick(event, target, reject=True))

    @filter.command("撤回", alias={"recall"})
    async def cmd_recall(self, event: AstrMessageEvent, count: str = ""):
        """撤回：/撤回 或 引用消息 /撤回，或 /撤回 10"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        n = int(count) if count.isdigit() else 1
        yield event.plain_result(await self.normal.recall(event, count=n))

    @filter.command("净化", alias={"清屏"})
    async def cmd_purge(self, event: AstrMessageEvent, count: str = ""):
        """批量撤回最近消息：/净化 30"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        n = int(count) if count.isdigit() else 30
        n = min(n, 100)
        yield event.plain_result(await self.normal.purge(event, count=n))

    @filter.command("改名")
    async def cmd_card(self, event: AstrMessageEvent, arg: str = ""):
        """改群名片：/改名 @某人 新昵称"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        name = self._strip_target_text(event, arg)
        yield event.plain_result(await self.normal.set_card(event, target, name))

    @filter.command("头衔")
    async def cmd_title(self, event: AstrMessageEvent, arg: str = ""):
        """设头衔（需群主）：/头衔 @某人 头衔"""
        if not self._check(event, PermLevel.OWNER):
            yield event.plain_result(self._no_perm("群主"))
            return
        target, _ = parse_target(event)
        title = self._strip_target_text(event, arg)
        yield event.plain_result(await self.normal.set_special_title(event, target, title))

    @filter.command("上管", alias={"设置管理员"})
    async def cmd_set_admin(self, event: AstrMessageEvent):
        """设管理员（需群主）：/上管 @某人"""
        if not self._check(event, PermLevel.OWNER):
            yield event.plain_result(self._no_perm("群主"))
            return
        target, _ = parse_target(event)
        yield event.plain_result(await self.normal.set_admin(event, target, enable=True))

    @filter.command("下管", alias={"取消管理员"})
    async def cmd_unset_admin(self, event: AstrMessageEvent):
        """取消管理员（需群主）：/下管 @某人"""
        if not self._check(event, PermLevel.OWNER):
            yield event.plain_result(self._no_perm("群主"))
            return
        target, _ = parse_target(event)
        yield event.plain_result(await self.normal.set_admin(event, target, enable=False))

    @filter.command("设精", alias={"设为精华"})
    async def cmd_essence(self, event: AstrMessageEvent):
        """设精华：引用消息 /设精"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.set_essence(event, enable=True))

    @filter.command("移精", alias={"移除精华"})
    async def cmd_unessence(self, event: AstrMessageEvent):
        """移精华：引用消息 /移精"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.set_essence(event, enable=False))

    @filter.command("群名", alias={"设置群名"})
    async def cmd_group_name(self, event: AstrMessageEvent, arg: str = ""):
        """改群名：/群名 新群名"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.set_group_name(event, arg.strip()))

    @filter.command("公告", alias={"发布群公告"})
    async def cmd_notice(self, event: AstrMessageEvent, arg: str = ""):
        """发群公告：/公告 内容"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.send_notice(event, arg.strip()))

    @filter.command("群员", alias={"群友信息"})
    async def cmd_members(self, event: AstrMessageEvent):
        """群成员概览：/群员"""
        yield event.plain_result(await self.normal.member_overview(event))

    # ========== 指令：警告 ==========
    @filter.command("警告", alias={"warn"})
    async def cmd_warn(self, event: AstrMessageEvent, arg: str = ""):
        """警告：/警告 @某人 [原因]"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        reason = self._strip_target_text(event, arg)
        yield event.plain_result(await self.warning.add_warning(event, target, reason))

    @filter.command("撤销警告")
    async def cmd_unwarn(self, event: AstrMessageEvent, arg: str = ""):
        """撤销警告：/撤销警告 @某人 [次数]"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        n = int(arg) if arg.strip().isdigit() else 1
        yield event.plain_result(await self.warning.remove_warning(event, target, n))

    @filter.command("违规记录", alias={"查警告"})
    async def cmd_query_warn(self, event: AstrMessageEvent):
        """查违规记录：/违规记录 [@某人]"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        target, _ = parse_target(event)
        yield event.plain_result(await self.warning.query_warning(event, target))

    # ========== 指令：活跃 ==========
    @filter.command("签到", alias={"checkin"})
    async def cmd_checkin(self, event: AstrMessageEvent):
        """签到：/签到"""
        yield event.plain_result(await self.activity.checkin(event))

    @filter.command("积分", alias={"查积分"})
    async def cmd_points(self, event: AstrMessageEvent):
        """查积分：/积分 [@某人]"""
        target, _ = parse_target(event)
        yield event.plain_result(await self.activity.query_points(event, target))

    @filter.command("排行", alias={"排行榜"})
    async def cmd_rank(self, event: AstrMessageEvent, arg: str = ""):
        """排行：/排行 [积分|发言]"""
        if arg.strip() in ("发言", "message", "msg"):
            yield event.plain_result(await self.activity.rank_messages(event))
        else:
            yield event.plain_result(await self.activity.rank_points(event))

    # ========== 指令：互动工具 ==========
    @filter.command("投票", alias={"vote"})
    async def cmd_vote(self, event: AstrMessageEvent, arg: str = ""):
        """投票：/投票 标题|选项1|选项2 或 /投票 编号"""
        if not self.cfg.interact.get("vote_enable", True):
            yield event.plain_result("🗳️ 本群未开启投票功能。")
            return
        text = arg.strip()
        if not text:
            yield event.plain_result(await self.interact.vote_result(event))
            return
        # 纯数字 -> 投票；含 | -> 发起
        if re.fullmatch(r"\d+", text):
            yield event.plain_result(await self.interact.cast_vote(event, text))
        elif "|" in text or "｜" in text:
            yield event.plain_result(await self.interact.start_vote(event, text))
        else:
            yield event.plain_result(await self.interact.vote_result(event))

    @filter.command("投票结果", alias={"票数"})
    async def cmd_vote_result(self, event: AstrMessageEvent):
        """查看投票结果：/投票结果"""
        yield event.plain_result(await self.interact.vote_result(event))

    @filter.command("接龙", alias={"chain"})
    async def cmd_chain(self, event: AstrMessageEvent, arg: str = ""):
        """接龙：/接龙 主题 发起，/接龙 内容 参与"""
        if not self.cfg.interact.get("chain_enable", True):
            yield event.plain_result("🔗 本群未开启接龙功能。")
            return
        text = arg.strip()
        if not text:
            yield event.plain_result(await self.interact.render_chain(event))
            return
        yield event.plain_result(await self.interact.add_chain(event, text))

    @filter.command("我的", alias={"群档案", "我的信息"})
    async def cmd_self_query(self, event: AstrMessageEvent, arg: str = ""):
        """自助查询：/我的 [积分|警告|发言|签到]"""
        if not self.cfg.interact.get("self_query_enable", True):
            return
        yield event.plain_result(await self.interact.self_query(event, arg))

    @filter.command("自助", alias={"查询"})
    async def cmd_self_help(self, event: AstrMessageEvent, arg: str = ""):
        """自助查询（别名）：/自助 积分"""
        if not self.cfg.interact.get("self_query_enable", True):
            return
        yield event.plain_result(await self.interact.self_query(event, arg))

    # ========== 指令：宵禁 ==========
    @filter.command("宵禁", alias={"夜间禁言"})
    async def cmd_curfew(self, event: AstrMessageEvent, arg: str = ""):
        """宵禁：/宵禁 状态 | /宵禁 开|关 | /宵禁 23:30-07:00"""
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self._handle_curfew(arg.strip()))

    async def _handle_curfew(self, text: str) -> str:
        """处理 /宵禁 的四种形态：状态 / 开 / 关 / 时间段。"""
        if not text or text in ("状态", "查看", "查询"):
            return self._curfew_status_text()

        if text in ("开", "开启", "打开", "on", "true", "1"):
            return await self._set_curfew_config({"curfew_enable": True})
        if text in ("关", "关闭", "停用", "off", "false", "0"):
            return await self._set_curfew_config({"curfew_enable": False})

        # 时间段：23:30-07:00 / 23:30~07:00 / 23:30 到 07:00 / 23:30 07:00
        normalized = text.replace("：", ":")
        times = re.findall(r"(\d{1,2}):([0-5]\d)", normalized)
        if times:
            if len(times) < 2:
                return "❌ 请同时给出开始和结束时间，例如：/宵禁 23:30-07:00"
            h1, m1 = times[0]
            h2, m2 = times[1]
            start, end = f"{int(h1):02d}:{m1}", f"{int(h2):02d}:{m2}"
            if start == end:
                return "❌ 宵禁开始和结束时间不能相同。"
            return await self._set_curfew_config(
                {"curfew_enable": True, "curfew_start": start, "curfew_end": end}
            )

        return (
            "🤔 没看懂参数。用法：\n"
            "· /宵禁 状态 —— 查看当前设置\n"
            "· /宵禁 开 或 /宵禁 关\n"
            "· /宵禁 23:30-07:00 —— 设置时段并开启"
        )

    async def _set_curfew_config(self, payload: dict) -> str:
        """写宵禁配置并立即启停后台任务，返回给用户的结果文案。"""
        try:
            self.cfg.apply_payload({"automate": payload})
        except ValueError as e:
            return f"❌ 参数无效：{e}"
        await self._apply_automate_sync()

        start = self.cfg.get("automate", "curfew_start", "23:00")
        end = self.cfg.get("automate", "curfew_end", "07:00")
        enabled = bool(self.cfg.get("automate", "curfew_enable", False))
        state = self.automate.is_enforcing()
        base = f"🌙 宵禁{'已开启' if enabled else '已关闭'} · 时段 {start} ~ {end}"
        if enabled and state:
            base += "\n🔴 当前正处于宵禁时段，已自动开启全体禁言。"
        elif enabled:
            base += "\n🟢 当前不在宵禁时段，到点会自动全体禁言。"
        return base

    def _curfew_status_text(self) -> str:
        enabled = bool(self.cfg.get("automate", "curfew_enable", False))
        start = self.cfg.get("automate", "curfew_start", "23:00")
        end = self.cfg.get("automate", "curfew_end", "07:00")
        if not enabled:
            return "🌙 宵禁：未开启\n提示：/宵禁 23:00-07:00 可设置时段并开启。"
        in_window = self.automate.is_in_curfew()
        state = "🔴 当前正处于宵禁时段（全体禁言中）" if in_window else "🟢 当前不在宵禁时段"
        return f"🌙 宵禁：已开启 · 时段 {start} ~ {end}\n{state}"

    # ========== 指令：面板 / 自检 / 配置向导 ==========
    @filter.command("面板", alias={"群管面板", "状态"})
    async def cmd_dashboard(self, event: AstrMessageEvent):
        """查看本群群管面板（一屏总览已开启能力与关键参数）。"""
        yield event.plain_result(self.panel.dashboard(event))

    @filter.command("自检", alias={"体检", "诊断"})
    async def cmd_self_check(self, event: AstrMessageEvent):
        """一键自检：权限 / 适配器 / 存储 / 配置一致性。"""
        yield event.plain_result(await self.panel.self_check(event))

    @filter.command("配置", alias={"配置向导", "设置向导"})
    async def cmd_config_guide(self, event: AstrMessageEvent, arg: str = ""):
        """配置向导：/配置 [风控|活跃|互动|宵禁|按群|本地]"""
        yield event.plain_result(self.panel.config_guide(arg))

    # ========== 指令：帮助 ==========
    @filter.command("群管帮助", alias={"磐石帮助", "群管"})
    async def cmd_help(self, event: AstrMessageEvent):
        """查看帮助。"""
        yield event.plain_result(HELP_TEXT)

    # ========== LLM 工具（供 AI 自主调用）==========
    @filter.llm_tool(name="panshi_ban_user")
    async def llm_ban(self, event: AstrMessageEvent, target: str, duration: int = 60, reason: str = ""):
        """禁言群成员。

        Args:
            target(string): 目标 QQ 号
            duration(number): 禁言时长（秒），默认 60
            reason(string): 禁言原因
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        seconds = self.cfg.clamp_ban_time(int(duration or 60))
        yield event.plain_result(await self.normal.set_ban(event, target, seconds))

    @filter.llm_tool(name="panshi_unban_user")
    async def llm_unban(self, event: AstrMessageEvent, target: str):
        """解除群成员的禁言。

        Args:
            target(string): 目标 QQ 号
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.cancel_ban(event, target))

    @filter.llm_tool(name="panshi_kick_user")
    async def llm_kick(self, event: AstrMessageEvent, target: str, reason: str = ""):
        """将群成员踢出群聊（需管理员权限）。

        Args:
            target(string): 目标 QQ 号
            reason(string): 踢出原因
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.kick(event, target, reject=False, reason=reason))

    @filter.llm_tool(name="panshi_recall_messages")
    async def llm_recall(self, event: AstrMessageEvent, count: int = 1):
        """撤回群内最近的消息。

        Args:
            count(number): 撤回条数，默认 1
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.recall(event, count=int(count or 1)))

    @filter.llm_tool(name="panshi_whole_ban")
    async def llm_whole_ban(self, event: AstrMessageEvent, enable: bool = True):
        """开启或关闭全体禁言。

        Args:
            enable(boolean): true 为开启全体禁言，false 为关闭
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.whole_ban(event, enable=bool(enable)))

    @filter.llm_tool(name="panshi_warn_user")
    async def llm_warn(self, event: AstrMessageEvent, target: str, reason: str = ""):
        """警告群成员（累计达阈值会自动禁言或踢出）。

        Args:
            target(string): 目标 QQ 号
            reason(string): 警告原因
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.warning.add_warning(event, target, reason))

    @filter.llm_tool(name="panshi_send_notice")
    async def llm_notice(self, event: AstrMessageEvent, content: str):
        """发布群公告。

        Args:
            content(string): 公告内容
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        yield event.plain_result(await self.normal.send_notice(event, content))

    @filter.llm_tool(name="panshi_set_curfew")
    async def llm_set_curfew(
        self,
        event: AstrMessageEvent,
        enable: bool = True,
        start: str = "",
        end: str = "",
    ):
        """设置或开关宵禁（宵禁时段内自动对全群开启/解除全体禁言）。

        Args:
            enable(boolean): 是否开启宵禁
            start(string): 开始时间 HH:MM（如 23:30），留空保持不变
            end(string): 结束时间 HH:MM（如 07:00），留空保持不变
        """
        if not self._check(event):
            yield event.plain_result(self._no_perm())
            return
        payload = {}
        if isinstance(enable, bool):
            payload["curfew_enable"] = enable
        if str(start or "").strip():
            payload["curfew_start"] = str(start).strip()
        if str(end or "").strip():
            payload["curfew_end"] = str(end).strip()
        if not payload:
            yield event.plain_result(self._curfew_status_text())
            return
        yield event.plain_result(await self._set_curfew_config(payload))

    # ========== 内部工具 ==========
    def _llm_available(self) -> bool:
        """智能识别是否配置了可用的 LLM 供应商。

        未配置时不再报「未找到 LLM 供应商」，而是走本地规则解析，
        本地也没覆盖时给出可执行的引导。
        """
        try:
            provider_id = (self.cfg.smart.get("smart_provider_id", "") or "").strip()
            if provider_id:
                return True
            ctx = getattr(self, "context", None)
            if ctx is None:
                return False
            try:
                providers = ctx.get_all_providers() or []
            except Exception:
                providers = []
            return bool(providers)
        except Exception:
            return False

    def _check(self, event, required: PermLevel = PermLevel.ADMIN) -> bool:
        group_id = get_group_id(event)
        if group_id and not self.cfg.group_enabled(group_id):
            return False
        return check_permission(event, required, self._super_admins)

    @staticmethod
    def _no_perm(level: str = "管理员") -> str:
        return f"⛔ 权限不足，该操作需要「{level}」及以上权限。"

    def _split_target_duration(self, event, arg: str) -> tuple[str | None, str]:
        """从参数里拆出目标与时长。

        支持: "/禁言 @某人 10m"、"/禁言 10m"（配合引用）、"/禁言 @某人"。
        """
        target, _ = parse_target(event)
        duration_text = ""
        if arg:
            for token in arg.split():
                if any(ch.isdigit() for ch in token) or token.startswith(("无限", "永久")):
                    duration_text = token
                    break
        return target, duration_text

    def _strip_target_text(self, event, arg: str) -> str:
        """从参数中移除 @/QQ号，得到纯文本内容。"""
        if not arg:
            return ""
        text = re.sub(r"\[CQ:at,qq=\d+\]", "", arg)
        text = re.sub(r"@\S+", "", text)
        return text.strip()

    @staticmethod
    def _message_id(event) -> str | None:
        try:
            raw = getattr(event, "message_obj", None)
            mid = getattr(raw, "message_id", None) if raw else None
            return str(mid) if mid else None
        except Exception:
            return None


HELP_TEXT = """🪨 磐石 · 智能群管

【基础管理】
/禁言 @某人 10m   — 禁言（时长支持 30s/10m/2h/1d）
/解禁 @某人       — 解除禁言
/全禁 开|关       — 全体禁言
/踢 @某人 [原因]  — 踢出
/拉黑 @某人       — 踢出并拉黑
/撤回 [N]         — 撤回引用消息或最近N条
/净化 [N]         — 批量清屏（默认30条）
/改名 @某人 昵称  — 修改群名片
/头衔 @某人 头衔  — 设置头衔（群主）
/上管 /下管 @某人 — 管理员任免（群主）
/设精 /移精       — 引用消息设/移精华
/群名 新名字      — 修改群名
/公告 内容        — 发布群公告
/群员             — 群成员概览

【警告系统】
/警告 @某人 [原因]  — 记录一次违规
/撤销警告 @某人 [N] — 撤销警告
/违规记录 [@某人]   — 查看违规历史
（累计达阈值自动禁言/踢出）

【群活跃】
/签到  /积分 [@某人]  /排行 [积分|发言]

【自动化】
/宵禁 状态          — 查看宵禁设置
/宵禁 开 | 关       — 开启/关闭宵禁
/宵禁 23:30-07:00   — 设置时段并开启
（宵禁时段自动全体禁言，过点自动解除）
定时公告：面板「自动化」里配置内容与间隔，
到点自动发到所有纳管群，改完即时生效。

【智能交互】
直接对我说人话即可，例如：
 「把刚才刷屏的禁言十分钟」
 「@张三 再发广告就踢了」
 「全群安静一下，禁言全体1小时」
 「宵禁改到23点半到7点」
（无需配置 AI 模型也能识别常用指令；踢人/拉黑等高危操作会先请你确认）

【互动玩法】
/投票 标题 | 选项1 | 选项2  — 发起投票
/投票 2                    — 投票给 2 号
/投票结果                  — 结算投票
/接龙 主题                 — 发起接龙
/我的 [积分|警告|发言|签到] — 成员自助查询

【管理面板】
/面板        — 一屏总览本群已开启能力与参数
/自检        — 体检：权限/适配器/存储/配置
/配置 [主题] — 配置向导（风控|活跃|互动|宵禁|按群|本地）
"""


async def setup(context: Context, config: dict | None = None):
    """AstrBot 插件入口（部分版本约定）。"""
    return PanshiPlugin(context, config)
