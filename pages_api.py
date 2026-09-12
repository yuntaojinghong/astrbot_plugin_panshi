"""面板后端 Web API。

通过 ``context.register_web_api()`` 注册路由，供 ``pages/settings/`` 下的
前端页面经 ``window.AstrBotPluginPage`` bridge 调用。

约定：
- 注册的路由带插件名前缀 ``/astrbot_plugin_panshi/...``
- 前端 bridge 调用时**不带**前缀，例如 ``apiGet("groups")``
- 响应统一用 ``astrbot.api.web`` 的 ``json_response`` / ``error_response``
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from astrbot.api import logger
except Exception:  # 便于脱离 AstrBot 单测
    import logging

    logger = logging.getLogger("panshi")

PLUGIN_NAME = "astrbot_plugin_panshi"

# astrbot.api.web 在较新版本才提供；老版本回退到 quart
_WEB_IMPORT_ERROR = ""
try:
    from astrbot.api.web import error_response, json_response, request
except Exception as e:  # pragma: no cover - 取决于 AstrBot 版本
    _WEB_IMPORT_ERROR = str(e)
    json_response = None  # type: ignore
    error_response = None  # type: ignore
    request = None  # type: ignore

    try:
        from quart import jsonify, request as _quart_request

        def json_response(payload: dict):  # type: ignore
            return jsonify(payload)

        def error_response(msg: str, status_code: int = 400):  # type: ignore
            return jsonify({"status": "error", "message": msg}), status_code

        request = _quart_request  # type: ignore
    except Exception:
        pass


class PanshiWebController:
    """注册并处理面板相关的 Web API。

    Args:
        context: AstrBot 的 Context
        service: :class:`PageService` 实例
    """

    def __init__(self, context: Any, service: Any):
        self.context = context
        self.service = service
        self._registered = False
        # 可选钩子：全局配置保存成功后调用（用于宵禁等设置即时生效）
        self.on_config_saved = None

    # ==================================================================
    #  注册
    # ==================================================================
    def register_routes(self) -> bool:
        """注册所有路由。返回是否成功。

        低版本 AstrBot 上没有 ``register_web_api``，此时返回 False，
        由调用方决定是否提示用户。
        """
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[磐石] 当前 AstrBot 版本不支持插件 Pages（缺少 register_web_api），"
                "配置面板将不可用，请升级到 4.24.2 以上"
            )
            return False

        if request is None:
            logger.warning(f"[磐石] Web 请求模块不可用，跳过面板注册: {_WEB_IMPORT_ERROR}")
            return False

        routes: list[tuple[str, Callable, list[str], str]] = [
            ("/bootstrap", self.api_bootstrap, ["GET"], "面板初始化数据"),
            ("/overview", self.api_overview, ["GET"], "运行状态概览"),
            ("/connection", self.api_connection, ["GET"], "协议端连接诊断"),
            ("/groups", self.api_groups, ["GET"], "群列表"),
            ("/groups/refresh", self.api_groups_refresh, ["POST"], "刷新群列表"),
            ("/global", self.api_get_global, ["GET"], "全局默认配置"),
            ("/global", self.api_update_global, ["POST"], "保存全局默认配置"),
            ("/group", self.api_get_group, ["GET"], "单个群的配置"),
            ("/group", self.api_update_group, ["POST"], "保存单个群的配置"),
            ("/group/reset", self.api_reset_group, ["POST"], "重置单个群的配置"),
        ]

        ok_count = 0
        for path, handler, methods, desc in routes:
            try:
                register(
                    f"/{PLUGIN_NAME}{path}",
                    self._wrap(handler),
                    methods,
                    desc,
                )
                ok_count += 1
            except Exception as e:
                logger.error(f"[磐石] 注册路由 {path} 失败: {e}")

        self._registered = ok_count > 0
        if self._registered:
            logger.info(f"[磐石] 配置面板已注册 {ok_count} 个接口")
        return self._registered

    def _wrap(self, handler: Callable) -> Callable:
        """统一异常处理：把 ValueError 转成 400，其余转 500。"""

        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except ValueError as e:
                return _err(str(e), 400)
            except Exception as e:  # pragma: no cover - 兜底
                logger.exception("[磐石] 面板接口异常")
                return _err(f"服务器内部错误: {e}", 500)

        wrapped.__name__ = getattr(handler, "__name__", "handler")
        return wrapped

    # ==================================================================
    #  Handlers
    # ==================================================================
    async def api_bootstrap(self):
        return _ok(await self.service.bootstrap())

    async def api_overview(self):
        return _ok(self.service.overview())

    async def api_connection(self):
        """协议端连接诊断：供面板「重新检测」使用。"""
        return _ok(self.service.connection())

    async def api_groups(self):
        force = _query_bool("force")
        return _ok(await self.service.list_groups(force=force))

    async def api_groups_refresh(self):
        # 重置缓存后重新拉取
        try:
            self.service.group_cache.invalidate()
        except Exception:
            pass
        groups = await self.service.list_groups(force=True)
        err = getattr(self.service.group_cache, "last_error", "")
        return _ok({"groups": groups, "error": err})

    async def api_get_global(self):
        return _ok(self.service.get_global_config())

    async def api_update_global(self):
        payload = await _json_body()
        config = payload.get("config", payload)
        result = _ok(self.service.update_global_config(config))
        # 配置已落盘：通知宿主插件做即时同步（如宵禁启停），失败不影响保存结果
        hook = getattr(self, "on_config_saved", None)
        if callable(hook):
            try:
                await hook()
            except Exception as e:
                logger.warning(f"[磐石] 配置保存后同步失败: {e}")
        return result

    async def api_get_group(self):
        gid = _query_str("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok(self.service.get_group_config(gid))

    async def api_update_group(self):
        payload = await _json_body()
        gid = payload.get("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        config = payload.get("config", {})
        return _ok(self.service.update_group_config(str(gid), config))

    async def api_reset_group(self):
        payload = await _json_body()
        gid = payload.get("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        return _ok(self.service.reset_group_config(str(gid)))


# ======================================================================
#  请求 / 响应辅助
# ======================================================================
def _ok(data: Any):
    if json_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return json_response(data)


def _err(message: str, status_code: int = 400):
    if error_response is None:
        raise RuntimeError("Web 响应模块不可用")
    return error_response(message, status_code=status_code)


def _query_str(key: str, default: str = "") -> str:
    try:
        val = request.query.get(key, default)
        return str(val) if val is not None else default
    except Exception:
        return default


def _query_bool(key: str) -> bool:
    raw = _query_str(key, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


async def _json_body() -> dict:
    try:
        payload = await request.json(default={})
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
