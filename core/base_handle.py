"""Handle 基类：封装群管操作的公共逻辑与 OneBot API 调用。"""

from __future__ import annotations

from astrbot.api import logger

from .errors import hint_for, humanize


class BaseHandle:
    """所有功能 Handle 的基类。"""

    def __init__(self, config, storage):
        self.cfg = config
        self.db = storage

    # ---------- 公共工具 ----------
    def cfg_for(self, event=None, group_id=None):
        """取「当前群视角」的配置对象（全局叠加该群 override）。

        修复 Issue #1：面板的「按群独立配置」此前只写存储、运行时不读取。
        handle 内**所有**取配置的地方都应改用本方法，例如::

            cfg = self.cfg_for(event)          # 由事件推导群号
            guard = cfg.guard

            cfg = self.cfg_for(group_id=gid)   # 已有群号（事件上下文之外）

        Args:
            event: 消息事件，用于推导群号。
            group_id: 显式群号；``event`` 为空时使用。

        Returns:
            该群视角的 ``PluginConfig``；无法定位群号时返回全局配置。
        """
        try:
            gid = group_id if group_id not in (None, "") else self.group_id(event)
            return self.cfg.for_group(gid)
        except Exception as e:
            # 静默退化会让「按群配置不生效」这类问题极难排查，必须留痕。
            logger.warning(f"[磐石] 读取按群配置失败，已退化为全局配置: {e}")
            return self.cfg

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
            (成功?, 结果或中文错误提示)
        """
        try:
            method = getattr(event.bot, action, None)
            if method is None:
                return False, f"当前适配器不支持 {action}"
            result = await method(**params)

            # 部分适配器不抛异常，而是返回 {"status":"failed", "retcode":N}
            if isinstance(result, dict):
                status = result.get("status")
                retcode = result.get("retcode")
                if status == "failed" or (retcode not in (None, 0)):
                    reason = result.get("message") or result.get("wording") or result
                    return False, humanize(reason)
            return True, result
        except Exception as e:
            logger.warning(f"[磐石] 调用 {action} 失败: {e}")
            return False, humanize(e)

    @staticmethod
    def failure_hint(action: str) -> str:
        """只取操作建议（💡 那行），用于已经有更准原因说明的场景。"""
        tip = hint_for(action)
        return f"\n💡 {tip}" if tip else ""

    @staticmethod
    def failure_text(action: str, err: object) -> str:
        """组装「失败原因 + 操作建议」。"""
        reason = humanize(err)
        tip = hint_for(action)
        return f"{reason}\n💡 {tip}" if tip else reason

    def stop(self, event) -> None:
        """阻止事件继续向后传播（避免触发其他插件/LLM）。"""
        try:
            event.stop_event()
        except Exception:
            pass
