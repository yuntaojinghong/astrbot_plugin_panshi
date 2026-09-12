"""Handle 基类：封装群管操作的公共逻辑与 OneBot API 调用。"""

from __future__ import annotations

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger("panshi")


class BaseHandle:
    """所有功能 Handle 的基类。"""

    def __init__(self, config, storage):
        self.cfg = config
        self.db = storage

    # ---------- 公共工具 ----------
    @staticmethod
    def group_id(event) -> int:
        return int(event.get_group_id())

    @staticmethod
    def sender_id(event) -> int:
        return int(event.get_sender_id())

    async def call_api(self, event, action: str, **params):
        """通用 OneBot API 调用，异常安全。

        Args:
            action: API 名，如 set_group_ban
            params: API 参数

        Returns:
            (成功?, 结果或错误信息)
        """
        try:
            method = getattr(event.bot, action, None)
            if method is None:
                return False, f"当前适配器不支持 {action}"
            result = await method(**params)
            return True, result
        except Exception as e:
            logger.warning(f"[磐石] 调用 {action} 失败: {e}")
            return False, str(e)

    def stop(self, event) -> None:
        """阻止事件继续向后传播（避免触发其他插件/LLM）。"""
        try:
            event.stop_event()
        except Exception:
            pass
