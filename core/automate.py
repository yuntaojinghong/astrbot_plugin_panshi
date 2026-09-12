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
        self._curfew_task = None
        logger.info("[磐石] 宵禁任务已停止")

    async def _curfew_loop(self) -> None:
        """每分钟检查一次是否进入/退出宵禁时段。"""
        while True:
            try:
                await asyncio.sleep(60)
                if not self.cfg.automate.get("curfew_enable", False):
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
        """对所有启用本插件的群开启/关闭全体禁言。

        注意：这里无法直接拿到 bot 实例，需由 main.py 注入 group_ids 与 sender。
        """
        sender = getattr(self, "_notice_sender", None)
        if sender is None:
            return
        groups = getattr(self, "_enabled_groups", []) or []
        for gid in groups:
            try:
                await sender(gid, enable)
            except Exception as e:
                logger.warning(f"[磐石] 宵禁操作群 {gid} 失败: {e}")

    def bind_sender(self, sender, groups: list[str]) -> None:
        """由 main.py 注入发送器与群列表。"""
        self._notice_sender = sender
        self._enabled_groups = groups
