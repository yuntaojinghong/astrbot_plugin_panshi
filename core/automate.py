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
        self._enforcing = False  # 宵禁时段是否已对全体开启禁言
        self._announce_task: asyncio.Task | None = None
        self._announce_last: float = 0.0  # 上次定时公告时间
        # 可注入的回调（由 main.py 提供）
        self._notice_sender = None  # 全体禁言: await sender(gid, enable)
        self._announce_sender = None  # 发公告: await sender(gid, text)
        self._enabled_groups: list[str] = []
        self._groups_provider = None  # 动态群列表: await provider() -> [gid]

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

        Returns:
            "off"        宵禁未开启
            "banned_now" 刚刚对全体开启禁言（当前处于宵禁时段）
            "in_window"  处于宵禁时段且已是禁言状态
            "waiting"    已开启但不在时段内，等待到点
            "lifted_now" 刚刚解除全体禁言
        """
        if not self.cfg.automate.get("curfew_enable", False):
            await self.stop_curfew()
            # 修复：关闭宵禁时必须真正下发解禁，否则群会被永久全体禁言。
            # 旧实现只改内存状态并回显「已解除」，提示与事实相反。
            if self._enforcing:
                await self._set_all_groups_whole_ban(False)
                curfew_state = "lifted_now"
            else:
                curfew_state = "off"
            self._enforcing = False
        else:
            await self.start_curfew()
            if self._in_curfew_window():
                if not self._enforcing:
                    await self._set_all_groups_whole_ban(True)
                    self._enforcing = True
                    curfew_state = "banned_now"
                else:
                    curfew_state = "in_window"
            elif self._enforcing:
                await self._set_all_groups_whole_ban(False)
                self._enforcing = False
                curfew_state = "lifted_now"
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
                    # 宵禁已被关闭但仍在禁言中：补一次解禁，防止永久全体禁言。
                    if self._enforcing:
                        await self._set_all_groups_whole_ban(False)
                        self._enforcing = False
                    continue
                in_curfew = self._in_curfew_window()
                if in_curfew and not self._enforcing:
                    await self._set_all_groups_whole_ban(True)
                    self._enforcing = True
                elif not in_curfew and self._enforcing:
                    await self._set_all_groups_whole_ban(False)
                    self._enforcing = False
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

    async def _set_all_groups_whole_ban(self, enable: bool) -> None:
        """对所有纳管群开启/关闭全体禁言（群列表每轮动态刷新）。"""
        sender = getattr(self, "_notice_sender", None)
        if sender is None:
            return
        groups = await self._current_groups()
        for gid in groups:
            try:
                await sender(gid, enable)
            except Exception as e:
                logger.warning(f"[磐石] 宵禁操作群 {gid} 失败: {e}")

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
