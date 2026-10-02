"""自动化：宵禁、定时公告。"""

from __future__ import annotations

import asyncio
import time

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")

from .base_handle import BaseHandle


class AutomateHandle(BaseHandle):
    """宵禁与定时任务。"""

    def __init__(self, config, storage):
        super().__init__(config, storage)
        self._curfew_task: asyncio.Task | None = None
        self._enforcing = False  # 本轮进程内是否已对全体开启禁言
        self._announce_task: asyncio.Task | None = None
        self._announce_last: float = 0.0  # 上次定时公告时间
        # 可注入的回调（由 main.py 提供）
        self._notice_sender = None  # 全体禁言: await sender(gid, enable) -> bool
        self._announce_sender = None  # 发公告: await sender(gid, text)
        self._enabled_groups: list[str] = []
        self._groups_provider = None  # 动态群列表: await provider() -> [gid]
        # 本轮「尝试禁言但结果不确定」的群（下发超时/失败，可能其实已生效）
        self._uncertain_groups: list[str] = []

    # ---------- 宵禁禁言群记录（持久化） ----------
    def _banned_groups(self) -> list[str]:
        """当前被宵禁置为全体禁言的群（来自存储，跨重启有效）。"""
        try:
            return self.db.get_curfew_banned() if self.db is not None else []
        except Exception as e:
            logger.warning(f"[磐石] 读取宵禁禁言群记录失败: {e}")
            return []

    def _remember_banned(self, groups) -> None:
        try:
            if self.db is not None:
                self.db.set_curfew_banned(groups)
        except Exception as e:
            logger.warning(f"[磐石] 保存宵禁禁言群记录失败: {e}")

    def has_pending_lift(self) -> bool:
        """是否存在「记着要解禁」的群。

        来源有两处：
        1. 存储里确认已禁言的群（含上次进程崩溃留下的）；
        2. 本轮尝试禁言但下发结果不确定的群——请求可能其实已经生效，
           必须一并解禁，否则会留下一个永远没人解除的全体禁言。
        """
        return bool(self._banned_groups()) or self._enforcing or bool(self._uncertain_groups)

    def bind_sender(self, sender, groups: list[str]) -> None:
        """由 main.py 注入发送器与群列表（兼容旧接口）。"""
        self._notice_sender = sender
        self._enabled_groups = list(groups or [])

    def bind_announce_sender(self, sender) -> None:
        """注入定时公告发送器：await sender(gid, text)。"""
        self._announce_sender = sender

    def bind_groups_provider(self, provider) -> None:
        """注入动态群列表提供器：await provider() -> [group_id]。

        宵禁/定时公告每轮执行前都会取最新列表，机器人新进的群即刻纳管。
        """
        self._groups_provider = provider

    async def _current_groups(self) -> list[str]:
        if self._groups_provider is not None:
            try:
                groups = await self._groups_provider()
                if groups is not None:
                    self._enabled_groups = [str(g) for g in groups if str(g)]
                    return self._enabled_groups
            except Exception as e:
                logger.warning(f"[磐石] 刷新群列表失败，沿用上次结果: {e}")
        return self._enabled_groups

    # ---------- 宵禁 ----------
    async def start_curfew(self) -> None:
        if self._curfew_task and not self._curfew_task.done():
            return
        self._curfew_task = asyncio.create_task(self._curfew_loop())
        logger.info("[磐石] 宵禁任务已启动")

    async def stop_curfew(self) -> None:
        if self._curfew_task and not self._curfew_task.done():
            self._curfew_task.cancel()
            try:
                await self._curfew_task
            except asyncio.CancelledError:
                pass
            logger.info("[磐石] 宵禁任务已停止")
        self._curfew_task = None

    def is_in_curfew(self) -> bool:
        """当前是否处于宵禁时段（供指令与面板查询）。"""
        return self._in_curfew_window()

    def is_enforcing(self) -> bool:
        """当前是否已对全体开启禁言。"""
        return self._enforcing

    async def apply_now(self) -> str:
        """配置变化后立即同步宵禁状态（不等下一轮 60s 检查）。

        解禁判定不依赖内存标记，而是看**持久化的宵禁禁言群记录**：
        插件重启后 ``_enforcing`` 会归零，若只看内存标记，一个已被全体禁言的群
        将永远没人解除（Issue #2 的残留形态）。

        Returns:
            "off"         宵禁未开启（且确实没有需要解除的群）
            "banned_now"  刚刚对全体开启禁言（当前处于宵禁时段）
            "in_window"   处于宵禁时段且已是禁言状态
            "waiting"     已开启但不在时段内，等待到点
            "lifted_now"  刚刚**确实**解除了全体禁言
            "lift_failed" 需要解除但下发失败（群可能仍在全体禁言中）
        """
        if not self.cfg.automate.get("curfew_enable", False):
            await self.stop_curfew()
            # 修复 Issue #2：关闭宵禁时必须真正下发解禁，否则群会被永久全体禁言。
            # 更关键的是——只有解禁**确实成功**才回报「已解除」，
            # 否则协议端没连上/调用失败时用户会看到与事实相反的提示。
            if self.has_pending_lift():
                if await self._lift_all(self._uncertain_groups):
                    self._uncertain_groups = []
                    curfew_state = "lifted_now"
                else:
                    curfew_state = "lift_failed"
            else:
                curfew_state = "off"
        else:
            await self.start_curfew()
            if self._in_curfew_window():
                banned = await self._ban_all()
                self._enforcing = bool(banned)
                curfew_state = "banned_now" if banned else "in_window"
            elif self.has_pending_lift() or self._uncertain_groups:
                if await self._lift_all(self._uncertain_groups):
                    self._uncertain_groups = []
                    curfew_state = "lifted_now"
                else:
                    curfew_state = "lift_failed"
            else:
                curfew_state = "waiting"

        # 定时公告同步启停
        try:
            if self.cfg.automate.get("announce_enable", False):
                await self.start_announce()
            else:
                await self.stop_announce()
        except Exception as e:
            logger.warning(f"[磐石] 定时公告同步失败: {e}")

        return curfew_state

    async def _curfew_loop(self) -> None:
        """每分钟检查一次是否进入/退出宵禁时段。"""
        while True:
            try:
                await asyncio.sleep(60)
                if not self.cfg.automate.get("curfew_enable", False):
                    # 宵禁已被关闭但仍有群在禁言中：补一次解禁，防止永久全体禁言。
                    # 解禁失败则保留记录，下一轮继续重试（而不是假装已完成）。
                    if self.has_pending_lift() or self._uncertain_groups:
                        if await self._lift_all(self._uncertain_groups):
                            self._uncertain_groups = []
                            logger.info("[磐石] 已补解除宵禁全体禁言")
                        else:
                            logger.warning("[磐石] 宵禁补解禁失败，下一轮继续重试")
                    continue
                in_curfew = self._in_curfew_window()
                if in_curfew:
                    banned = await self._ban_all()
                    self._enforcing = bool(banned)
                elif self.has_pending_lift() or self._uncertain_groups:
                    if await self._lift_all(self._uncertain_groups):
                        self._uncertain_groups = []
                        logger.info("[磐石] 宵禁时段结束，已解除全体禁言")
                    else:
                        logger.warning("[磐石] 宵禁解禁失败，下一轮继续重试")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[磐石] 宵禁循环异常: {e}")

    def _in_curfew_window(self) -> bool:
        start = str(self.cfg.automate.get("curfew_start", "23:00"))
        end = str(self.cfg.automate.get("curfew_end", "07:00"))
        try:
            now = time.strftime("%H:%M")
        except Exception:
            return False

        if start <= end:
            return start <= now < end
        # 跨天，例如 23:00 - 07:00
        return now >= start or now < end

    def _curfew_on_for(self, group_id: str) -> bool:
        """该群视角下宵禁是否开启（面板「独立配置」可按群关掉宵禁）。"""
        try:
            return bool(self.cfg.for_group(group_id).automate.get("curfew_enable", False))
        except Exception:
            return bool(self.cfg.automate.get("curfew_enable", False))

    async def _send_whole_ban(self, group_id: str, enable: bool) -> bool:
        sender = getattr(self, "_notice_sender", None)
        if sender is None:
            logger.warning("[磐石] 宵禁未绑定发送器，无法下发全体禁言")
            return False
        try:
            return bool(await sender(group_id, enable))
        except Exception as e:
            logger.warning(f"[磐石] 宵禁操作群 {group_id} 失败: {e}")
            return False

    async def _ban_all(self) -> list[str]:
        """对所有纳管群开启全体禁言，返回**下发成功**的群号列表。

        未开启宵禁的群（按群覆盖关掉宵禁）会被跳过；成功的群会写入存储，
        这样即使插件重启，也知道哪些群需要被解除。
        """
        groups = await self._current_groups()
        if not groups:
            logger.warning("[磐石] 宵禁没有可操作的群（群列表为空）")
            return []
        banned = list(self._banned_groups())
        attempted: list[str] = []
        for gid in groups:
            if not self._curfew_on_for(gid):
                continue
            if gid in banned:
                continue
            attempted.append(gid)
            if await self._send_whole_ban(gid, True):
                banned.append(gid)
        # 下发失败但「可能已经禁上了」的群：不做记录，但本轮解禁时要一并处理，
        # 否则协议端超时（请求可能已生效）会留下一个永远没人解除的全体禁言。
        self._uncertain_groups = [g for g in attempted if g not in banned]
        # 群已不在纳管范围时，把它从待解禁列表里摘掉（否则永远解不掉）
        banned = [g for g in banned if g in groups]
        self._remember_banned(banned)
        return banned

    async def _lift_all(self, attempted: list[str] | None = None) -> bool:
        """解除所有「记录在案」的宵禁全体禁言。

        与 ``_ban_all`` 不同，解禁**不做按群宵禁开关判断**：只要之前被宵禁
        禁言过就必须解除，否则关掉某群宵禁后该群会被永久全体禁言。
        解禁成功的群从记录中移除；失败的保留，供下一轮重试。

        Args:
            attempted: 本轮**尝试过**禁言的群（下发失败时记录为空，但群里可能
                已经真的被禁言了）。解禁失败时这些群也要一并汇报，不能因为
                「记录里没有」就告诉用户「已解除」。

        Returns:
            True 表示需要解除的群全部成功（或本来就没有需要解除的群）。
        """
        pending = list(self._banned_groups())
        # 记录之外的「尝试过但没确认成功」的群，也要一并尝试解禁（幂等调用）
        for gid in attempted or []:
            if gid not in pending:
                pending.append(gid)
        if not pending:
            self._enforcing = False
            return True
        still: list[str] = []
        for gid in pending:
            if not await self._send_whole_ban(gid, False):
                still.append(gid)
        self._remember_banned(still)
        if still:
            logger.warning(
                f"[磐石] 仍有 {len(still)} 个群的全体禁言未解除，下一轮重试：{still}"
            )
            return False
        self._enforcing = False
        return True

    # ---------- 定时公告 ----------
    async def start_announce(self) -> None:
        if self._announce_task and not self._announce_task.done():
            return
        self._announce_task = asyncio.create_task(self._announce_loop())
        logger.info("[磐石] 定时公告任务已启动")

    async def stop_announce(self) -> None:
        if self._announce_task and not self._announce_task.done():
            self._announce_task.cancel()
            try:
                await self._announce_task
            except asyncio.CancelledError:
                pass
            logger.info("[磐石] 定时公告任务已停止")
        self._announce_task = None

    async def _announce_loop(self) -> None:
        """定时把公告内容发到所有纳管群。"""
        while True:
            try:
                await asyncio.sleep(60)
                if not self.cfg.automate.get("announce_enable", False):
                    continue
                content = str(
                    self.cfg.automate.get("announce_content", "") or ""
                ).strip()
                if not content:
                    continue
                try:
                    interval_min = max(
                        10, int(self.cfg.automate.get("announce_interval_minutes", 360))
                    )
                except (TypeError, ValueError):
                    interval_min = 360
                now = time.time()
                if self._announce_last and (now - self._announce_last) < interval_min * 60:
                    continue
                sender = self._announce_sender
                if sender is None:
                    continue
                groups = await self._current_groups()
                ok = 0
                for gid in groups:
                    try:
                        await sender(gid, content)
                        ok += 1
                    except Exception as e:
                        logger.warning(f"[磐石] 定时公告发送到群 {gid} 失败: {e}")
                self._announce_last = now
                if ok:
                    logger.info(f"[磐石] 定时公告已发送到 {ok} 个群")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[磐石] 定时公告循环异常: {e}")
