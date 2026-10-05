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

from astrbot.api import logger

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
            ("/export", self.api_export, ["GET"], "导出全部配置"),
            ("/import", self.api_import, ["POST"], "导入配置备份"),
            ("/groups", self.api_groups, ["GET"], "群列表"),
            ("/groups/refresh", self.api_groups_refresh, ["POST"], "刷新群列表"),
            ("/global", self.api_get_global, ["GET"], "全局默认配置"),
            ("/global", self.api_update_global, ["POST"], "保存全局默认配置"),
            ("/group", self.api_get_group, ["GET"], "单个群的配置"),
            ("/group", self.api_update_group, ["POST"], "保存单个群的配置"),
            ("/group/reset", self.api_reset_group, ["POST"], "重置单个群的配置"),
            # 积分商城 / 奖池的可视化编辑（省得在配置页手写 JSON 数组）
            ("/shop/items", self.api_shop_items, ["GET"], "商品列表"),
            ("/shop/items", self.api_shop_save_items, ["POST"], "保存商品列表"),
            ("/shop/prizes", self.api_shop_prizes, ["GET"], "奖池列表"),
            ("/shop/prizes", self.api_shop_save_prizes, ["POST"], "保存奖池"),
            ("/shop/reset", self.api_shop_reset, ["POST"], "恢复商品与奖池的默认值"),
        ]

        # 注册的路径**不带插件名前缀**。
        #
        # 这一点搞错了就是「未找到该路由」，而且所有路由一起失效（单段也失效）。
        # 真实链路（读自 AstrBot 4.28.2 的 plugins.cpython-312.pyc）：
        #
        #   路由声明   /plugins/extensions/{plugin_path:path}
        #   plugin_path = "astrbot_plugin_panshi/shop/items"
        #   _match_registered_web_api(registered_web_apis, plugin_path, method)
        #     request_path = "/" + subpath.lstrip("/")     -> "/astrbot_plugin_panshi/shop/items"
        #     re.fullmatch(pattern, request_path)          -> 拿注册路径去匹配**整条**
        #
        # 也就是说匹配时用的就是完整 plugin_path，插件名没有被剥掉。
        # 所以注册 "/shop/items" 才会命中；注册带前缀的
        # "/astrbot_plugin_panshi/shop/items" 则永远匹配不上。
        #
        # 前端 bridge 那边是 apiGet("shop/items")，由 SDK 自己补插件名，
        # 与这里的约定一致。
        ok_count = 0
        registered_paths: list[str] = []
        for path, handler, methods, desc in routes:
            candidate = path if path.startswith("/") else f"/{path}"
            try:
                register(candidate, self._wrap(handler), methods, desc)
                ok_count += 1
                registered_paths.append(candidate)
            except Exception as e:
                logger.error(f"[磐石] 注册路由 {candidate} 失败: {e}")

        self._registered = ok_count > 0
        if self._registered:
            # 把实际注册的路径打出来。线上再遇到「未找到该路由」，
            # 这条日志能立刻看出接口注册成了什么形式。
            logger.info(
                f"[磐石] 配置面板已注册 {ok_count} 个接口，"
                f"子路径示例: {', '.join(registered_paths[:4])}"
            )
        else:
            logger.warning(
                "[磐石] 配置面板一个接口都没注册上。"
                f"register_web_api 是否可用: {callable(register)}；"
                f"Web 请求模块: {'可用' if request is not None else _WEB_IMPORT_ERROR}"
            )
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

    async def api_export(self):
        return _ok(self.service.export_all())

    async def api_import(self):
        payload = await _json_body()
        result = _ok(self.service.import_all(payload))
        # 导入会替换全局配置与各群覆盖，必须重新同步宵禁/公告任务
        await self._notify_saved()
        return result

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
        await self._notify_saved()
        return result

    async def _notify_saved(self) -> None:
        """通知宿主插件做即时同步（宵禁启停 / 定时公告），失败不影响保存结果。

        按群配置也会影响宵禁：某个群若把 ``automate.curfew_enable`` 关掉，
        必须立刻重新同步，否则面板上「已关掉」而群里仍被执行全体禁言。
        """
        hook = getattr(self, "on_config_saved", None)
        if callable(hook):
            try:
                await hook()
            except Exception as e:
                logger.warning(f"[磐石] 配置保存后同步失败: {e}")

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
        result = _ok(self.service.update_group_config(str(gid), config))
        await self._notify_saved()
        return result

    async def api_reset_group(self):
        payload = await _json_body()
        gid = payload.get("group_id")
        if not gid:
            return _err("缺少参数 group_id", 400)
        result = _ok(self.service.reset_group_config(str(gid)))
        await self._notify_saved()
        return result

    # ------------------------------------------------------------------ #
    #  积分商城 / 奖池的可视化编辑
    # ------------------------------------------------------------------ #
    #
    # 商品和奖池原来只能去 AstrBot 配置页手写 JSON 数组，加一件商品要
    # 小心翼翼补逗号引号。这几个接口配合 pages/settings 里的管理页，
    # 让用户点「＋」填三个框就能加一件。
    #
    # 数据存在插件的 storage 里（配置里的默认值作为初始内容），
    # 所以改完立即生效，不需要重载插件。

    def _shop(self):
        """取 ShopHandle；没有（旧版本/未初始化）时返回 None。"""
        return getattr(self.service, "shop", None)

    async def api_shop_items(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        return _ok({"items": shop.editable_items()})

    async def api_shop_save_items(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        items = payload.get("items")
        if not isinstance(items, list):
            return _err("缺少参数 items（数组）", 400)
        result = shop.save_items(items)
        if not result.get("ok"):
            # 校验失败要原样返回原因，前端才能逐条展示给用户
            return _err("；".join(result.get("problems") or ["保存失败"]), 400)
        return _ok({"items": shop.editable_items(), "saved": len(result["items"])})

    async def api_shop_prizes(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        return _ok({"prizes": shop.editable_prizes()})

    async def api_shop_save_prizes(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        payload = await _json_body()
        prizes = payload.get("prizes")
        if not isinstance(prizes, list):
            return _err("缺少参数 prizes（数组）", 400)
        result = shop.save_prizes(prizes)
        if not result.get("ok"):
            return _err("；".join(result.get("problems") or ["保存失败"]), 400)
        return _ok({"prizes": shop.editable_prizes(),
                    "saved": len(result["prizes"]),
                    "total_chance": result.get("total_chance", 0)})

    async def api_shop_reset(self):
        shop = self._shop()
        if shop is None:
            return _err("商城模块不可用", 503)
        shop.reset_shop_data()
        return _ok({"items": shop.editable_items(),
                    "prizes": shop.editable_prizes(),
                    "message": "已恢复为配置里的默认商品与奖池"})


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
